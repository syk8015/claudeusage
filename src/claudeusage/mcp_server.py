#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-server.py — 이 프로젝트의 분석기들을 MCP 도구로 내놓는다.

왜 MCP 인가. 이 도구는 로컬 로그(~/.claude/projects, 수백 MB)를 읽는다. 웹사이트로
만들면 사용자가 그걸 업로드해야 한다. MCP 서버로 만들면 **데이터가 있는 자리에서
그대로 돈다.** 그림은 이렇다 — 사용자가 클로드코드에 "나 구독 잘 쓰고 있어?"라고
묻고, 클로드코드가 이 도구를 돌려 답한다. AI 가 우리 사용자다.

새 분석 로직은 없다. 기존 CLI 를 그대로 감싼다.

    cc_value.py    살아남은 줄 · 낭비 · 활동별 비용
    cc_limit.py    한도가 뭘 먹고 오르는지
    cc_chat.py     채팅이 먹은 한도
    cc_usage.py    지금 한도 (앤트로픽에 물어봄)

전송은 stdio 다. **stdout 은 JSON-RPC 전용**이고 사람이 읽을 말은 전부 stderr 로
보낸다. 하나라도 stdout 에 섞이면 클라이언트가 프로토콜을 잃는다.

안전: 모델이 주는 값은 명령줄에 그대로 안 들어간다. 인자는 미리 정한 목록에서
고르게 하고(enum), 경로만 값으로 받되 실제로 있는 디렉터리인지 확인한 뒤
argv 리스트로 넘긴다(셸을 안 거친다).
"""

import json, os, re, subprocess, sys

from .i18n import t
from . import paths

PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "claudeusage", "title": "Subscription value meter", "version": "0.1.0"}

RUN_TIMEOUT = 600          # 전체 기록을 훑는 분석은 분 단위가 걸린다
MAX_CHARS = 60000          # 모델 컨텍스트를 통째로 먹지 않게 자른다
ANSI = re.compile(r"\033\[[0-9;]*m")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ────────────────────────────────────────────── 도구 정의

def _enum(desc, values, default):
    return {"type": "string", "description": desc, "enum": list(values), "default": default}


TOOLS = [
    {
        "name": "subscription_value",
        "title": "Subscription value",
        "description": (
            "Measures what Claude Code usage actually left behind. Edits are folded in time "
            "order, so only lines that **survived to the end of the session** count — code "
            "rewritten three turns later drops out. "
            "view: summary (surviving lines and cost), waste (rework, failures, rejections and "
            "context bloat priced in dollars), mix (cost by activity), files (per-file detail). "
            "scope=project requires project_path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": _enum("all = every project, project = one project",
                               ["all", "project"], "all"),
                "project_path": {"type": "string",
                                 "description": "Absolute path of the project, when scope=project"},
                "view": _enum("Which angle to report", ["summary", "waste", "mix", "files"],
                              "summary"),
            },
        },
    },
    {
        "name": "limit_breakdown",
        "title": "Limit breakdown",
        "description": (
            "Works out **what drives** the 5-hour and weekly rate limits. On a subscription the "
            "real constraint is the limit gauge, not dollars. Cache reads are most of the cost "
            "but weigh about 1/70 of fresh input against the limit, so saving money and saving "
            "limit are different skills. "
            "view: windows (burn per window), fit (per-model weights and multipliers), "
            "steps (every change point), weekly (7-day windows)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": _enum("Which angle to report",
                              ["windows", "fit", "steps", "weekly"], "windows"),
            },
        },
    },
    {
        "name": "chat_share",
        "title": "Limit eaten by chat",
        "description": (
            "Rate limits apply to the whole account, so claude.ai chat and the mobile app drain "
            "the same gauge while leaving no local trace — log-only tools undercount. This "
            "subtracts what local logs explain from what the gauge actually did, and checks that "
            "estimate against the per-product breakdown Anthropic returns. "
            "view: weeks (weekly summary vs. that ground truth), windows (per 5-hour window), "
            "sources (coverage of each data source)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": _enum("Which angle to report", ["weeks", "windows", "sources"], "weeks"),
            },
        },
    },
    {
        "name": "current_limits",
        "title": "Current limits",
        "description": (
            "Asks Anthropic what is left right now: the 5-hour, weekly and per-model (Fable) "
            "limits, plus the **per-product breakdown** (Claude Code / chat / Cowork). "
            "Reads Claude Code's credentials from the macOS keychain and sends them only to "
            "api.anthropic.com. "
            "record=true appends one sample to data/usage-log.jsonl — the breakdown covers only "
            "the current weekly window and is gone once that window rolls over, so it is worth "
            "recording now and then."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record": {"type": "boolean", "default": False,
                           "description": "Append one sample to the log file"},
            },
        },
    },
]

# 도구 이름 → (스크립트, view 값 → 인자)
DISPATCH = {
    "subscription_value": ("cc_value", {
        "summary": [], "waste": ["--waste"], "mix": ["--mix"], "files": ["--files"]}),
    "limit_breakdown": ("cc_limit", {
        "windows": [], "fit": ["--fit"], "steps": ["--steps"], "weekly": ["--weekly"]}),
    "chat_share": ("cc_chat", {
        "weeks": [], "windows": ["--windows"], "sources": ["--sources"]}),
    "current_limits": ("cc_usage", {"": []}),
}


def build_argv(name, args):
    # 하위 프로세스로 돌린다. 분석 도구들이 sys.argv 를 직접 읽고 표를 찍는 구조라,
    # 같은 프로세스에서 부르면 출력을 가로채기가 지저분하다. 모듈 이름으로 부르므로
    # 설치본(site-packages)에서도 저장소에서도 똑같이 동작한다.
    script, views = DISPATCH[name]
    argv = [sys.executable, "-m", "claudeusage." + script]

    if name == "subscription_value":
        scope = args.get("scope", "all")
        if scope == "project":
            path = args.get("project_path") or ""
            # 모델이 준 문자열이다. 셸에 안 넘기고, 실제로 있는 디렉터리인지만 본다.
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isdir(path):
                raise ValueError(t("project_path is not a directory: %s",
                                   "project_path 가 실제 디렉터리가 아니다: %s") % path)
            argv += ["--project", path]
        else:
            argv += ["--all"]
    elif name == "current_limits":
        if args.get("record"):
            argv += ["--log"]

    view = args.get("view", "")
    if view:
        if view not in views:
            raise ValueError(t("view must be one of: %s", "view 는 %s 중 하나여야 한다")
                             % ", ".join(sorted(views)))
        argv += views[view]
    return argv


def run_tool(name, args):
    argv = build_argv(name, args)
    log("run: %s" % " ".join(argv[1:]))
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError(t("Did not finish within %ds — a very large history.",
                           "%d초 안에 안 끝났다. 기록이 아주 많은 경우다.") % RUN_TIMEOUT)
    out = ANSI.sub("", (p.stdout or "") + (p.stderr or ""))
    out = out.strip() or t("(no output)", "(출력이 없다)")
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS] + t("\n… (truncated — call again with a narrower view)",
                                  "\n… (잘렸다. 좁은 view 를 골라 다시 부를 것)")
    if p.returncode != 0:
        raise ValueError(t("The tool exited with %d:\n%s",
                           "도구가 %d 로 끝났다:\n%s") % (p.returncode, out))
    return out


# ────────────────────────────────────────────── JSON-RPC

def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method, mid = req.get("method"), req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        # 클라이언트가 말한 버전을 그대로 돌려주는 게 안전하다. 모르는 값이면 우리 것으로.
        want = params.get("protocolVersion")
        ver = want if isinstance(want, str) and want else PROTOCOL_VERSION
        reply(mid, {"protocolVersion": ver,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER,
                    "instructions": "Measures whether a Claude subscription is paying off, from "
                                    "local logs: what drives the rate limit, how much of it chat "
                                    "ate, and what coding actually left behind."})
        return

    if method in ("notifications/initialized", "notifications/cancelled"):
        return                                  # 알림에는 답하지 않는다

    if method == "ping":
        reply(mid, {})
        return

    if method == "tools/list":
        reply(mid, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in DISPATCH:
            reply(mid, error={"code": -32602, "message": "Unknown tool: %s" % name})
            return
        try:
            text = run_tool(name, args)
        except Exception as e:
            # 도구가 실패한 것은 프로토콜 오류가 아니다. isError 로 돌려줘야 모델이 읽는다.
            reply(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            return
        reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        return

    if mid is not None:
        reply(mid, error={"code": -32601, "message": "Unknown method: %s" % method})


def main():
    log("claudeusage MCP server started (data: %s)" % paths.data_dir())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            log("skipping a line that is not JSON")
            continue
        try:
            handle(req)
        except Exception as e:                  # 서버는 무슨 일이 있어도 안 죽는다
            log("failed to handle: %r" % e)
            if isinstance(req, dict) and req.get("id") is not None:
                reply(req.get("id"), error={"code": -32603, "message": str(e)})
    log("stdin closed, exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
