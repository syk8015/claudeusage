#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-limit.py — cc-limit.py 를 실데이터에 돌려 정합성을 검사한다.

이 프로젝트의 규칙: 지표를 바꾸면 반드시 실데이터에 돌려본다. 지금까지 나온
발견이 전부 실데이터에서만 나왔기 때문이다. cc-limit.py 를 손대면 이걸 돌린다.

    claudeusage check
"""

import os, sys, bisect

from . import cc_limit as L

FAIL = []


def ck(name, ok, extra=""):
    print(("  \033[32m통과\033[0m  " if ok else "  \033[31m실패\033[0m  ") + name
          + ("   " + extra if extra else ""))
    if not ok:
        FAIL.append(name)


def main():
    # ── 1. 창 묶기와 변화점 (5시간·7일 둘 다)
    for field, wl, lab in (("five_hour", 5 * 3600, "5시간"), ("seven_day", 7 * 86400, "7일")):
        rows, _, _ = L.load_samples(L.DEFAULT_LOG, field)
        wins, steps = L.windows_and_steps(rows, wl)
        print("\n\033[1m%s 창 %d개 · 변화점 %d개\033[0m" % (lab, len(wins), len(steps)))
        ck("창 안에 음수 Δ%p 가 없다 (누적최대가 톱니를 지웠다)",
           all(s["dpct"] > 0 for s in steps))
        ck("변화점 Δ%p 합 = 창의 시작→최대",
           all(sum(x["dpct"] for x in w["steps"]) == w["p_last"] - w["p_first"] for w in wins))
        ck("귀속 구간이 겹치지 않고 시간순이다",
           all(s["tf"] < s["t1"] for s in steps)
           and all(a["t1"] <= b["tf"] for w in wins for a, b in zip(w["steps"], w["steps"][1:])))
        ck("t0 가 귀속 구간 안에 있다", all(s["tf"] <= s["t0"] <= s["t1"] for s in steps))
        ck("창마다 리셋 시각이 유일하다", len({w["reset"] for w in wins}) == len(wins))

    # ── 2. 비용 대조 (5시간 기준)
    rows, _, _ = L.load_samples(L.DEFAULT_LOG, "five_hour")
    wins, steps = L.windows_and_steps(rows, 5 * 3600)
    reqs = L.request_index(min(w["reset"] - 5 * 3600 for w in wins), rows[-1]["t"])
    L.attribute(steps, reqs)
    bar = L.cost_by_window(rows, 5 * 3600)

    print("\n\033[1m비용 대조\033[0m")
    ratios = sorted(L.agg(w["steps"])["cost"] / bar[w["reset"]]
                    for w in wins if bar.get(w["reset"], 0) > 5)
    med = ratios[len(ratios) // 2]
    # 로그 기반은 상태바보다 약 6% 적게 나오는 게 정상이다(README.ko.md 함정 7).
    # 로그에 안 남는 백그라운드 호출이 세션마다 $0.3~$4 씩 있고, 이건 세션 크기와
    # 무관해서 작은 세션이 많은 창일수록 비율이 더 내려간다.
    ck("창별 기록/상태바 중앙값이 0.85~1.02", 0.85 <= med <= 1.02,
       "중앙값 %.3f  범위 %.2f~%.2f" % (med, ratios[0], ratios[-1]))
    ck("어느 창도 상태바를 넘지 않는다", ratios[-1] <= 1.02, "최대 %.2f" % ratios[-1])

    # ── 3. 귀속 누락. 게이지를 한 번도 못 본 첫 창은 원래 귀속이 불가능하다.
    times = [r["t"] for r in reqs]
    covered = [False] * len(reqs)
    for s in steps:
        for i in range(bisect.bisect_right(times, s["tf"]), bisect.bisect_right(times, s["t1"])):
            covered[i] = True
    blind = {w["reset"] for w in wins if not w["steps"]}
    lost = sum(r["cost"] for r, c in zip(reqs, covered)
               if not c and not any(b - 5 * 3600 < r["t"] <= b for b in blind))
    total = sum(r["cost"] for r in reqs)
    ck("변화점을 못 본 창을 빼면 누락이 5% 미만", lost / total < 0.05,
       "누락 $%.2f / $%.2f = %.1f%%" % (lost, total, 100 * lost / total))

    # ── 4. 로그 밖 소비. 2026-09-14 채팅을 쓰는 동안 15→53%(+38)가 로컬 $1.75 로
    #       올랐다(사용자 확인). 이 점프를 못 잡으면 판정 기준이 틀어진 것이다.
    print("\n\033[1m로그 밖 소비\033[0m")
    off = [s for s in steps if s.get("offlog")]
    ck("판정된 변화점은 전부 Δ≥%d%%p 이고 1%%p 당 $%.1f 미만"
       % (L.OFFLOG_MIN_PCT, L.OFFLOG_MAX_RATE),
       all(s["dpct"] >= L.OFFLOG_MIN_PCT and s["cost"] / s["dpct"] < L.OFFLOG_MAX_RATE for s in off),
       "%d개 · %d%%p" % (len(off), sum(s["dpct"] for s in off)))
    anchor = [s for s in steps if L.utc(s["t1"], "%Y-%m-%dT%H:%M") == "2026-09-14T02:58"
              and s["p0"] == 15 and s["p1"] == 53]
    ck("9/14 채팅 구간 15→53 점프를 잡는다", bool(anchor) and all(s["offlog"] for s in anchor))
    ck("로그 밖 소비가 전체의 절반을 넘지 않는다 (넘으면 기준이 너무 느슨하다)",
       sum(s["dpct"] for s in off) < 0.5 * sum(s["dpct"] for s in steps))

    # ── 5. 단가표. 실측으로 역산한 값과 맞아야 한다.
    print("\n\033[1m단가표\033[0m")
    one = {"usage": {"output_tokens": 1_000_000}}
    ck("fable-5-1 은 $25/M 출력 (fable-5 규칙에 먼저 걸리면 안 된다)",
       L.usage_cost(dict(one, model="claude-fable-5-1")) == 25.0)
    ck("fable-5 는 $50/M 출력", L.usage_cost(dict(one, model="claude-fable-5")) == 50.0)
    ck("opus-5[1m] 은 $25/M 출력이고 opus-5 로 묶인다",
       L.usage_cost(dict(one, model="claude-opus-5[1m]")) == 25.0
       and L.model_key("claude-opus-5[1m]") == "opus-5")

    print("\n\033[1m%s\033[0m" % ("전부 통과" if not FAIL else "실패 %d건: %s" % (len(FAIL), ", ".join(FAIL))))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
