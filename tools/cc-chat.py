#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-chat.py — 한도 중에 "클로드코드가 아닌 것"(주로 채팅)이 먹은 몫을 잰다.

왜 필요한가. 한도는 계정 전체에 걸린다. claude.ai 채팅·앱도 같은 5시간/주간 한도를
먹는데, 로컬에는 클로드코드 기록만 남는다. 그래서 로그만 읽는 도구(ccusage 포함,
우리 cc-value.py 도)는 구독 소진을 구조적으로 덜 센다. 2026-09-14 실측에서 5시간
한도가 로컬 요청 8건($1.75) 동안 15→53% 로 뛰었고, 사용자가 그 시간에 채팅을 썼다고
확인했다(README 한도 절 5번).

재는 법은 뺄셈이다.

    채팅 몫 = 게이지가 실제로 오른 양 − 로컬 로그로 설명되는 양

설명되는 양은 cc-limit.py 가 적합한 모델별 가중치(%p/$)로 계산한다. 그 가중치는
로그 밖 소비를 뺀 창들로만 적합한 것이라 순환논리가 아니다.

소스가 셋이다. 각자 구멍이 달라서 겹쳐야 메워진다.

    상태바 표본   data/ratelimit-log.jsonl        클로드코드가 떠 있는 동안만
    데스크톱 앱   plan-usage-history.json         앱이 떠 있는 동안만, 15분 간격
    엔드포인트    seven_day_breakdown (cc-usage)  제품별 정답. 주간 창 하나치만

앞의 둘은 게이지 숫자라 "얼마나 올랐나"만 알려주고 누가 올렸는지는 안 알려준다.
그래서 뺄셈으로 추정한다. 셋째가 정답이라 추정이 맞는지 채점할 수 있다.

    python3 tools/cc-chat.py             주간 요약 (정답과 대조)
    python3 tools/cc-chat.py --windows   5시간 창별 추정
    python3 tools/cc-chat.py --sources   소스별 커버리지
"""

import os, sys, json, time, importlib.util
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location("cclimit", os.path.join(HERE, "cc-limit.py"))
L = importlib.util.module_from_spec(_spec)
_argv, sys.argv = sys.argv, ["cc-chat"]          # cc-limit 은 import 시 argv 를 안 읽지만 안전하게
_spec.loader.exec_module(L)
sys.argv = _argv

B, D, R, Y, G, X = "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[0m"

DESKTOP = os.path.expanduser(
    "~/Library/Application Support/Claude/plan-usage-history.json")
USAGE_LOG = os.path.join(ROOT, "data", "usage-log.jsonl")

WIN = 5 * 3600
# 주간 창 경계. 실측한 seven_day.resets_at 이 09-08·09-15·09-22 전부 화요일 15:00Z 였다.
# 8월처럼 리셋 기록이 없는 기간은 이 규칙으로 되짚는다.
WEEK_ANCHOR = 1789484400.0                       # 2026-09-15T15:00:00Z
WEEK = 7 * 86400


def week_of(t):
    return WEEK_ANCHOR + WEEK * ((t - WEEK_ANCHOR) // WEEK)


# ────────────────────────────────────────────── 1. 게이지 표본 모으기

def desktop_samples():
    """데스크톱 앱이 남긴 한도 표본. {t, u:{fh, sd}} 가 15분 간격으로 쌓인다.

    상태바 수집기(9/1~)보다 앞선 8/17 부터 있고, 무엇보다 **클로드코드를 안 켠 시간**을
    덮는다. 채팅만 쓴 시간대는 상태바 표본에 아예 안 잡히므로 이게 없으면 안 보인다.
    앱이 꺼져 있으면 안 쌓이는 건 같으므로 이쪽도 완전하지 않다.
    """
    if not os.path.exists(DESKTOP):
        return []
    try:
        d = json.load(open(DESKTOP, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    out = []
    for s in d.get("samples") or []:
        u = s.get("u") or {}
        t, p = s.get("t"), u.get("fh")
        if isinstance(t, (int, float)) and isinstance(p, (int, float)):
            out.append({"t": t / 1000.0, "sid": "desktop", "pct": int(round(p)),
                        "reset": None, "cost": None, "src": "desktop"})
    out.sort(key=lambda r: r["t"])
    return out


def merged_samples():
    """상태바 + 데스크톱. 상태바에는 리셋 시각이 있고 데스크톱에는 없다.

    데스크톱 표본은 리셋 시각을 아는 창에 시간으로 끼워 넣는다. 그 밖(주로 8월)은
    창 경계를 모르므로 '값이 내려가면 새 창'으로 되짚는다. 게이지는 창이 바뀔 때만
    내려가기 때문이다(창 안의 하락은 오래된 스냅샷이고, 그건 상태바 얘기라 여기선 없다).
    """
    bar, skip, n_raw = L.load_samples(L.DEFAULT_LOG, "five_hour")
    for r in bar:
        r["src"] = "bar"
    desk = desktop_samples()

    known = sorted({r["reset"] for r in bar})
    rows = list(bar)
    loose = []
    for r in desk:
        hit = None
        for reset in known:
            if reset - WIN < r["t"] <= reset:
                hit = reset
                break
        if hit is not None:
            rows.append(dict(r, reset=hit))
        else:
            loose.append(r)

    # 남은 표본은 창을 되짚는다. 리셋 시각을 모르므로 창 id 는 음수 일련번호로 둔다.
    loose.sort(key=lambda r: r["t"])
    gid, prev, start = 0, None, None
    for r in loose:
        # 새 창으로 치는 경우 셋. 값이 내려갔다(리셋됐다), 표본이 한참 끊겼다,
        # 그리고 5시간을 넘겼다 — 창은 5시간보다 길 수 없으므로 안 끊으면 두 창이
        # 한 덩어리가 된다(게이지가 0 인 채로 창이 바뀌면 하락이 안 보인다).
        if (prev is None or r["pct"] < prev["pct"] or r["t"] - prev["t"] > WIN
                or r["t"] - start > WIN):
            gid += 1
            start = r["t"]
        rows.append(dict(r, reset=-gid))
        prev = r
    rows.sort(key=lambda r: (r["t"], r["sid"]))
    return rows, {"bar": len(bar), "desk_in": len(desk) - len(loose),
                  "desk_out": len(loose), "skip": skip, "raw": n_raw}


def windows_of(rows):
    """창별로 묶어 '얼마나 올랐나'와 '언제부터 언제까지'를 낸다.

    귀속 구간은 cc-limit.py 와 같은 규칙이다. 게이지는 늦게 관측되므로 **직전 창을
    마지막으로 본 시각부터** 이번 창을 마지막으로 본 시각까지의 요청이 이번 상승의
    원인이다. 그래야 창 사이에 낀 요청이 안 샌다.
    """
    by = defaultdict(list)
    for r in rows:
        by[r["reset"]].append(r)

    wins = []
    for reset, ws in sorted(by.items(), key=lambda kv: kv[1][0]["t"]):
        ws.sort(key=lambda r: r["t"])
        cur, top = ws[0]["pct"], ws[0]["pct"]
        t_top = ws[0]["t"]
        for r in ws:
            if r["pct"] > top:
                top, t_top = r["pct"], r["t"]
        wins.append({
            "reset": reset, "inferred": reset < 0,
            "t_first": ws[0]["t"], "t_last": ws[-1]["t"], "t_top": t_top,
            "p_first": ws[0]["pct"], "p_top": top, "rise": top - ws[0]["pct"],
            "srcs": {r["src"] for r in ws}, "n": len(ws),
        })
    wins.sort(key=lambda w: w["t_first"])
    prev_end = None
    for w in wins:
        if not w["inferred"] and w["p_first"] == 0:
            # 0 부터 봤으니 창이 열린 시각부터 센다. 그 앞의 요청은 이전 창 몫이다
            # (게이지가 리셋됐으므로). cc-limit.py 와 같은 규칙.
            w["tf"] = w["reset"] - WIN
        else:
            # 창이 언제 열렸는지 모른다. 직전 관측 이후로만 세되, 창 길이보다 더
            # 거슬러 올라가지는 않는다. 안 그러면 안 본 시간의 요청까지 다 얹힌다.
            back = max(w["t_first"] - WIN, prev_end if prev_end is not None else w["t_first"] - WIN)
            w["tf"] = min(back, w["t_first"])
            if not w["inferred"]:
                # 리셋 시각을 아는 창이면 창 시작 앞으로는 못 간다. 중간부터 본 창
                # (p_first > 0)이 여기 걸린다 — 안 조이면 구간이 5시간을 넘는다.
                w["tf"] = max(w["tf"], w["reset"] - WIN)
        # 어떤 경우에도 창 하나가 5시간을 넘게 먹을 수는 없다.
        w["tf"] = max(w["tf"], w["t_top"] - WIN)
        prev_end = w["t_top"]
    return wins


# ────────────────────────────────────────────── 2. 로컬 로그로 설명되는 몫

def weights():
    """cc-limit.py 와 같은 방식으로 모델별 %p/$ 가중치를 적합한다.

    '모델별 $' 가설이 창 단위 R² 0.95 로 가장 잘 맞는다. 로그 밖 소비로 판정된
    변화점은 빼고 적합한다 — 그걸 넣으면 채팅 몫이 로컬 비용에 얹혀 가중치가 부풀고,
    그러면 뺄셈 결과가 0 에 가까워진다(스스로를 못 보게 된다).
    """
    rows, _, _ = L.load_samples(L.DEFAULT_LOG, "five_hour")
    wins, steps = L.windows_and_steps(rows, WIN)
    reqs = L.request_index(min(w["reset"] - WIN for w in wins), rows[-1]["t"])
    L.attribute(steps, reqs)
    wagg = [L.agg([s for s in w["steps"] if s.get("reqs") and not s.get("offlog")])
            for w in wins
            if any(s.get("reqs") and not s.get("offlog") for s in w["steps"])]
    fit = L.run_fit(wagg, "모델별 $")
    if not fit:
        raise SystemExit("%s가중치 적합에 실패했다.%s" % (R, X))
    return dict(zip(fit["labels"], fit["b"])), fit


def predict(cost_by_model, w):
    """로컬 로그가 설명하는 %p. 가중치에 없는 모델은 전체 평균으로 친다."""
    fallback = sum(w.values()) / len(w) if w else 0.0
    return sum(c * w.get(m, fallback) for m, c in cost_by_model.items())


def bar_cost_by_window():
    """상태바가 세는 창별 비용. 로그 합계보다 완전하다.

    로그로 더한 비용은 상태바 누계보다 약 6~9% 적다(README 함정 7 — 로그에 안 남는
    백그라운드 호출). 그대로 두면 '설명되는 몫'이 그만큼 낮게 나오고, 차액이 전부
    채팅으로 넘어간다. 창마다 비율을 재서 되돌린다.
    """
    rows, _, _ = L.load_samples(L.DEFAULT_LOG, "five_hour")
    return L.cost_by_window(rows, WIN)


def measure(wins, weights_by_model, bar_cost=None):
    reqs = L.request_index(min(w["tf"] for w in wins) - 1, max(w["t_top"] for w in wins))
    times = [r["t"] for r in reqs]
    import bisect
    for w in wins:
        chunk = reqs[bisect.bisect_right(times, w["tf"]):bisect.bisect_right(times, w["t_top"])]
        cm = defaultdict(float)
        for r in chunk:
            cm[r["mk"]] += r["cost"]
        w["reqs"] = len(chunk)
        w["cost"] = sum(r["cost"] for r in chunk)
        w["cost_by_model"] = dict(cm)
        # 로그 누락 보정. 상태바가 더 많이 봤으면 그 비율만큼 올린다. 모델 구성은
        # 로그 것을 그대로 쓴다(상태바는 모델을 안 쪼개 준다). 1.5배는 안전장치다.
        w["gross"] = 1.0
        bc = (bar_cost or {}).get(w["reset"], 0.0)
        if w["cost"] > 1.0 and bc > w["cost"]:
            w["gross"] = min(1.5, bc / w["cost"])
        w["pred"] = predict(cm, weights_by_model) * w["gross"]
        w["resid"] = w["rise"] - w["pred"]
        # 관측이 온전한 창만 뺄셈이 뜻을 가진다. 아니면 상승분은 낮게, 설명분은 높게
        # 나와 뺄셈이 통째로 기운다. 8월(되짚은 창)이 전부 여기 걸린다.
        #   · 창 경계를 알아야 한다(리셋 시각). 되짚은 창은 앱이 켜진 토막이라 경계가 아니다.
        #   · 0 부터 봤어야 한다. 중간부터 보면 그 앞의 상승을 못 센다.
        w["clean"] = not w["inferred"] and w["p_first"] == 0
        # 음수 잔차는 "로컬 로그가 상승분보다 많다"는 뜻이라 채팅이 아니라 관측 잡음이다.
        # 합계에서는 그대로 두고(그래야 편향이 안 생긴다) 창 단위로만 0 에서 자른다.
        w["chat"] = max(0.0, w["resid"])
    return wins


# ────────────────────────────────────────────── 3. 엔드포인트 정답

def truth_rows():
    """cc-usage.py --log 가 쌓은 제품별 분해. 주간 창마다 마지막 것을 쓴다."""
    if not os.path.exists(USAGE_LOG):
        return {}
    out = {}
    for line in open(USAGE_LOG, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        bd = r.get("breakdown")
        if not bd or not bd.get("rows"):
            continue
        wk = bd.get("window_started_at")
        weekly = next((l.get("percent") for l in r.get("limits") or []
                       if l.get("kind") == "weekly_all"), None)
        if wk is None:
            continue
        out[week_of(wk + 60)] = {"rows": bd["rows"], "weekly": weekly,
                                 "at": L.to_epoch(r.get("fetched_at"))}
    return out


def truth_chat_pct(t):
    """그 주의 정답에서 클로드코드가 아닌 비중(%)."""
    row = t["rows"]
    tot = sum(x.get("percent") or 0 for x in row)
    if not tot:
        return None
    cc = sum(x.get("percent") or 0 for x in row if x.get("key") == "claude_code")
    return 100.0 * (tot - cc) / tot


# ────────────────────────────────────────────── 4. 출력

def print_sources(info, wins):
    print("\n%s게이지 표본 소스%s" % (B, X))
    print("      상태바        %5d개   클로드코드가 떠 있는 동안" % info["bar"])
    print("      데스크톱 앱    %5d개   그중 %d개는 상태바가 이미 본 창, %d개는 새 구간"
          % (info["desk_in"] + info["desk_out"], info["desk_in"], info["desk_out"]))
    known = [w for w in wins if not w["inferred"]]
    inf = [w for w in wins if w["inferred"]]
    print("\n%s창%s" % (B, X))
    print("      리셋 시각을 아는 창  %3d개  %s ~ %s"
          % (len(known), L.utc(known[0]["t_first"]) if known else "-",
             L.utc(known[-1]["t_last"]) if known else "-"))
    print("      되짚은 창           %3d개  %s ~ %s   %s(8월 등, 경계가 거칠다)%s"
          % (len(inf), L.utc(inf[0]["t_first"]) if inf else "-",
             L.utc(inf[-1]["t_last"]) if inf else "-", D, X))
    only = [w for w in wins if w["srcs"] == {"desktop"}]
    print("      데스크톱만 본 창     %3d개  %s(상태바만 보면 안 보이던 구간)%s"
          % (len(only), D, X))


def print_windows(wins):
    print("\n%s5시간 창별 — 채팅 몫 추정%s   상승분에서 로컬 로그로 설명되는 몫을 뺀다\n" % (B, X))
    print("%s창 관측 구간            상승%%p  설명%%p  채팅%%p  요청   API$  소스%s" % (D, X))
    for w in wins:
        if w["rise"] <= 0:
            continue
        mark = ""
        if w["chat"] >= 5:
            mark = " %s채팅 의심%s" % (Y, X)
        if not w["reqs"] and w["rise"] >= 3:
            mark = " %s로컬 요청 0%s" % (R, X)
        print("%s→%s %5.0f %7.1f %7.1f %5d %7.2f  %s%s"
              % (L.utc(w["t_first"]), L.utc(w["t_top"], "%H:%M"),
                 w["rise"], w["pred"], w["chat"], w["reqs"], w["cost"],
                 "+".join(sorted(w["srcs"])), mark))


def print_weeks(wins, truth):
    by = defaultdict(lambda: {"rise": 0.0, "pred": 0.0, "chat": 0.0, "resid": 0.0,
                              "wins": 0, "skipped": 0, "skip_rise": 0.0})
    for w in wins:
        if w["rise"] <= 0:
            continue
        a = by[week_of(w["t_top"])]
        if not w["clean"]:
            a["skipped"] += 1
            a["skip_rise"] += w["rise"]
            continue
        for k in ("rise", "pred", "chat", "resid"):
            a[k] += w[k]
        a["wins"] += 1

    print("\n%s주간 — 채팅이 먹은 한도%s   관측이 온전한 5시간 창만 센다\n" % (B, X))
    print("%s주 시작(UTC)   창수  상승%%p  설명%%p  채팅%%p  추정 비중   정답(엔드포인트)   뺀 창%s" % (D, X))
    for wk in sorted(by):
        a = by[wk]
        est = 100.0 * a["chat"] / a["rise"] if a["rise"] else 0.0
        t = truth.get(wk)
        tv = "-"
        if t:
            tc = truth_chat_pct(t)
            if tc is not None:
                gap = est - tc
                col = G if abs(gap) <= 5 else Y
                tv = "%s%.0f%%  (차이 %+.0f%%p)%s" % (col, tc, gap, X)
        skip = ("%s%d개 %.0f%%p%s" % (D, a["skipped"], a["skip_rise"], X)) if a["skipped"] else ""
        print("%s  %4d %7.0f %7.0f %7.0f %9.0f%%   %-28s %s"
              % (L.utc(wk, "%m-%d %H:%MZ"), a["wins"], a["rise"], a["pred"], a["chat"],
                 est, tv, skip))

    tot = {k: sum(a[k] for a in by.values()) for k in ("rise", "pred", "chat", "resid")}
    if tot["rise"]:
        print("\n%s온전한 창 %.0f%%p 중 채팅 몫 추정 %.0f%%p (%.0f%%)%s"
              % (B, tot["rise"], tot["chat"], 100 * tot["chat"] / tot["rise"], X))
        print("%s창 단위로 0 에서 자르기 전의 잔차 합은 %+.0f%%p — 이만큼이 추정의 편향이다."
              " 가중치 적합 오차가 19%% 라 창 하나하나는 그만큼 흔들린다.%s"
              % (D, tot["resid"], X))
    pure = [w for w in wins if w["rise"] >= 2 and not w["reqs"]]
    if pure:
        print("\n%s로컬 요청이 0 인데 게이지가 오른 창 %d개 · %.0f%%p — 추정이 아니라 관측이다%s"
              % (B, len(pure), sum(w["rise"] for w in pure), X))
        for w in pure[:8]:
            print("      %s→%s  +%d%%p  %s"
                  % (L.utc(w["t_first"]), L.utc(w["t_top"], "%H:%M"), w["rise"],
                     "+".join(sorted(w["srcs"]))))
    if not truth:
        print("%s정답 칸이 비어 있다. `python3 tools/cc-usage.py --log` 를 가끔 돌리면 채워진다."
              " 주간 창이 지나가면 그 주 정답은 다시 못 받는다.%s" % (Y, X))


def main():
    rows, info = merged_samples()
    if not rows:
        print("%s게이지 표본이 없다.%s" % (R, X))
        return 1
    wins = windows_of(rows)
    wb, fit = weights()
    measure(wins, wb, bar_cost_by_window())

    print("%s표본 %d개 · 창 %d개 · 가중치는 창 %d개로 적합 (R² %.3f, 오차 %.0f%%)%s"
          % (D, len(rows), len(wins), fit["n"], fit["r2"], fit["rel"], X))
    print("%s  %s%s" % (D, " · ".join("%s %.3f%%p/$" % kv for kv in
                                      sorted(wb.items(), key=lambda kv: -kv[1])), X))

    if "--sources" in sys.argv:
        print_sources(info, wins)
    elif "--windows" in sys.argv:
        print_windows(wins)
    else:
        print_weeks(wins, truth_rows())
        print("\n%s--windows 창별 · --sources 소스별 커버리지%s" % (D, X))
    return 0


if __name__ == "__main__":
    sys.exit(main())
