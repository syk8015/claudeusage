#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_prune.py — prune 이 한도 역산 결과를 바꾸지 않는지 실데이터로 잰다.

    python3 tools/verify_prune.py <표본 복사본> [--older-than 0] [--work 폴더] [--md 결과.md]

복사본을 두 벌 만든다(정리 전·후). 후 쪽에만 `claudeusage prune --apply` 를 돌리고
두 쪽을 비교한다.

  1. 명령 출력 전체 — limit·limit --steps·limit --fit(5시간·7일)·chat·check … 글자 단위.
     정리 전을 두 번 돌려 **원래 흔들리는 명령**(살아 있는 대화 기록을 읽는 것)은 가려낸다.
  2. limit --fit 이 계산하는 값 — 항목별 정리 전/후/차이. print_fit 과 같은 식이다.
  3. 대조군 — 표본을 시간으로 솎은 파일(세션마다 10분에 한 줄). 같은 잣대가 **달라짐을
     잡아내는지** 본다. 이게 못 잡으면 1·2 의 "차이 없음"은 아무것도 증명하지 못한다.

넘긴 파일은 읽기만 한다. 그래도 진짜 표본(data/ratelimit-log.jsonl)을 직접 넘기지 말고
복사본을 넘길 것. 수집기가 계속 덧붙이므로 두 번 읽으면 값이 다르다.
"""

import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from collections import defaultdict

os.environ["CLAUDEUSAGE_LANG"] = "ko"                   # 항목 이름을 한국어로 고정한다
SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, SRC)
from claudeusage import cc_limit as L                    # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")

COMMANDS = [["limit"], ["limit", "--steps"], ["limit", "--fit"],
            ["limit", "--weekly"], ["limit", "--weekly", "--steps"], ["limit", "--weekly", "--fit"],
            ["chat"], ["chat", "--windows"], ["check"]]

# 본문 표에 올릴 항목. 나머지는 부록에 전부 있다.
HEADLINE = ["변화점", "창", "로그 밖 소비 의심 변화점",
            "[요청 수] 창 계수 요청", "[API환산 $] 창 계수 $",
            "[토큰 종류별] 창 R²", "[토큰 종류별] 창 오차%",
            "[토큰 종류별] 창 계수 출력", "[토큰 종류별] 창 계수 신규입력", "[토큰 종류별] 창 계수 캐시읽기",
            "[모델별 $] 창 R²", "배율 fable-5-1 출력", "배율 fable-5 출력", "배율 opus-4-8 출력",
            "배율 fable-5-1 캐시읽기"]


# ───────────────────────────────────────── 1. 명령을 그대로 돌려 출력 비교

def run(args, data_dir):
    env = dict(os.environ, PYTHONPATH=SRC, CLAUDEUSAGE_DATA=data_dir, CLAUDEUSAGE_LANG="ko")
    p = subprocess.run([sys.executable, "-m", "claudeusage.cli"] + args, env=env,
                       capture_output=True, text=True)
    return p.returncode, ANSI.sub("", p.stdout)


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def compare_commands(before, after):
    rows = []
    for args in COMMANDS:
        b1, b2, a = run(args, before), run(args, before), run(args, after)
        noisy = b1 != b2                      # 정리와 무관하게 돌 때마다 다른 명령
        same = b1 == a
        nd = sum(1 for x, y in zip(b1[1].splitlines(), a[1].splitlines()) if x != y) \
            + abs(len(b1[1].splitlines()) - len(a[1].splitlines()))
        rows.append({"cmd": " ".join(args), "lines": len(b1[1].splitlines()),
                     "sha_b": sha(b1[1]), "sha_a": sha(a[1]), "rc_b": b1[0], "rc_a": a[0],
                     "same": same, "noisy": noisy, "ndiff": nd})
    return rows


# ───────────────────────────────────────── 2. limit --fit 이 계산하는 값

def fit_items(tag, wins, steps):
    """print_fit 이 화면에 찍기 전에 계산하는 값. 식은 그 함수와 같다."""
    out = []
    A = lambda name, v: out.append((tag, name, v))
    off = [s for s in steps if s.get("offlog")]
    steps = [s for s in steps if not s.get("offlog")]
    use = [L.agg([s]) for s in steps if s.get("reqs")]
    wagg = [L.agg([s for s in w["steps"] if s.get("reqs") and not s.get("offlog")]) for w in wins
            if any(s.get("reqs") and not s.get("offlog") for s in w["steps"])]
    A("적합에 쓴 변화점", len(use))
    A("적합에 쓴 창", len(wagg))
    A("로그 밖으로 뺀 %p", sum(s["dpct"] for s in off))
    for kind in L.HYPOTHESES:
        fs, fw = L.run_fit(use, kind), L.run_fit(wagg, kind)
        for lvl, f in (("변화점", fs), ("창", fw)):
            if f:
                A("[%s] %s R²" % (kind, lvl), f["r2"])
                A("[%s] %s 오차%%" % (kind, lvl), f["rel"])
        if fw:
            for lb, w in zip(fw["labels"], fw["b"]):
                A("[%s] 창 계수 %s" % (kind, lb), w)

    names = [g[0] for g in L.GROUPS]
    models = sorted({m for r in use for m in r["cost_by_model"]},
                    key=lambda m: -sum(r["cost_by_model"].get(m, 0) for r in use))
    per_pct = {}
    for m in models:
        pure = [s for s in steps if s.get("reqs") and s["cost"]
                and s["cost_by_model"].get(m, 0) / s["cost"] >= 0.9]
        if not pure:
            continue
        a = L.agg(pure)
        if a["dpct"] < 5:
            continue
        per_pct[m] = dict({n: a[n] / a["dpct"] for n in names}, cost=a["cost"] / a["dpct"])
        A("[모델 %s] 순수 변화점" % m, len(pure))
        A("[모델 %s] Δ%%p 합" % m, a["dpct"])
        A("[모델 %s] 1%%p 당 $" % m, a["cost"] / a["dpct"])
        for n in names:
            A("[모델 %s] 1%%p 당 %s 토큰" % (m, n), a[n] / a["dpct"])
        byw = defaultdict(list)
        for s in pure:
            byw[s["reset"]].append(s)
        bins = [L.agg(v) for v in byw.values()]
        rowset = bins if len(bins) >= 5 else [L.agg([s]) for s in pure]
        if len(rowset) >= 4:
            Xm = [[r[n] / 1e6 for n in names] for r in rowset]
            y = [r["dpct"] for r in rowset]
            b, _ = L.nnls(Xm, y, names)
            if b:
                r2, _, rel = L.fit_stats(Xm, y, b)
                A("[모델 %s] 창 단위 R²" % m, r2)
                A("[모델 %s] 창 단위 오차%%" % m, rel)
                for n, v in zip(names, b):
                    A("[모델 %s] 창 단위 계수 %s" % (m, n), v)
    base = per_pct.get("opus-5")
    if base:
        for m, a in per_pct.items():
            if m != "opus-5":
                for n in names:
                    A("배율 %s %s" % (m, n), base[n] / a[n] if a[n] else 0.0)
                A("배율 %s 달러당" % m, base["cost"] / a["cost"] if a["cost"] else 0.0)
    return out


def measure(log, cache):
    """(창 종류, 항목, 값) 목록. cache 는 대화 기록 색인. 같은 구간이면 한 번만 읽는다."""
    items = []
    for field, wl, tag in (("five_hour", 5 * 3600, "5시간"), ("seven_day", 7 * 86400, "7일")):
        rows, skip, n_raw = L.load_samples(log, field)
        wins, steps = L.windows_and_steps(rows, wl)
        key = (min(w["reset"] - wl for w in wins), rows[-1]["t"])
        if key not in cache:
            cache[key] = L.request_index(*key)
        L.attribute(steps, cache[key])
        bar = L.cost_by_window(rows, wl)
        A = lambda name, v: items.append((tag, name, v))
        A("파일의 줄 수", n_raw)
        A("쓴 줄 수", len(rows))
        for k, v in sorted(skip.items()):
            A("제외: " + k, v)
        A("창", len(wins))
        A("변화점", len(steps))
        A("창 안에서 내려간 표본(stale)", sum(w["stale"] for w in wins))
        A("변화점 Δ%p 합", sum(s["dpct"] for s in steps))
        A("귀속된 요청 수", sum(s["reqs"] for s in steps))
        A("귀속된 API환산 $", sum(s["cost"] for s in steps))
        A("로그 밖 소비 의심 변화점", sum(1 for s in steps if s["offlog"]))
        A("창별 상태바 비용(창마다 비교)", dict(bar))
        items += fit_items(tag, wins, steps)
    return items


def fmt(v):
    if isinstance(v, dict):
        v = sum(v.values())
    return format(v, ",") if isinstance(v, int) else "%.6g" % v


def gap(a, b):
    if isinstance(a, dict):
        return max([abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b)] or [0.0])
    return b - a


def fmt_gap(g):
    return "0" if g == 0 else "%.3g" % g


def compare_items(ia, ib):
    """항목 이름이 어긋나면 그 자체가 차이다."""
    da, db = {(t, n): v for t, n, v in ia}, {(t, n): v for t, n, v in ib}
    out = []
    for k in list(da) + [k for k in db if k not in da]:
        if k in da and k in db:
            out.append((k, da[k], db[k], gap(da[k], db[k])))
        else:
            out.append((k, da.get(k, "없음"), db.get(k, "없음"), float("nan")))
    return out


# ───────────────────────────────────────── 3. 대조군: 순진하게 솎은 표본

def downsample(src, dst, minutes):
    last = {}
    with open(src, "rb") as f, open(dst, "wb") as out:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                out.write(raw)
                continue
            ts, sid = L.to_epoch(e.get("logged_at")), e.get("session_id")
            if ts is not None and sid in last and ts - last[sid] < minutes * 60:
                continue
            if ts is not None:
                last[sid] = ts
            out.write(raw)


# ───────────────────────────────────────── 표 만들기

def table(rows):
    lines = ["| 창 | 항목 | 정리 전 | 정리 후 | 차이 |", "|---|---|---:|---:|---:|"]
    for (tag, name), a, b, g in rows:
        lines.append("| %s | %s | %s | %s | %s |" % (tag, name, fmt(a), fmt(b),
                                                     "다름" if g != g else fmt_gap(g)))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="prune 이 limit --fit 을 바꾸지 않는지 잰다")
    ap.add_argument("copy", help="표본 복사본 (진짜 파일 말고)")
    ap.add_argument("--older-than", default="0", help="prune 에 넘길 값 (기본 0 = 전부 줄임, 가장 센 조건)")
    ap.add_argument("--work", default=None, help="작업 폴더 (기본: 임시 폴더)")
    ap.add_argument("--md", default=None, help="결과를 이 마크다운 파일에 쓴다")
    a = ap.parse_args()

    work = a.work or tempfile.mkdtemp(prefix="verify-prune-")
    dirs = {k: os.path.join(work, k) for k in ("before", "after", "control")}
    for d in dirs.values():
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    log = lambda k: os.path.join(dirs[k], "ratelimit-log.jsonl")
    shutil.copyfile(a.copy, log("before"))
    shutil.copyfile(a.copy, log("after"))
    md5 = hashlib.md5(open(log("before"), "rb").read()).hexdigest()
    print("복사본 md5 %s · %d 바이트" % (md5, os.path.getsize(log("before"))), file=sys.stderr)

    print("prune --apply 를 후 쪽에만 돌린다…", file=sys.stderr)
    env = dict(os.environ, PYTHONPATH=SRC, CLAUDEUSAGE_DATA=dirs["after"], CLAUDEUSAGE_LANG="ko")
    p = subprocess.run([sys.executable, "-m", "claudeusage.cli", "prune", "--apply",
                        "--older-than", a.older_than, "--log", log("after")],
                       env=env, capture_output=True, text=True)
    prune_out = ANSI.sub("", p.stdout).replace(work, "<작업폴더>")
    if p.returncode:
        print(prune_out, p.stderr, file=sys.stderr)
        return 2
    n_before, n_after = os.path.getsize(log("before")), os.path.getsize(log("after"))
    downsample(log("before"), log("control"), 10)
    n_ctrl = os.path.getsize(log("control"))

    print("명령 출력 비교…", file=sys.stderr)
    usage = os.path.join(os.path.dirname(os.path.abspath(a.copy)), "usage-log.jsonl")
    for d in ("before", "after"):                # chat 이 같이 읽는 제품별 기록. 있으면 양쪽에 똑같이
        if os.path.exists(usage):
            shutil.copyfile(usage, os.path.join(dirs[d], "usage-log.jsonl"))
    cmds = compare_commands(dirs["before"], dirs["after"])

    print("limit --fit 값 비교…", file=sys.stderr)
    cache = {}
    ib, ia, ic = measure(log("before"), cache), measure(log("after"), cache), measure(log("control"), cache)
    real, ctrl = compare_items(ib, ia), compare_items(ib, ic)

    diffs = [r for r in real if r[3] != 0]
    bad_cmds = [c for c in cmds if not c["same"] and not c["noisy"]]
    ctrl_diffs = [r for r in ctrl if r[3] != 0]

    md = []
    md.append("### 실행 요약\n")
    md.append("- 입력 복사본: md5 `%s`, %s 바이트" % (md5, format(n_before, ",")))
    md.append("- `prune --apply --older-than %s` 후: %s 바이트 (%.1f%% 감소), 줄 수 %s → %s"
              % (a.older_than, format(n_after, ","), 100.0 * (n_before - n_after) / n_before,
                 format(sum(1 for _ in open(log("before"), "rb")), ","),
                 format(sum(1 for _ in open(log("after"), "rb")), ",")))
    md.append("- 비교한 항목 %d개 중 차이 난 것 **%d개**, 비교한 명령 %d개 중 출력이 달라진 것 **%d개**\n"
              % (len(real), len(diffs), len(cmds), len(bad_cmds)))
    md.append("### prune 의 출력\n\n```\n%s\n```\n" % prune_out.strip())
    md.append("### 명령 출력 전체 비교 (글자 단위)\n")
    md.append("| 명령 | 줄 수 | 정리 전 sha256 | 정리 후 sha256 | 다른 줄 | 종료코드 전/후 | 판정 |")
    md.append("|---|---:|---|---|---:|---:|---|")
    for c in cmds:
        verdict = "같다" if c["same"] else ("**흔들리는 명령** (정리 전끼리도 다름)" if c["noisy"] else "**다르다**")
        md.append("| `%s` | %d | `%s` | `%s` | %d | %d/%d | %s |"
                  % (c["cmd"], c["lines"], c["sha_b"], c["sha_a"], c["ndiff"], c["rc_b"], c["rc_a"], verdict))
    head = [r for r in real if r[0][1] in HEADLINE]
    md.append("\n### `limit --fit` 이 계산하는 값 — 핵심 항목\n")
    md.append(table(head))
    md.append("\n<details><summary>부록: 비교한 항목 전부 (%d개)</summary>\n\n%s\n\n</details>" % (len(real), table(real)))
    md.append("\n### 대조군: 표본을 시간으로 솎으면 (세션마다 10분에 한 줄)\n")
    md.append("크기 %s 바이트(정리 전의 %.1f%%). prune 결과(%.1f%%)보다 %s 값은 다음처럼 달라진다. "
              "비교한 %d개 중 **%d개**가 달라졌다.\n"
              % (format(n_ctrl, ","), 100.0 * n_ctrl / n_before, 100.0 * n_after / n_before,
                 "더 작지만" if n_ctrl < n_after else "비슷하지만",
                 len(ctrl), len(ctrl_diffs)))
    show = [r for r in ctrl if r[0][1] in HEADLINE and r[3] != 0]
    md.append(table(show) if show else "(핵심 항목에서는 차이가 안 잡혔다 — 이 대조군은 쓸모없다)")
    text = "\n".join(md)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    print("\n판정: 항목 차이 %d · 명령 출력 차이 %d · 대조군이 잡은 차이 %d"
          % (len(diffs), len(bad_cmds), len(ctrl_diffs)), file=sys.stderr)
    if not ctrl_diffs:
        print("경고: 대조군에서 차이가 안 잡혔다. 이 검증은 차이를 못 본다.", file=sys.stderr)
        return 3
    return 1 if (diffs or bad_cmds) else 0


if __name__ == "__main__":
    sys.exit(main())
