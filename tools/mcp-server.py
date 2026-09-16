#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-server.py — 이 프로젝트의 분석기들을 MCP 도구로 내놓는다.

왜 MCP 인가. 이 도구는 로컬 로그(~/.claude/projects, 수백 MB)를 읽는다. 웹사이트로
만들면 사용자가 그걸 업로드해야 한다. MCP 서버로 만들면 **데이터가 있는 자리에서
그대로 돈다.** 그림은 이렇다 — 사용자가 클로드코드에 "나 구독 잘 쓰고 있어?"라고
묻고, 클로드코드가 이 도구를 돌려 답한다. AI 가 우리 사용자다.

새 분석 로직은 없다. 기존 CLI 를 그대로 감싼다.

    tools/cc-value.py    살아남은 줄 · 낭비 · 활동별 비용
    tools/cc-limit.py    한도가 뭘 먹고 오르는지
    tools/cc-chat.py     채팅이 먹은 한도
    tools/cc-usage.py    지금 한도 (앤트로픽에 물어봄)

전송은 stdio 다. **stdout 은 JSON-RPC 전용**이고 사람이 읽을 말은 전부 stderr 로
보낸다. 하나라도 stdout 에 섞이면 클라이언트가 프로토콜을 잃는다.

안전: 모델이 주는 값은 명령줄에 그대로 안 들어간다. 인자는 미리 정한 목록에서
고르게 하고(enum), 경로만 값으로 받되 실제로 있는 디렉터리인지 확인한 뒤
argv 리스트로 넘긴다(셸을 안 거친다).
"""

import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "claudeusage", "title": "구독 본전 계산기", "version": "0.1.0"}

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
        "title": "구독 본전 보기",
        "description": (
            "클로드코드 사용이 실제로 무엇을 남겼는지 잰다. 편집 기록을 시간순으로 접어 "
            "세션이 끝났을 때 **끝까지 살아남은 줄**만 세므로, 3턴 뒤에 갈아엎은 코드는 빠진다. "
            "view 로 보는 각도를 고른다: summary(살아남은 줄·비용), waste(재작업·실패·거부·컨텍스트 "
            "비대를 돈으로), mix(활동별 비용 분해), files(파일별 상세). "
            "scope=project 면 project_path 가 필요하다."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": _enum("all=모든 프로젝트, project=한 프로젝트", ["all", "project"], "all"),
                "project_path": {"type": "string",
                                 "description": "scope=project 일 때 분석할 프로젝트의 절대경로"},
                "view": _enum("보는 각도", ["summary", "waste", "mix", "files"], "summary"),
            },
        },
    },
    {
        "name": "limit_breakdown",
        "title": "한도 분해",
        "description": (
            "5시간·주간 한도가 **무엇 때문에** 오르는지 역산한다. 돈이 아니라 한도가 구독자의 "
            "진짜 제약이다. 캐시 읽기는 비용의 대부분이지만 한도 무게는 신규입력의 1/70 이라, "
            "돈 아끼는 요령과 한도 아끼는 요령이 다르다. "
            "view: windows(창별 소진), fit(모델별 가중치·배율 적합), steps(변화점 전부), "
            "weekly(7일 창)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": _enum("보는 각도", ["windows", "fit", "steps", "weekly"], "windows"),
            },
        },
    },
    {
        "name": "chat_share",
        "title": "채팅이 먹은 한도",
        "description": (
            "한도는 계정 전체에 걸린다. claude.ai 채팅·앱도 같은 한도를 먹는데 로컬에는 "
            "클로드코드 기록만 남으므로, 로그만 읽으면 소진을 덜 센다. 게이지 상승분에서 "
            "로컬 로그로 설명되는 몫을 빼서 채팅 몫을 추정하고, 엔드포인트가 주는 제품별 "
            "정답과 대조한다. view: weeks(주간 요약·정답 대조), windows(5시간 창별), "
            "sources(소스별 커버리지)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": _enum("보는 각도", ["weeks", "windows", "sources"], "weeks"),
            },
        },
    },
    {
        "name": "current_limits",
        "title": "지금 한도",
        "description": (
            "지금 남은 한도를 앤트로픽에 직접 물어 본다. 5시간·주간·모델별(Fable) 한도와 "
            "**제품별 분해**(Claude Code / 채팅 / Cowork)가 온다. "
            "맥 키체인에서 클로드코드 자격증명을 읽어 api.anthropic.com 에만 보낸다. "
            "record=true 면 표본을 data/usage-log.jsonl 에 한 줄 남긴다 — 제품별 분해는 "
            "주간 창 하나치만 오고 지나가면 다시 못 받으므로, 가끔 남겨 둘 값어치가 있다."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record": {"type": "boolean", "default": False,
                           "description": "표본을 기록 파일에 한 줄 남길지"},
            },
        },
    },
]

# 도구 이름 → (스크립트, view 값 → 인자)
DISPATCH = {
    "subscription_value": ("cc-value.py", {
        "summary": [], "waste": ["--waste"], "mix": ["--mix"], "files": ["--files"]}),
    "limit_breakdown": ("cc-limit.py", {
        "windows": [], "fit": ["--fit"], "steps": ["--steps"], "weekly": ["--weekly"]}),
    "chat_share": ("cc-chat.py", {
        "weeks": [], "windows": ["--windows"], "sources": ["--sources"]}),
    "current_limits": ("cc-usage.py", {"": []}),
}


def build_argv(name, args):
    script, views = DISPATCH[name]
    argv = [sys.executable, os.path.join(HERE, script)]

    if name == "subscription_value":
        scope = args.get("scope", "all")
        if scope == "project":
            path = args.get("project_path") or ""
            # 모델이 준 문자열이다. 셸에 안 넘기고, 실제로 있는 디렉터리인지만 본다.
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isdir(path):
                raise ValueError("project_path 가 실제 디렉터리가 아니다: %s" % path)
            argv += ["--project", path]
        else:
            argv += ["--all"]
    elif name == "current_limits":
        if args.get("record"):
            argv += ["--log"]

    view = args.get("view", "")
    if view:
        if view not in views:
            raise ValueError("view 는 %s 중 하나여야 한다" % ", ".join(sorted(views)))
        argv += views[view]
    return argv


def run_tool(name, args):
    argv = build_argv(name, args)
    log("실행: %s" % " ".join(argv[1:]))
    try:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError("%d초 안에 안 끝났다. 기록이 아주 많은 경우다." % RUN_TIMEOUT)
    out = ANSI.sub("", (p.stdout or "") + (p.stderr or ""))
    out = out.strip() or "(출력이 없다)"
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS] + "\n… (잘렸다. 좁은 view 를 골라 다시 부를 것)"
    if p.returncode != 0:
        raise ValueError("도구가 %d 로 끝났다:\n%s" % (p.returncode, out))
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
                    "instructions": "구독 한도와 본전을 재는 도구다. 한도가 왜 닳는지, "
                                    "채팅이 얼마나 먹는지, 코딩이 무엇을 남겼는지 답한다."})
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
            reply(mid, error={"code": -32602, "message": "그런 도구가 없다: %s" % name})
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
        reply(mid, error={"code": -32601, "message": "모르는 메서드: %s" % method})


def main():
    log("claudeusage MCP 서버 시작 (%s)" % ROOT)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            log("JSON 이 아닌 줄을 건너뛴다")
            continue
        try:
            handle(req)
        except Exception as e:                  # 서버는 무슨 일이 있어도 안 죽는다
            log("처리 실패: %r" % e)
            if isinstance(req, dict) and req.get("id") is not None:
                reply(req.get("id"), error={"code": -32603, "message": str(e)})
    log("입력이 닫혔다. 종료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
