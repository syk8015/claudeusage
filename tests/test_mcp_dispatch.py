# -*- coding: utf-8 -*-
"""
MCP 도구가 부르는 모듈이 실제로 있는지 본다.

2026-09-17 패키지 구조를 바꿀 때 DISPATCH 넷 중 둘이 옛 파일명(cc-chat.py·cc-usage.py)으로
남았다. tools/list 는 넷을 멀쩡히 내놓아 레지스트리 검사를 통과했고, 실제로 부른 순간에만
ModuleNotFoundError 가 났다. 그래서 목록이 아니라 "부를 대상"을 확인한다.
"""

import importlib.util

import pytest

from claudeusage import mcp_server as M


@pytest.mark.parametrize("name", sorted(M.DISPATCH))
def test_dispatch_module_exists(name):
    script, _ = M.DISPATCH[name]
    spec = importlib.util.find_spec("claudeusage." + script)
    assert spec is not None, "%s → claudeusage.%s 가 없다" % (name, script)


def test_every_listed_tool_is_dispatched():
    listed = {tool["name"] for tool in M.TOOLS}
    assert listed == set(M.DISPATCH)
