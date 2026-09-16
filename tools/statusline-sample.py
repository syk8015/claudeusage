#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
statusline-sample.py — 한도 게이지 표본을 모은다. 한도 분석의 재료다.

클로드코드는 상태바를 그릴 때마다 세션 상태를 JSON 으로 stdin 에 넘긴다. 거기에
지금 한도 소진율과 누적 비용이 들어 있는데 **어디에도 저장되지 않는다.** 화면에
찍고 버린다. 그래서 값이 바뀔 때마다 한 줄씩 남겨 둔다. 나중에 cc-limit.py 와
cc-chat.py 가 이걸 거꾸로 풀어 "한도가 무엇을 먹고 올랐나"를 낸다.

두 가지 방법으로 쓴다.

  1. 상태바가 없거나 이걸 그냥 쓰고 싶다면 — 기록하고 간단한 상태바를 찍는다.
         python3 tools/statusline-sample.py --print

  2. 이미 쓰는 상태바가 있다면 — 기록하고 받은 JSON 을 그대로 흘려보낸다.
         python3 tools/statusline-sample.py | bash ~/.claude/my-statusline.sh

설정은 ~/.claude/settings.json 의 statusLine 에 넣는다. install.sh 가 그대로
복사해 붙일 수 있는 조각을 찍어 준다.

무슨 일이 있어도 상태바를 깨뜨리지 않는다. 기록이 실패해도 조용히 넘어간다.
"""

import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "data", "ratelimit-log.jsonl")
STATE = os.path.expanduser("~/.claude")


def sample(data, raw):
    """같은 상태면 안 쌓는다. 시각처럼 매번 달라지는 값은 빼고 비교한다.

    비용은 누계 금액만 본다. cost 를 통째로 넣으면 total_duration_ms 가 렌더마다
    달라져 중복 제거가 사실상 꺼진다(실측: 연속 쌍 4067개 중 걸러진 게 0개).
    금액만 보면 37% 가 걸러지고 잃는 정보는 없다.

    중복 방지 파일은 세션마다 따로 둔다. 여러 세션이 동시에 돌 때 서로 덮어쓰면
    같은 상태가 계속 기록된다.
    """
    sid = data.get("session_id")
    if not sid:
        return
    key = json.dumps({"r": data.get("rate_limits"),
                      "u": data.get("context_window"),
                      "c": {"total_cost_usd": (data.get("cost") or {}).get("total_cost_usd")}},
                     sort_keys=True, ensure_ascii=False)
    last = os.path.join(STATE, ".ratelimit-last-%s" % sid)
    try:
        if open(last, encoding="utf-8").read() == key:
            return
    except OSError:
        pass

    import time
    row = dict(data)
    row["logged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        with open(last, "w", encoding="utf-8") as f:
            f.write(key)
    except OSError:
        pass


def line(data):
    """간단한 상태바. 디렉터리 · 모델 · 컨텍스트 · 환산비용 · 5시간 한도."""
    parts = []
    d = ((data.get("workspace") or {}).get("current_dir") or "")
    home = os.path.expanduser("~")
    if d.startswith(home):
        d = "~" + d[len(home):]
    if d:
        parts.append(d)
    m = (data.get("model") or {}).get("display_name")
    if m:
        parts.append(m)
    cw = data.get("context_window") or {}
    if cw.get("used_percentage") is not None:
        parts.append("ctx %.0f%%" % cw["used_percentage"])
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        # 실제 청구액이 아니라 "API 정가로 냈다면" 환산액이다. 구독은 월정액이다.
        parts.append("환산 $%.2f" % cost)
    fh = ((data.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage")
    if fh is not None:
        parts.append("5h %.0f%%" % fh)
    return " │ ".join(parts)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        sys.stdout.write(raw)                 # 못 읽어도 흘려보낸다
        return 0
    try:
        sample(data, raw)
    except Exception:
        pass                                  # 기록 실패가 상태바를 깨뜨리면 안 된다
    if "--print" in sys.argv:
        print(line(data))
    else:
        sys.stdout.write(raw)                 # 다음 상태바로 넘긴다
    return 0


if __name__ == "__main__":
    sys.exit(main())
