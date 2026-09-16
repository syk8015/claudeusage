#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-limit.py — 5시간/주간 한도가 무엇을 먹고 오르는지 역산한다.

구독은 토큰당 과금이 아니라 한도에 걸린다. 그런데 그 한도가 캐시 읽기를 어떻게
세는지, 모델마다 배율이 다른지는 공개돼 있지 않다. 로컬에도 기록이 없다.

그래서 상태바가 표본을 남겨 뒀다(data/ratelimit-log.jsonl). 여기에 게이지 숫자와
누적 비용이 같이 온다. 그것과 대화 기록(~/.claude/projects)의 요청별 토큰을
시간축에서 맞물리면, 게이지가 1%p 오를 때마다 그 사이에 들어간 토큰을 알 수 있다.
그 표를 200개쯤 모아 게이지의 식을 거꾸로 푼다.

    claudeusage limit            창별 표
    claudeusage limit --steps    변화점 전부
    claudeusage limit --fit      가설 적합
    claudeusage limit --weekly   7일 창 기준
    claudeusage limit --csv 경로  변화점을 CSV로
    claudeusage limit --log 경로  다른 표본 파일
"""

import json, os, sys, glob, time, calendar, bisect
from collections import defaultdict

from .i18n import t
from . import paths
from .cc_value import load

B, D, R, Y, X = "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[0m"

DEFAULT_LOG = paths.ratelimit_log()
PROJECTS = os.path.expanduser("~/.claude/projects")

# 로그에 있는 그대로의 토큰 종류. CSV 와 --steps 표는 이걸 쓴다.
TOK = ["input", "output", "cache_read", "cache_write_5m", "cache_write_1h"]

# 적합에 쓰는 묶음. 실측 결과 input 은 전체의 0.0%, cache_write_5m 은 0.02% 라
# 따로 두면 계수가 잡음만 따라간다. "처음 들어온 토큰"과 "다시 읽은 토큰"으로 나눈다.
GROUPS = [("출력", ["output"]),
          ("신규입력", ["input", "cache_write_5m", "cache_write_1h"]),
          ("캐시읽기", ["cache_read"])]

# 묶음 이름과 가설 이름은 dict 키이자 cc-chat.py 가 run_fit 에 넘기는 값이라
# 한국어 그대로 둔다. 화면에 나갈 때만 아래 표로 갈아 끼운다.
GROUP_LABEL = {"출력": t("output", "출력"),
               "신규입력": t("fresh input", "신규입력"),
               "캐시읽기": t("cache read", "캐시읽기")}


def gl(name):
    return GROUP_LABEL.get(name, name)

# ── 모델별 100만 토큰당 단가 (입력, 출력). 반드시 긴 이름부터.
#
# cc-value.py 의 RATES 는 ("fable-5", 10, 50) 이 앞에 있어 claude-fable-5-1 이
# 거기 먼저 걸린다. 실측으로 확인한 결과 이건 틀렸다. 100% 단일 모델 세션에서
# 상태바의 누계와 대조해 단가를 역산하면:
#     claude-fable-5-1  →  입력 $5.04 / 출력 $25.21   (= $5/$25)
#     claude-opus-5     →  입력 $5.04 / 출력 $25.20   (= $5/$25)
#     claude-fable-5    →  입력 $10.02 / 출력 $50.10  (= $10/$50)
# 즉 Fable 5.1 은 Fable 5 의 절반이고 Opus 5 와 같다. 이 표를 안 고치면
# Fable 5.1 세션 비용이 정확히 2배로 부풀고, 모델별 한도 배율이 통째로 틀린다.
RATES = [
    ("fable-5-1", 5, 25), ("mythos-5-1", 5, 25),
    ("fable-5", 10, 50), ("mythos", 10, 50),
    ("opus-5", 5, 25), ("opus-4-8", 5, 25), ("opus-4-7", 5, 25), ("opus-4-6", 5, 25),
    ("sonnet-5", 2, 10), ("sonnet-4-6", 3, 15), ("haiku", 1, 5),
]
FAST = (10, 50)          # fast 모드 프리미엄 (Opus 5/4.8)
DEFAULT = (5, 25)
MODEL_KEYS = [k for k, _, _ in RATES]

# ── 로그 밖 소비. 한도는 계정 전체에 걸리는데 로컬 기록은 클로드코드 것뿐이다.
# claude.ai 채팅·앱이나 다른 기기에서 쓴 몫도 게이지를 올린다. 실측: 2026-09-14
# 한국시간 11:40~15:20 에 채팅을 쓰는 동안 15→53%(+38)가 로컬 요청 8건 $1.75 로
# 올랐다. 평소 Opus 5 는 1%p 당 $2 안팎이다. 그런 변화점을 적합에 넣으면 그 몫이
# 로컬 토큰에 억지로 얹혀 배율이 틀어지므로 따로 표시하고 뺀다.
# Δ%p 가 작으면 정수 눈금 탓에 원래 들쭉날쭉하므로 큰 점프만 본다.
OFFLOG_MIN_PCT = 5
OFFLOG_MAX_RATE = 0.3    # 1%p 당 $ 이보다 싸면 로컬 요청으로 설명이 안 된다


def is_offlog(s):
    return s["dpct"] >= OFFLOG_MIN_PCT and s.get("cost", 0.0) / s["dpct"] < OFFLOG_MAX_RATE


def model_key(model):
    """모델명을 아는 값으로만 접는다. [1m] 접미는 단가가 같으므로 같은 모델로 본다."""
    m = (model or "").replace("[1m]", "")
    for k in MODEL_KEYS:
        if k in m:
            return k
    return "other"


def usage_cost(msg):
    """요청 하나의 API 환산가. cc-value.py 와 같은 식이되 단가표만 바로잡았다."""
    u = msg.get("usage") or {}
    if u.get("speed") == "fast":
        pin, pout = FAST
    else:
        pin, pout = DEFAULT
        for key, i, o in RATES:
            if key in (msg.get("model") or ""):
                pin, pout = i, o
                break
    cc = u.get("cache_creation") or {}
    return (u.get("input_tokens", 0) * pin
            + u.get("output_tokens", 0) * pout
            + u.get("cache_read_input_tokens", 0) * pin * 0.1
            + cc.get("ephemeral_1h_input_tokens", 0) * pin * 2
            + cc.get("ephemeral_5m_input_tokens", 0) * pin * 1.25) / 1_000_000


def to_epoch(s):
    """2026-09-01T07:38:58Z / 2026-09-03T16:15:07.088Z 둘 다 받는다. UTC."""
    if not isinstance(s, str) or len(s) < 19:
        return None
    frac, rest = 0.0, s[19:]
    if rest.startswith("."):
        try:
            frac = float("0" + rest.rstrip("Z"))
        except ValueError:
            frac = 0.0
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + frac
    except ValueError:
        return None


def utc(ts, fmt="%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(ts)) if ts else "-"


# ────────────────────────────────────────────── 1. 한도 표본 읽기

def load_samples(log_path, field):
    """상태바 표본을 읽고 쓸 수 없는 줄을 걸러낸다. field 는 five_hour / seven_day."""
    rows, skip = [], defaultdict(int)
    raw = load(log_path)
    for e in raw:
        sid = e.get("session_id")
        if not sid or sid == "test-preview":
            skip[t("synthetic/unnamed rows", "합성·무명 행")] += 1
            continue
        if e.get("agent_type") or (e.get("agent") or {}).get("name"):
            skip[t("subagent renders", "서브에이전트 렌더")] += 1
            continue
        w = (e.get("rate_limits") or {}).get(field) or {}
        pct, reset = w.get("used_percentage"), w.get("resets_at")
        if pct is None or reset is None:
            skip[t("no %s value", "%s 값 없음") % field] += 1
            continue
        ts = to_epoch(e.get("logged_at"))
        if ts is None:
            skip[t("timestamp parse failed", "시각 파싱 실패")] += 1
            continue
        rows.append({"t": ts, "sid": sid, "pct": int(round(float(pct))),
                     "reset": int(reset),
                     "cost": (e.get("cost") or {}).get("total_cost_usd")})
    rows.sort(key=lambda r: (r["t"], r["sid"]))
    return rows, skip, len(raw)


def cost_by_window(rows, win_len):
    """상태바 기준 창별 비용 증가분. 대조용.

    세션의 total_cost_usd 는 세션 시작부터의 누계라 창 경계와 안 맞는다. 그래서
    같은 세션의 연속한 두 표본 사이 증가분을 뒤쪽 표본이 속한 창에 넣는다.
    세션의 첫 표본이 이미 0 이 아닌 경우(관측 전에 쓴 몫)는 셀 수 없어 빠진다.
    """
    by_sid = defaultdict(list)
    for r in rows:
        if r["cost"] is not None:
            by_sid[r["sid"]].append(r)
    out = defaultdict(float)
    for pts in by_sid.values():
        for a, b in zip(pts, pts[1:]):
            if b["cost"] > a["cost"]:
                out[b["reset"]] += b["cost"] - a["cost"]
    return out


# ────────────────────────────────────────────── 2. 창 묶기 + 변화점 뽑기

def windows_and_steps(rows, win_len):
    """resets_at 으로 창을 묶고, 창 안에서 누적최대 게이지가 오른 지점만 뽑는다.

    창 경계는 시계로 계산하면 안 된다. 실측에서 5시간 창이 09:20 → 23:00 → 05:00
    으로 두 번 밀렸다. 놀다가 다시 쓰기 시작할 때 창이 새로 열리기 때문이다.

    세션마다 자기가 마지막으로 받은 스냅샷을 들고 있어서, 그냥 시간순으로 늘어놓으면
    같은 창 안에서도 값이 내려가는 것처럼 보인다(실측 488건). 실제로 내려간 게 아니라
    오래된 숫자를 다시 본 것이므로 누적최대로 읽는다.

    시각이 셋 나온다.
      t0 = 이전 값이 마지막으로 확인된 시각 (상승을 얼마나 좁게 집었는지 보는 용도)
      t1 = 새 값이 처음 보인 시각
      tf = 비용을 귀속시킬 구간의 시작 = 직전 변화점의 t1

    귀속은 (tf, t1] 로 한다. (t0, t1] 로 하면 변화점 사이에 낀 요청이 통째로 빠진다
    (실측: 관측 시간의 2.1%만 덮였다). 게이지는 늦게 관측되므로, 직전 관측 이후에
    끝난 요청 전부가 이번에 보인 상승의 원인이다.
    """
    by_win = defaultdict(list)
    for r in rows:
        by_win[r["reset"]].append(r)

    wins, steps = [], []
    for reset in sorted(by_win):
        ws = by_win[reset]                      # load_samples 에서 이미 시간순
        cur = t_prev = None
        stale = 0
        w_steps = []
        # 0 부터 봤다면 창이 열린 시각까지 거슬러 올라가도 된다. 이미 소진된 상태로
        # 관측이 시작됐다면 그 앞은 우리가 모르는 몫이라 첫 표본부터 센다.
        tf = reset - win_len if ws[0]["pct"] == 0 else ws[0]["t"]
        for r in ws:
            p = r["pct"]
            if cur is None:
                cur, t_prev = p, r["t"]
                continue
            if p > cur:
                w_steps.append({"reset": reset, "tf": tf, "t0": t_prev, "t1": r["t"],
                                "p0": cur, "p1": p, "dpct": p - cur})
                cur, t_prev, tf = p, r["t"], r["t"]
            elif p == cur:
                t_prev = r["t"]
            else:
                stale += 1                      # 오래된 스냅샷. 무시한다.
        wins.append({"reset": reset, "t_first": ws[0]["t"], "t_last": ws[-1]["t"],
                     "p_first": ws[0]["pct"], "p_last": max(r["pct"] for r in ws),
                     "n": len(ws), "stale": stale, "steps": w_steps,
                     "sessions": len({r["sid"] for r in ws})})
        steps.extend(w_steps)
    return wins, steps


# ────────────────────────────────────────────── 3. 대화 기록에서 요청 색인

def request_index(t_lo, t_hi, verbose=False):
    """관측 구간에 끝난 모든 요청을 모은다.

    한도는 계정 전체에 걸리므로 표본에 찍힌 세션만 보면 안 되고, 그 시간대에 돈
    세션을 전부 봐야 한다. 서브에이전트 기록도 별도 파일이라 같이 읽는다.
    한 응답이 content block 수만큼 여러 줄로 기록되고 전부 같은 usage 를 달기
    때문에 message.id 로 중복을 제거한다(실측 배수 약 2.1배).
    """
    paths = sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")))
    paths += sorted(glob.glob(os.path.join(PROJECTS, "*", "*", "subagents", "*.jsonl")))

    seen, reqs, scanned, dupes = set(), [], 0, 0
    for p in paths:
        try:
            if os.path.getmtime(p) < t_lo:      # 구간 시작 전에 끝난 파일
                continue
        except OSError:
            continue
        scanned += 1
        try:
            f = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                # 값싼 사전 검사. 대부분의 줄이 여기서 걸러진다.
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue                    # 깨진 서로게이트 페어 줄
                if e.get("type") != "assistant":
                    continue
                msg = e.get("message") or {}
                u, mid = msg.get("usage"), msg.get("id")
                if not u or not mid:
                    continue
                if (msg.get("model") or "").startswith("<"):
                    continue                    # <synthetic> — 실제 호출이 아니다
                if mid in seen:
                    dupes += 1
                    continue
                seen.add(mid)
                ts = to_epoch(e.get("timestamp"))
                if ts is None or ts <= t_lo or ts > t_hi:
                    continue
                cc = u.get("cache_creation") or {}
                reqs.append({
                    "t": ts, "mk": model_key(msg.get("model")), "cost": usage_cost(msg),
                    "input": u.get("input_tokens", 0) or 0,
                    "output": u.get("output_tokens", 0) or 0,
                    "cache_read": u.get("cache_read_input_tokens", 0) or 0,
                    "cache_write_5m": cc.get("ephemeral_5m_input_tokens", 0) or 0,
                    "cache_write_1h": cc.get("ephemeral_1h_input_tokens", 0) or 0,
                })
    reqs.sort(key=lambda r: r["t"])
    if verbose:
        print(t("%s%d log files · %d requests (%d duplicates removed)%s",
                "%s기록 %d개 파일에서 요청 %d건 (중복 %d건 제거)%s")
              % (D, scanned, len(reqs), dupes, X), file=sys.stderr)
    return reqs


def attribute(steps, reqs):
    """변화점마다 (tf, t1] 구간의 요청을 합산한다."""
    times = [r["t"] for r in reqs]
    for s in steps:
        chunk = reqs[bisect.bisect_right(times, s["tf"]):bisect.bisect_right(times, s["t1"])]
        s["reqs"] = len(chunk)
        s["cost"] = sum(r["cost"] for r in chunk)
        for k in TOK:
            s[k] = sum(r[k] for r in chunk)
        cm, tm = defaultdict(float), defaultdict(lambda: defaultdict(int))
        for r in chunk:
            cm[r["mk"]] += r["cost"]
            for k in TOK:
                tm[r["mk"]][k] += r[k]
        s["cost_by_model"] = dict(cm)
        s["tok_by_model"] = {m: dict(v) for m, v in tm.items()}
        s["offlog"] = is_offlog(s)
    return steps


def agg(group):
    """변화점 여러 개를 하나로 합친다. 변화점 하나든 창 하나든 같은 모양이 된다."""
    a = {"dpct": float(sum(s["dpct"] for s in group)),
         "reqs": sum(s.get("reqs", 0) for s in group),
         "cost": sum(s.get("cost", 0.0) for s in group)}
    for k in TOK:
        a[k] = sum(s.get(k, 0) for s in group)
    for name, keys in GROUPS:
        a[name] = sum(a[k] for k in keys)
    cm, tm = defaultdict(float), defaultdict(lambda: defaultdict(float))
    for s in group:
        for m, c in s.get("cost_by_model", {}).items():
            cm[m] += c
        for m, tk in s.get("tok_by_model", {}).items():
            for name, keys in GROUPS:
                tm[m][name] += sum(tk.get(k, 0) for k in keys)
    a["cost_by_model"] = dict(cm)
    a["tok_by_model"] = {m: dict(v) for m, v in tm.items()}
    return a


# ────────────────────────────────────────────── 4. 최소제곱 (절편 없음)

def lstsq(Xm, y):
    """정규방정식 + 부분 피벗 가우스 소거. 미지수가 몇 개뿐이라 numpy 가 필요 없다."""
    n = len(Xm[0])
    A = []
    for i in range(n):
        row = [sum(Xm[r][i] * Xm[r][j] for r in range(len(Xm))) for j in range(n)]
        row.append(sum(Xm[r][i] * y[r] for r in range(len(Xm))))
        A.append(row)
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(A[r][c]))
        if abs(A[piv][c]) < 1e-12:
            return None                          # 특이행렬. 그 열에 데이터가 없다.
        A[c], A[piv] = A[piv], A[c]
        for r in range(n):
            if r == c:
                continue
            f = A[r][c] / A[c][c]
            for k in range(c, n + 1):
                A[r][k] -= f * A[c][k]
    return [A[i][n] / A[i][i] for i in range(n)]


def nnls(Xm, y, labels):
    """음수 계수가 나오면 그 열을 빼고 다시 푼다.

    어떤 항목을 더 썼는데 한도가 내려간다는 건 있을 수 없다. 음수가 나오면 그 항목이
    한도에 거의 영향이 없다는 뜻이므로 0 으로 놓고 빼는 게 맞다. 어느 열을 뺐는지는
    같이 돌려준다.
    """
    idx = list(range(len(labels)))
    while idx:
        Xs = [[row[i] for i in idx] for row in Xm]
        b = lstsq(Xs, y)
        if b is None:                            # 데이터가 가장 적은 열부터 뺀다
            tot = [sum(abs(row[i]) for row in Xm) for i in idx]
            idx.pop(tot.index(min(tot)))
            continue
        worst = min(range(len(b)), key=lambda k: b[k])
        if b[worst] >= 0:
            full = [0.0] * len(labels)
            for k, i in enumerate(idx):
                full[i] = b[k]
            return full, [labels[i] for i in range(len(labels)) if i not in idx]
        idx.pop(worst)
    return None, list(labels)


def fit_stats(Xm, y, b):
    pred = [sum(Xm[r][i] * b[i] for i in range(len(b))) for r in range(len(y))]
    res = [y[r] - pred[r] for r in range(len(y))]
    ss_tot = sum(v * v for v in y)              # 절편이 없으므로 중심화하지 않는다
    r2 = 1 - sum(v * v for v in res) / ss_tot if ss_tot else 0.0
    mae = sum(abs(v) for v in res) / len(res) if res else 0.0
    mean_y = sum(y) / len(y) if y else 0.0
    return r2, mae, (100 * mae / mean_y if mean_y else 0.0)


# ────────────────────────────────────────────── 5. 가설

def build(rows, kind):
    """가설 이름 → (설계행렬, 열 이름). 열 이름이 그대로 결과에 나온다."""
    if kind == "요청 수":
        return [[float(r["reqs"])] for r in rows], ["요청"]
    if kind == "API환산 $":
        return [[r["cost"]] for r in rows], ["$"]
    if kind == "모델별 $":
        ms = sorted({m for r in rows for m in r["cost_by_model"]
                     if sum(x["cost_by_model"].get(m, 0) for x in rows) > 1.0})
        return [[r["cost_by_model"].get(m, 0.0) for m in ms] for r in rows], ms
    if kind == "토큰 종류별":
        names = [g[0] for g in GROUPS]
        return [[r[n] / 1e6 for n in names] for r in rows], names
    raise ValueError(kind)


HYPOTHESES = ["요청 수", "API환산 $", "모델별 $", "토큰 종류별"]

HYP_LABEL = {"요청 수": t("requests", "요청 수"),
             "API환산 $": t("API-eq $", "API환산 $"),
             "모델별 $": t("per model $", "모델별 $"),
             "토큰 종류별": t("per token type", "토큰 종류별")}


def run_fit(rows, kind):
    Xm, labels = build(rows, kind)
    if not labels:
        return None
    y = [r["dpct"] for r in rows]
    b, dropped = nnls(Xm, y, labels)
    if b is None:
        return None
    r2, mae, rel = fit_stats(Xm, y, b)
    return {"b": b, "labels": labels, "dropped": dropped,
            "r2": r2, "mae": mae, "rel": rel, "n": len(rows)}


def print_fit(wins, steps, label):
    off = [s for s in steps if s.get("offlog")]
    steps = [s for s in steps if not s.get("offlog")]
    use = [agg([s]) for s in steps if s.get("reqs")]
    wagg = [agg([s for s in w["steps"] if s.get("reqs") and not s.get("offlog")]) for w in wins
            if any(s.get("reqs") and not s.get("offlog") for s in w["steps"])]

    print("\n%s%s %s%s" % (B, label, t("limit hypothesis fit", "한도 가설 적합"), X))
    print(t("%s%d change points (noisy, Δ%%p piles up at 1) / %d windows (less noise, fewer samples)%s",
            "%s변화점 %d개(잡음 큼, Δ%%p 가 1 에 몰림) / 창 %d개(잡음 작음, 표본 적음)%s")
          % (D, len(use), len(wagg), X))
    if off:
        print(t("%s%d change points suspected off-log · %.0f%%p dropped (chat / other devices)%s",
                "%s로그 밖 소비 의심 변화점 %d개 · %.0f%%p 는 뺐다 (채팅·다른 기기 몫)%s")
              % (Y, len(off), sum(s["dpct"] for s in off), X))
    if len(use) < 20:
        print(t("%sOnly %d change points. Do not trust this.%s",
                "%s변화점이 %d개뿐이다. 결과를 믿지 말 것.%s") % (R, len(use), X))
        if not use:
            return
    elif len(use) < 50:
        print(t("%sFewer than 50 change points. Direction only.%s",
                "%s변화점이 50개 미만이다. 방향만 참고할 것.%s") % (Y, X))

    fits = {}
    print("\n%s%s%s" % (D, t("%-14s %8s %7s %8s %7s"
                             % ("hypothesis", "step R²", "err%", "win R²", "err%"),
                             "가설            변화점 R²   오차%    창 R²   오차%"), X))
    for kind in HYPOTHESES:
        fs, fw = run_fit(use, kind), run_fit(wagg, kind)
        fits[kind] = (fs, fw)
        print("%-14s %8s %7s %8s %7s"
              % (HYP_LABEL[kind],
                 "%.3f" % fs["r2"] if fs else "-", "%.0f" % fs["rel"] if fs else "-",
                 "%.3f" % fw["r2"] if fw else "-", "%.0f" % fw["rel"] if fw else "-"))

    # ── 계수. 창 단위가 잡음이 적어 계수는 창 기준으로 읽는다.
    for kind in HYPOTHESES:
        fw = fits[kind][1]
        if not fw:
            continue
        print("\n%s%s%s  %s" % (B, HYP_LABEL[kind], X,
                                t("(from %d windows)", "(창 %d개 기준)") % fw["n"]))
        for lb, w in sorted(zip(fw["labels"], fw["b"]), key=lambda kv: -kv[1]):
            if lb in fw["dropped"]:
                continue
            if kind == "요청 수":
                print(t("      1 request = %.3f%%p   %.0f requests to 100%%",
                        "      요청 1건 = %.3f%%p   100%% 에 %.0f건") % (w, 100 / w) if w else "")
            elif kind in ("API환산 $", "모델별 $"):
                print(t("      %-10s %7.4f %%p/$   $%.2f per 1%%p",
                        "      %-10s %7.4f %%p/$   1%%p 당 $%.2f") % (lb, w, 1 / w))
            else:
                print(t("      %-11s %8.2f %%p/1M tok   %s tok per 1%%p",
                        "      %-10s %8.2f %%p/100만토큰   1%%p 당 %s토큰")
                      % (gl(lb), w, _human(1e6 / w)))
        for lb in fw["dropped"]:
            why = ""
            if kind == "토큰 종류별" and lb == "캐시읽기":
                why = t(" (an illusion hidden by model effects — see per model below)",
                        " (모델 효과에 가린 착시다. 아래 모델별을 볼 것)")
            print(t("      %s%-11s pinned to 0 — not separable from the others%s%s",
                    "      %s%-10s 0 으로 눌림 — 다른 항목과 갈라지지 않는다%s%s")
                  % (Y, gl(lb), why, X))

    # ── 모델별 토큰 무게. 교차모델 적합에서 캐시읽기가 0(또는 음수)으로 나오는 건
    #    모델 효과에 가린 착시다. 한 모델 안에서 보면 작지만 양수로 살아난다.
    print(t("\n%sper model%s  only change points where one model is 90%%+ of cost",
            "\n%s모델별%s  한 모델이 비용의 90%% 이상인 변화점만 모았다") % (B, X))
    names = [g[0] for g in GROUPS]
    models = sorted({m for r in use for m in r["cost_by_model"]},
                    key=lambda m: -sum(r["cost_by_model"].get(m, 0) for r in use))
    per_pct = {}                                 # 모델 → 1%p 당 $·토큰. 배율표에 쓴다.
    for m in models:
        pure = [s2 for s2 in steps
                if s2.get("reqs") and s2["cost"]
                and s2["cost_by_model"].get(m, 0) / s2["cost"] >= 0.9]
        if not pure:
            continue
        a = agg(pure)
        if a["dpct"] < 5:
            print(t("      %s%-10s pure steps %d, %.0f%%p — too few%s",
                    "      %s%-10s 순수 변화점 %d개, %.0f%%p — 부족%s")
                  % (D, m, len(pure), a["dpct"], X))
            continue
        print(t("      %s%-10s%s pure steps %3d · %3.0f%%p · $%.2f",
                "      %s%-10s%s 순수 변화점 %3d개 · %3.0f%%p · $%.2f")
              % (B, m, X, len(pure), a["dpct"], a["dpct"] and a["cost"]))
        print(t("        per 1%%p  $%.2f   output %s · fresh input %s · cache read %s tokens",
                "        1%%p 당  $%.2f   출력 %s · 신규입력 %s · 캐시읽기 %s 토큰")
              % (a["cost"] / a["dpct"], _human(a["출력"] / a["dpct"]),
                 _human(a["신규입력"] / a["dpct"]), _human(a["캐시읽기"] / a["dpct"])))
        byw = defaultdict(list)
        for s2 in pure:
            byw[s2["reset"]].append(s2)
        per_pct[m] = dict({n: a[n] / a["dpct"] for n in names},
                          cost=a["cost"] / a["dpct"], wins=len(byw))
        bins = [agg(v) for v in byw.values()]
        rowset, lvl = ((bins, t("window", "창")) if len(bins) >= 5
                       else ([agg([s2]) for s2 in pure], t("step", "변화점")))
        if len(rowset) < 4:
            continue
        Xm = [[r[n] / 1e6 for n in names] for r in rowset]
        b, dropped = nnls(Xm, [r["dpct"] for r in rowset], names)
        if not b:
            continue
        r2, mae, rel = fit_stats(Xm, [r["dpct"] for r in rowset], b)
        print(t("        per %s n=%d  R² %.3f err %.0f%%   %s%s",
                "        %s단위 n=%d  R² %.3f 오차 %.0f%%   %s%s")
              % (lvl, len(rowset), r2, rel,
                 " · ".join("%s %.1f" % (gl(n), v) for n, v in zip(names, b)),
                 ("   [0: %s]" % ",".join(gl(n) for n in dropped)) if dropped else ""))

    # ── 배율표. 1%p 올리는 데 든 양의 역비다. 토큰당으로 본다. 달러당은 세션 스타일
    #    (대화를 얼마나 길게 끄는가)이 섞여 모델 가중치가 아니다. 참고로만 붙인다.
    base = per_pct.get("opus-5")
    if base:
        print(t("\n%smultiplier%s  opus-5 = 1; higher burns the limit faster (per token)",
                "\n%s배율%s  opus-5 = 1, 클수록 한도가 빨리 닳는다 (토큰당)") % (B, X))
        for m, a in per_pct.items():
            if m == "opus-5":
                continue
            print(t("      %-10s output %.1fx · fresh input %.1fx · cache read %.1fx   %s(per $ %.1fx · %d windows%s)%s",
                    "      %-10s 출력 %.1f배 · 신규입력 %.1f배 · 캐시읽기 %.1f배   %s(달러당 %.1f배 · 창 %d개%s)%s")
                  % (m, *(base[n] / a[n] if a[n] else 0.0 for n in names), D,
                     base["cost"] / a["cost"] if a["cost"] else 0.0, a["wins"],
                     t(" — too few, shaky", " — 적어서 흔들린다") if a["wins"] < 8 else "", X))

    tot = {n: sum(r[n] for r in wagg) for n in names}
    s_all = sum(tot.values()) or 1
    print("\n%s%s%s" % (B, t("token share vs limit contribution", "토큰 비중 대 한도 기여"), X))
    print(t("%s      Cache read is %.1f%% of all tokens. Per token it weighs about 1/70 of",
            "%s      캐시읽기는 토큰의 %.1f%% 를 차지한다. 토큰 하나당 무게는 신규입력의")
          % (D, 100 * tot["캐시읽기"] / s_all))
    print(t("      fresh input — tiny, but there are 60x more of them, so the contributions",
            "      1/70 쯤으로 아주 작지만, 개수가 60배가 넘어 기여는 비슷해진다."))
    print(t("      end up similar. Money goes almost all to cache read; the limit is split three ways.%s",
            "      돈은 캐시읽기가 거의 다 먹지만, 한도는 셋이 나눠 먹는다.%s") % X)

def _human(v):
    if v >= 1e6:
        return t("%.1fM", "%.1f백만") % (v / 1e6)
    if v >= 1e3:
        return t("%.0fk", "%.0f천") % (v / 1e3)
    return "%.0f" % v


# ────────────────────────────────────────────── 6. 표

def _mix(cost_by_model, total):
    if not total:
        return "-"
    items = sorted(cost_by_model.items(), key=lambda kv: -kv[1])[:3]
    return " ".join("%s %d%%" % (m, round(100 * c / total)) for m, c in items if c > 0)


def print_windows(wins, label, bar_cost):
    tot_steps = sum(len(w["steps"]) for w in wins)
    print(t("\n%s%s limit burn per window%s   %d windows · %d change points\n",
            "\n%s%s 창별 한도 소진%s   창 %d개 · 변화점 %d개\n")
          % (B, label, X, len(wins), tot_steps))
    print("%s%s%s"
          % (D, t("%-12s  %-17s %-7s %4s %8s %8s %6s %5s %5s  %s"
                  % ("reset (UTC)", "observed span", " limit%", "Δ%", "Δ$ log",
                     "Δ$ bar", "$/1%p", "reqs", "sess", "models"),
                  "창 리셋(UTC)      관측 구간          한도%    Δ%   Δ$기록  Δ$상태바   $/1%p  요청  세션  모델 구성"), X))
    rates = []
    for w in wins:
        a = agg(w["steps"])
        # $/1%p 는 로그 밖 점프를 빼고 낸다. 넣으면 채팅 쓴 창이 싸 보인다.
        inside = agg([s for s in w["steps"] if not s.get("offlog")])
        per = "     -"
        if inside["dpct"]:
            rates.append(inside["cost"] / inside["dpct"])
            per = "%6.2f" % rates[-1]
        flag = " %s%s%s" % (Y, t("partial", "부분"), X) if w["p_first"] > 0 else ""
        if a["dpct"] > inside["dpct"]:
            flag += " %s%s %.0f%%p%s" % (Y, t("off-log", "로그밖"), a["dpct"] - inside["dpct"], X)
        if w is wins[-1]:
            flag += " %s%s%s" % (Y, t("open", "열림"), X)
        print("%s  %s→%s %3d→%-3d %4.0f %8.2f %8.2f %s %5d %5d  %s%s"
              % (utc(w["reset"], "%m-%d %H:%MZ"), utc(w["t_first"]), utc(w["t_last"], "%H:%M"),
                 w["p_first"], w["p_last"], a["dpct"], a["cost"], bar_cost.get(w["reset"], 0.0),
                 per, a["reqs"], w["sessions"], _mix(a["cost_by_model"], a["cost"]), flag))

    tot = agg([s for w in wins for s in w["steps"]])
    if tot["dpct"]:
        print(t("\n%s%.0f%%p total for API-equivalent $%.2f — average $%.2f per 1%%p%s",
                "\n%s전체 %.0f%%p 에 API환산 $%.2f — 평균 1%%p 당 $%.2f%s")
              % (B, tot["dpct"], tot["cost"], tot["cost"] / tot["dpct"], X))
        off = sum(s["dpct"] for w in wins for s in w["steps"] if s.get("offlog"))
        if off:
            print(t("%sof that %.0f%%p (%.0f%%) is suspected off-log — chat/app/other devices ate the same limit.%s",
                    "%s그중 %.0f%%p (%.0f%%) 는 로그 밖 소비 의심 — 채팅·앱·다른 기기가 같은 한도를 먹었다.%s")
                  % (Y, off, 100 * off / tot["dpct"], X))
            print(t("%s$/1%%p is computed with that share removed.%s",
                    "%s$/1%%p 는 그 몫을 빼고 냈다.%s") % (D, X))
    if len(rates) >= 4 and min(rates):
        print(t("%s$/1%%p per window ranges %.2f ~ %.2f (%.1fx) — the limit is not proportional to API-equivalent dollars.%s",
                "%s창별 $/1%%p 가 %.2f ~ %.2f (%.1f배 차이) — 한도는 API환산 달러에 비례하지 않는다.%s")
              % (D, min(rates), max(rates), max(rates) / min(rates), X))


def print_steps(steps):
    print(t("\n%s%d change points%s   requests finishing in (tf, t1] are summed\n",
            "\n%s변화점 %d개%s   (tf, t1] 구간에 끝난 요청을 합산\n") % (B, len(steps), X))
    print("%s%s%s"
          % (D, t("%-12s  %-20s %6s %-7s %3s %4s %8s %6s %8s %8s  %s"
                  % ("reset", "attribution tf → t1", "len", " limit%", "Δ%", "reqs",
                     "API$", "out k", "fresh k", "cacheR M", "models"),
                  "창 리셋      귀속 구간 tf → t1     길이  한도%   Δ%  요청     API$  출력k 신규입력k 캐시읽기M  모델"), X))
    for s in steps:
        a = agg([s])
        print("%s  %s→%s %5.0fs %3d→%-3d %3d %4d %8.3f %6.0f %8.0f %8.1f  %s%s"
              % (utc(s["reset"], "%m-%d %H:%MZ"), utc(s["tf"]), utc(s["t1"], "%H:%M:%S"),
                 s["t1"] - s["tf"], s["p0"], s["p1"], s["dpct"], s["reqs"], s["cost"],
                 a["출력"] / 1e3, a["신규입력"] / 1e3, a["캐시읽기"] / 1e6,
                 _mix(s["cost_by_model"], s["cost"]),
                 "  %s%s%s" % (Y, t("off-log", "로그밖"), X) if s.get("offlog") else ""))


def write_csv(steps, path):
    models = sorted({m for s in steps for m in s.get("cost_by_model", {})})
    cols = (["window_reset", "window_reset_utc", "tf_utc", "t1_utc", "span_sec",
             "pct_from", "pct_to", "d_pct", "requests", "cost_usd", "offlog"] + TOK
            + ["cost_" + m for m in models])
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(cols) + "\n")
        for s in steps:
            row = [s["reset"], utc(s["reset"], "%Y-%m-%dT%H:%M:%SZ"),
                   utc(s["tf"], "%Y-%m-%dT%H:%M:%SZ"), utc(s["t1"], "%Y-%m-%dT%H:%M:%SZ"),
                   "%.0f" % (s["t1"] - s["tf"]), s["p0"], s["p1"], s["dpct"],
                   s.get("reqs", 0), "%.6f" % s.get("cost", 0.0), int(bool(s.get("offlog")))]
            row += [s.get(k, 0) for k in TOK]
            row += ["%.6f" % s.get("cost_by_model", {}).get(m, 0.0) for m in models]
            f.write(",".join(str(v) for v in row) + "\n")
    print(t("%d change points → %s", "변화점 %d개 → %s") % (len(steps), path))


# ────────────────────────────────────────────── 7. main

def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
            return sys.argv[i + 1]
    return default


def main():
    log_path = opt("--log", DEFAULT_LOG)
    weekly = "--weekly" in sys.argv
    field = "seven_day" if weekly else "five_hour"
    label = t("7-day", "7일") if weekly else t("5-hour", "5시간")
    win_len = 7 * 86400 if weekly else 5 * 3600

    if not os.path.exists(log_path):
        print(t("%sNo limit gauge samples: %s%s",
                "%s한도 게이지 표본이 없다: %s%s") % (R, log_path, X))
        print(t("\nClaude Code prints the limit burn on screen and throws it away. A collector",
                "\n클로드코드는 한도 소진율을 화면에 찍고 버린다. 상태바에 수집기를 걸어야"))
        print(t("has to sit on your status line. Put this in %s~/.claude/settings.json%s:\n"
                '    "statusLine": {"type": "command", "command": "claudeusage statusline --print"}\n',
                "쌓인다. %s~/.claude/settings.json%s 에 이걸 넣는다.\n"
                '    "statusLine": {"type": "command", "command": "claudeusage statusline --print"}\n')
              % (B, X))
        print(t("%sWhat works before samples pile up:%s",
                "%s표본이 쌓이기 전에도 되는 것:%s") % (D, X))
        print(t("%s  claudeusage value --all    surviving lines · waste%s",
                "%s  claudeusage value --all    살아남은 줄·낭비%s") % (D, X))
        print(t("%s  claudeusage usage          limit now + per product%s",
                "%s  claudeusage usage          지금 한도 + 제품별 분해%s") % (D, X))
        return 1

    rows, skip, n_raw = load_samples(log_path, field)
    if not rows:
        print(t("%sNo usable samples.%s", "%s쓸 수 있는 표본이 없다.%s") % (R, X))
        return 1

    wins, steps = windows_and_steps(rows, win_len)
    reqs = request_index(min(w["reset"] - win_len for w in wins), rows[-1]["t"], verbose=True)
    attribute(steps, reqs)

    if "--csv" in sys.argv:
        write_csv(steps, opt("--csv", paths.data_file("limit-steps.csv")))
        return 0

    print(t("%s%d sample rows, %d used · %d windows · %d change points%s",
            "%s표본 %d줄 중 %d줄 사용 · 창 %d개 · 변화점 %d개%s")
          % (D, n_raw, len(rows), len(wins), len(steps), X))
    if skip:
        print("%s  %s %s%s" % (D, t("excluded:", "제외:"),
                               ", ".join("%s %d" % kv for kv in sorted(skip.items())), X))
    print(t("%s  %d samples went down inside a window — stale snapshots, read as running max and ignored%s",
            "%s  창 안에서 값이 내려간 표본 %d건 — 오래된 스냅샷이라 누적최대로 읽어 무시함%s")
          % (D, sum(w["stale"] for w in wins), X))

    if "--steps" in sys.argv:
        print_steps(steps)
    elif "--fit" in sys.argv:
        print_fit(wins, steps, label)
    else:
        print_windows(wins, label, cost_by_window(rows, win_len))
        print(t("\n%s--steps all change points · --fit hypothesis fit · --weekly 7-day window · --csv export%s",
                "\n%s--steps 변화점 전부 · --fit 가설 적합 · --weekly 7일 창 · --csv 내보내기%s") % (D, X))
    return 0


if __name__ == "__main__":
    sys.exit(main())
