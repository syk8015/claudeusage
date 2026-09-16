#!/bin/sh
# install.sh — MCP 서버와 클로드코드 스킬을 설치한다.
#
# 사용자 수준으로 넣는다. 이 도구는 ~/.claude 를 읽으므로 어느 프로젝트에서 물어도
# 답해야 값을 한다. 저장소를 어디에 두든 되게, 실제 경로를 그때그때 박아 넣는다.
#
#     ./install.sh              설치 (여러 번 돌려도 안전하다)
#     ./install.sh --uninstall  되돌리기
set -e

REPO=$(cd "$(dirname "$0")" && pwd)
SKILL_DIR="$HOME/.claude/skills/claudeusage"
NAME=claudeusage

if [ "$1" = "--uninstall" ]; then
  claude mcp remove --scope user "$NAME" 2>/dev/null || echo "MCP 서버가 등록돼 있지 않다."
  rm -rf "$SKILL_DIR"
  echo "지웠다: $SKILL_DIR"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 이 없다."; exit 1; }
command -v claude  >/dev/null 2>&1 || { echo "claude CLI 가 없다. 클로드코드를 먼저 설치할 것."; exit 1; }

# ── 1. 스킬. __REPO__ 를 실제 경로로 바꿔 넣는다.
mkdir -p "$SKILL_DIR"
sed "s|__REPO__|$REPO|g" "$REPO/skills/claudeusage/SKILL.md" > "$SKILL_DIR/SKILL.md"
echo "스킬 설치: $SKILL_DIR/SKILL.md"

# ── 2. MCP 서버. 이미 있으면 지우고 다시 넣는다(경로가 바뀌었을 수 있다).
claude mcp remove --scope user "$NAME" >/dev/null 2>&1 || true
claude mcp add --scope user --transport stdio "$NAME" -- python3 "$REPO/tools/mcp-server.py"

echo
echo "확인: claude mcp list   ($NAME 이 ✔ Connected 여야 한다)"
echo "새 세션부터 스킬이 보인다. 지금 세션에는 안 뜬다."

# ── 3. 표본 수집기. 이건 자동으로 안 건다 — 상태바는 사용자 것이다.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ] && grep -q "statusline-sample.py" "$SETTINGS" 2>/dev/null; then
  echo
  echo "표본 수집기: 이미 걸려 있다."
else
  cat <<TXT

── 한도 분석을 쓰려면 한 가지 더 ──────────────────────────────
클로드코드는 한도 소진율을 화면에 찍고 버린다. 저장을 안 한다.
그래서 cc-limit.py 와 cc-chat.py 는 표본이 쌓여야 돌아간다.
(cc-value.py 와 cc-usage.py 는 지금 바로 된다.)

$SETTINGS 의 statusLine 에 이걸 넣는다.

  "statusLine": {
    "type": "command",
    "command": "python3 $REPO/tools/statusline-sample.py --print"
  }

이미 쓰는 상태바가 있으면 --print 를 빼고 앞에 이어 붙인다.
받은 JSON 을 그대로 흘려보내므로 뒤에 원래 상태바를 파이프로 잇는다.

  "command": "python3 $REPO/tools/statusline-sample.py | bash ~/.claude/my-statusline.sh"

상태바는 우리가 함부로 못 고친다. 직접 넣을 것.
TXT
fi
