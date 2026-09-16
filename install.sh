#!/bin/sh
# install.sh — 저장소를 직접 받아 쓸 때의 설치. PyPI 로 받았으면 이 파일이 필요 없다.
#
#     pip install claudeusage     또는     uv tool install claudeusage
#     claude mcp add --scope user --transport stdio claudeusage -- claudeusage-mcp
#
# 이 스크립트는 "저장소를 clone 해서 고쳐 가며 쓰는" 경우를 위한 것이다.
# 패키지를 편집 모드로 설치하고, 스킬을 넣고, MCP 서버를 등록한다.
#
#     ./install.sh              설치 (여러 번 돌려도 안전하다)
#     ./install.sh --uninstall  되돌리기
set -e

REPO=$(cd "$(dirname "$0")" && pwd)
SKILL_DIR="$HOME/.claude/skills/claudeusage"
VENV="$HOME/.claudeusage/venv"
NAME=claudeusage

if [ "$1" = "--uninstall" ]; then
  claude mcp remove --scope user "$NAME" 2>/dev/null || echo "MCP 서버가 등록돼 있지 않다."
  rm -rf "$SKILL_DIR"
  echo "지웠다: $SKILL_DIR"
  echo "패키지는 남아 있다. 지우려면: pip uninstall claudeusage"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 이 없다."; exit 1; }
command -v claude  >/dev/null 2>&1 || { echo "claude CLI 가 없다. 클로드코드를 먼저 설치할 것."; exit 1; }

# ── 1. 패키지. 편집 모드라 저장소를 고치면 바로 반영된다.
#
# 요즘 파이썬(홈브류·데비안 등)은 시스템 환경에 설치하는 걸 막는다(PEP 668).
# 그래서 되는 걸 순서대로 시도한다. uv → pipx → pip --user → 마지막에 안내.
install_pkg() {
  if command -v uv >/dev/null 2>&1; then
    uv tool install -q --editable "$REPO" && { echo "설치: uv tool (편집 모드)"; return 0; }
  fi
  if command -v pipx >/dev/null 2>&1; then
    pipx install --force -e "$REPO" >/dev/null 2>&1 && { echo "설치: pipx (편집 모드)"; return 0; }
  fi
  if python3 -m pip install -q -e "$REPO" 2>/dev/null; then
    echo "설치: pip (편집 모드)"; return 0
  fi
  if python3 -m pip install -q --user -e "$REPO" 2>/dev/null; then
    echo "설치: pip --user (편집 모드)"; return 0
  fi
  # 마지막 수단: 전용 가상환경. 새 도구를 깔 필요가 없고 시스템도 안 건드린다.
  if python3 -m venv "$VENV" 2>/dev/null && "$VENV/bin/pip" install -q -e "$REPO"; then
    echo "설치: 전용 가상환경 ($VENV)"
    BIN="$VENV/bin"
    if [ -d "$HOME/.local/bin" ]; then
      ln -sf "$BIN/claudeusage" "$HOME/.local/bin/claudeusage"
      ln -sf "$BIN/claudeusage-mcp" "$HOME/.local/bin/claudeusage-mcp"
      echo "링크: ~/.local/bin/claudeusage"
    else
      echo "PATH 에 넣으려면: export PATH=\"$BIN:\$PATH\""
    fi
    return 0
  fi
  return 1
}

if ! install_pkg; then
  cat <<TXT
패키지를 설치하지 못했다. 요즘 파이썬은 시스템 환경 설치를 막는다(PEP 668).
아래 중 하나를 쓴 뒤 이 스크립트를 다시 돌릴 것.

    brew install uv && uv tool install --editable "$REPO"
    python3 -m pip install --user -e "$REPO"
    python3 -m pip install --break-system-packages -e "$REPO"   (권하지 않음)

설치 없이 저장소에서 바로 쓰려면:
    PYTHONPATH="$REPO/src" python3 -m claudeusage.cli limit
TXT
  exit 1
fi

# ── 2. 스킬. __REPO__ 를 실제 경로로 바꿔 넣는다.
mkdir -p "$SKILL_DIR"
sed "s|__REPO__|$REPO|g" "$REPO/skills/claudeusage/SKILL.md" > "$SKILL_DIR/SKILL.md"
echo "스킬 설치: $SKILL_DIR/SKILL.md"

# ── 3. MCP 서버. 이미 있으면 지우고 다시 넣는다.
claude mcp remove --scope user "$NAME" >/dev/null 2>&1 || true
# PATH 에 없을 수도 있으니(가상환경에 깔린 경우) 실제 경로를 찾아 등록한다.
SERVER=$(command -v claudeusage-mcp 2>/dev/null || echo "$VENV/bin/claudeusage-mcp")
claude mcp add --scope user --transport stdio "$NAME" -- "$SERVER"

echo
echo "확인: claude mcp list   ($NAME 이 ✔ Connected 여야 한다)"
echo "새 세션부터 스킬이 보인다. 지금 세션에는 안 뜬다."

# ── 4. 표본 수집기. 이건 자동으로 안 건다 — 상태바는 사용자 것이다.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ] && grep -q "claudeusage statusline\|statusline-sample.py" "$SETTINGS" 2>/dev/null; then
  echo
  echo "표본 수집기: 이미 걸려 있다."
else
  cat <<'TXT'

── 한도 분석을 쓰려면 한 가지 더 ──────────────────────────────
클로드코드는 한도 소진율을 화면에 찍고 버린다. 저장을 안 한다.
그래서 limit 과 chat 은 표본이 쌓여야 돌아간다.
(value 와 usage 는 지금 바로 된다.)

~/.claude/settings.json 의 statusLine 에 이걸 넣는다.

  "statusLine": {
    "type": "command",
    "command": "claudeusage statusline --print"
  }

이미 쓰는 상태바가 있으면 --print 를 빼고 파이프로 이어 붙인다.
받은 JSON 을 그대로 흘려보내므로 뒤에 원래 상태바를 잇는다.

  "command": "claudeusage statusline | bash ~/.claude/my-statusline.sh"

상태바는 우리가 함부로 못 고친다. 직접 넣을 것.
TXT
fi
