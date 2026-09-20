#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py — `claudeusage <하위명령>` 하나로 모든 도구를 부른다.

설치하면 명령이 여럿 생기는 것보다 하나에 모여 있는 게 기억하기 쉽다.
MCP 서버만 따로 `claudeusage-mcp` 로도 부를 수 있게 둔다(설정 파일에 적기 좋게).

    claudeusage value --all
    claudeusage limit --fit
    claudeusage chat
    claudeusage usage --log
    claudeusage statusline --print      (상태바에 거는 것)
    claudeusage prune                   (오래된 표본 줄이기. --apply 전엔 미리보기만)
    claudeusage mcp                     (MCP 서버)
    claudeusage check                   (정합성 검사 둘 다)
"""

import sys

from .i18n import t

SUB = {
    "value": ("cc_value", t("surviving lines, waste, cost by activity",
                            "살아남은 줄·낭비·활동별 비용")),
    "limit": ("cc_limit", t("what drives the rate limit", "한도가 뭘 먹고 오르는지")),
    "chat":  ("cc_chat",  t("how much of the limit chat ate", "채팅이 먹은 한도")),
    "usage": ("cc_usage", t("limits right now, per product", "지금 한도 + 제품별 분해")),
    "statusline": ("statusline", t("status line collector", "상태바 표본 수집기")),
    "prune": ("prune", t("shrink old samples (preview unless --apply)",
                         "오래된 표본 줄이기 (--apply 전엔 미리보기만)")),
    "mcp":   ("mcp_server", t("run the MCP server (stdio)", "MCP 서버 실행 (stdio)")),
}


def usage():
    print("claudeusage <command> [options]\n")
    for name, (_, desc) in SUB.items():
        print("  %-11s %s" % (name, desc))
    print("  %-11s %s" % ("check", t("run both consistency checkers",
                                     "정합성 검사 둘 다 돌린다")))
    print("\n" + t("Docs: https://github.com/syk8015/claudeusage",
                   "문서: https://github.com/syk8015/claudeusage"))


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        usage()
        return 0
    cmd = sys.argv[1]
    # 하위 명령 이름을 지우고 나머지를 그대로 넘긴다. 각 도구는 sys.argv 를 직접 읽는다.
    sys.argv = [("claudeusage " + cmd)] + sys.argv[2:]

    if cmd == "check":
        from . import check_limit, check_chat
        return (check_limit.main() or 0) + (check_chat.main() or 0)
    if cmd not in SUB:
        print(t("unknown command: %s", "모르는 명령: %s") % cmd)
        usage()
        return 1
    mod = __import__("claudeusage." + SUB[cmd][0], fromlist=["main"])
    return mod.main()


if __name__ == "__main__":
    sys.exit(main())
