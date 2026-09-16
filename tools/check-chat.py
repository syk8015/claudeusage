#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-chat.py — cc-chat.py 를 실데이터에 돌려 정합성을 검사한다.

cc-chat.py 는 뺄셈으로 채팅 몫을 추정한다. 뺄셈은 양쪽이 같은 구간을 재야 뜻이
있으므로, 구간·부호·기준점이 틀어지지 않았는지 본다. 앵커는 2026-09-14 다.
그날 한국시간 11:40~15:20 에 채팅을 썼다고 사용자가 확인했고, 로그로도 5시간 창이
15→53% 로 뛰었다. 이 창을 못 잡으면 추정기가 죽은 것이다.

    python3 tools/check-chat.py
"""

import os, sys, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("ccchat", os.path.join(HERE, "cc-chat.py"))
C = importlib.util.module_from_spec(_spec)
sys.argv = ["check"]
_spec.loader.exec_module(C)

FAIL = []


def ck(name, ok, extra=""):
    print(("  \033[32m통과\033[0m  " if ok else "  \033[31m실패\033[0m  ") + name
          + ("   " + extra if extra else ""))
    if not ok:
        FAIL.append(name)


def main():
    rows, info = C.merged_samples()
    wins = C.windows_of(rows)
    wb, fit = C.weights()
    C.measure(wins, wb, C.bar_cost_by_window())

    # ── 1. 소스 병합. 데스크톱은 더하기만 해야 한다.
    print("\n\033[1m소스 병합\033[0m")
    bar_only = [r for r in rows if r["src"] == "bar"]
    ck("상태바 표본이 그대로 다 있다 (병합이 덜어내지 않았다)",
       len(bar_only) == info["bar"], "%d개" % len(bar_only))
    ck("표본이 시간순이다", all(a["t"] <= b["t"] for a, b in zip(rows, rows[1:])))
    ck("되짚은 창은 전부 데스크톱만 본 것이다 (상태바가 본 창은 리셋을 안다)",
       all(w["srcs"] == {"desktop"} for w in wins if w["inferred"]))
    ck("창 안에서 게이지가 내려가지 않는다 (누적최대로 읽는다)",
       all(w["p_top"] >= w["p_first"] for w in wins))

    # ── 2. 귀속 구간. 0 부터 본 창은 창이 열린 시각부터 세야 한다.
    print("\n\033[1m귀속 구간\033[0m")
    clean = [w for w in wins if w["clean"]]
    ck("온전한 창이 절반은 된다 (기준이 너무 빡빡하면 쓸 데이터가 없다)",
       len(clean) >= len(wins) * 0.4, "%d / %d" % (len(clean), len(wins)))
    ck("온전한 창의 귀속 구간은 창 안에 있다",
       all(w["tf"] >= w["reset"] - C.WIN - 1 and w["t_top"] <= w["reset"] + 1 for w in clean))
    ck("귀속 구간이 창 길이를 안 넘는다",
       all(w["t_top"] - w["tf"] <= C.WIN + 60 for w in wins))

    # ── 3. 추정값의 부호와 크기.
    print("\n\033[1m추정값\033[0m")
    ck("채팅 몫은 음수가 없다", all(w["chat"] >= 0 for w in wins))
    ck("채팅 몫이 상승분을 못 넘는다", all(w["chat"] <= w["rise"] + 1e-9 for w in wins))
    over = [w for w in clean if w["gross"] > 1.0]
    ck("로그 누락 보정은 1.0~1.5 배 안에 있다",
       all(1.0 <= w["gross"] <= 1.5 for w in wins), "보정된 창 %d개" % len(over))
    tot_rise = sum(w["rise"] for w in clean)
    tot_chat = sum(w["chat"] for w in clean)
    ck("전체 채팅 비중이 0~50% 안에 있다 (넘으면 가중치나 구간이 틀어진 것)",
       0 <= tot_chat <= 0.5 * tot_rise,
       "%.0f%%p / %.0f%%p = %.0f%%" % (tot_chat, tot_rise, 100 * tot_chat / tot_rise))

    # ── 4. 앵커. 9/14 채팅 구간을 잡아야 한다.
    print("\n\033[1m앵커 (2026-09-14 채팅)\033[0m")
    anchors = [w for w in wins
               if C.L.utc(w["t_top"], "%Y-%m-%d") == "2026-09-14"
               and C.L.utc(w["t_top"], "%H") in ("04", "10")]
    ck("9/14 새벽·오전 창을 찾는다", len(anchors) == 2, "%d개" % len(anchors))
    ck("두 창 다 채팅 몫이 30%p 를 넘는다",
       bool(anchors) and all(w["chat"] >= 30 for w in anchors),
       " · ".join("%s +%.0f%%p" % (C.L.utc(w["t_top"], "%H:%M"), w["chat"]) for w in anchors))
    quiet = [w for w in clean if w["reqs"] > 200 and C.L.utc(w["t_top"], "%m-%d") != "09-14"]
    if quiet:
        worst = max(quiet, key=lambda w: w["chat"] / (w["rise"] or 1))
        ck("채팅 안 쓴 날의 큰 창은 채팅 몫이 상승분의 절반을 안 넘는다",
           worst["chat"] <= 0.5 * worst["rise"],
           "가장 큰 창 %s %.0f%%p / %.0f%%p" % (C.L.utc(worst["t_top"], "%m-%d %H:%M"),
                                                worst["chat"], worst["rise"]))

    # ── 5. 정답 대조. 기록이 쌓이면 자동으로 늘어난다.
    print("\n\033[1m엔드포인트 정답 대조\033[0m")
    truth = C.truth_rows()
    if not truth:
        print("  \033[2m건너뜀 — usage-log.jsonl 에 분해 기록이 없다\033[0m")
    for wk, t in sorted(truth.items()):
        got = [w for w in clean if C.week_of(w["t_top"]) == wk]
        rise = sum(w["rise"] for w in got)
        est = 100 * sum(w["chat"] for w in got) / rise if rise else 0
        tc = C.truth_chat_pct(t)
        ck("%s 주: 추정과 정답 차이가 15%%p 이내" % C.L.utc(wk, "%m-%d"),
           tc is None or abs(est - tc) <= 15,
           "추정 %.0f%% vs 정답 %.0f%% (온전한 창 %d개)" % (est, tc or 0, len(got)))

    print("\n\033[1m%s\033[0m" % ("전부 통과" if not FAIL else
                                  "실패 %d건: %s" % (len(FAIL), ", ".join(FAIL))))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
