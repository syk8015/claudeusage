#!/bin/bash
# cc-cost.sh — 클로드코드 세션의 "만약 API 정가로 결제했다면" 비용을 계산한다.
#
#   ./cc-cost.sh              현재 프로젝트의 가장 최근 세션
#   ./cc-cost.sh all          현재 프로젝트의 모든 세션 합계
#   ./cc-cost.sh <세션ID>     특정 세션
#   ./cc-cost.sh <경로.jsonl> 특정 파일
#
# 토큰 수는 API 응답의 usage 필드를 그대로 쓰므로 추정이 아니라 실측값이다.
# 단가만 아래 표에서 가져온다(2026-06 기준 Anthropic 1st-party API 정가).

set -uo pipefail
# 한글 경로가 바이트 단위로 쪼개지지 않도록 UTF-8 로케일 강제
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

# ── 모델별 단가 (100만 토큰당 USD): "입력 출력"
#    캐시 읽기 = 입력×0.1 / 캐시쓰기 5분 = 입력×1.25 / 캐시쓰기 1시간 = 입력×2
price_of() {
  case "$1" in
    *fable-5-1*|*mythos-5-1*)      echo "5 25" ;;   # 5.1 은 5 의 절반. 반드시 먼저 볼 것
    *fable-5*|*mythos-5*)          echo "10 50" ;;
    *opus-5*|*opus-4-8*|*opus-4-7*|*opus-4-6*) echo "5 25" ;;
    *sonnet-5*)                    echo "2 10" ;;
    *sonnet-4-6*)                  echo "3 15" ;;
    *haiku-4-5*)                   echo "1 5" ;;
    *) echo "5 25" ;;   # 모르는 모델은 Opus 요율로 가정
  esac
}
# fast 모드(Opus 5/4.8)는 프리미엄 요율
price_of_fast() { echo "10 50"; }

# 경로 munging 규칙을 추측하는 대신, 실패하면 기록 안의 cwd 필드로 역추적한다.
proj_dir() {
  local here munged d
  here=$(pwd)
  munged=$(printf '%s' "$here" | sed 's/[^a-zA-Z0-9]/-/g')
  if [ -d "$HOME/.claude/projects/$munged" ]; then
    echo "$HOME/.claude/projects/$munged"; return
  fi
  for d in "$HOME"/.claude/projects/*/; do
    local f
    f=$(ls -t "$d"*.jsonl 2>/dev/null | head -1) || continue
    [ -z "$f" ] && continue
    if [ "$(head -50 "$f" | jq -r 'select(.cwd != null) | .cwd' 2>/dev/null | head -1)" = "$here" ]; then
      echo "${d%/}"; return
    fi
  done
  echo "$HOME/.claude/projects/$munged"   # 못 찾으면 추측값 반환 (아래에서 에러 처리)
}

# 세션 하나(메인 + 서브에이전트)의 usage 엔트리를 전부 stdout 으로 흘려보낸다.
collect() {
  local main="$1" sub_dir="${1%.jsonl}/subagents"
  cat "$main"
  [ -d "$sub_dir" ] && cat "$sub_dir"/*.jsonl 2>/dev/null || true
}

# stdin 의 JSONL → 모델×speed 별로 토큰을 집계
aggregate() {
  # 일부 기록에 깨진 서로게이트 페어가 섞여 있어 파싱 실패 줄은 건너뛴다
  jq -R 'fromjson? // empty' | jq -sr '
    [ .[] | select(.type=="assistant" and .message.usage != null) ]
    # 한 응답이 content block 수만큼 쪼개져 기록되므로 message.id 로 중복 제거
    | group_by(.message.id) | map(.[0])
    | group_by(.message.model + "|" + (.message.usage.speed // "standard"))
    | map({
        key:   (.[0].message.model + "|" + (.[0].message.usage.speed // "standard")),
        reqs:  length,
        inp:   (map(.message.usage.input_tokens            // 0) | add),
        out:   (map(.message.usage.output_tokens           // 0) | add),
        think: (map(.message.usage.output_tokens_details.thinking_tokens // 0) | add),
        cr:    (map(.message.usage.cache_read_input_tokens // 0) | add),
        w1h:   (map(.message.usage.cache_creation.ephemeral_1h_input_tokens // 0) | add),
        w5m:   (map(.message.usage.cache_creation.ephemeral_5m_input_tokens // 0) | add),
        web:   (map(.message.usage.server_tool_use.web_search_requests      // 0) | add)
      })
    | .[] | [.key,.reqs,.inp,.out,.think,.cr,.w1h,.w5m,.web] | @tsv
  '
}

report() {
  local label="$1"; shift
  printf '\n\033[1m%s\033[0m\n' "$label"
  printf '%-26s %6s %10s %10s %10s %10s %9s\n' \
    "모델" "요청" "출력tok" "캐시읽기" "캐시쓰기" "생각tok" "USD"
  printf '%s\n' "────────────────────────────────────────────────────────────────────────────────────"

  local grand=0
  while IFS=$'\t' read -r key reqs inp out think cr w1h w5m web; do
    [ -z "${key:-}" ] && continue
    local model="${key%|*}" speed="${key#*|}"
    local rates
    if [ "$speed" = "fast" ]; then rates=$(price_of_fast); else rates=$(price_of "$model"); fi
    local pin pout
    pin=$(echo "$rates" | cut -d' ' -f1); pout=$(echo "$rates" | cut -d' ' -f2)

    local usd
    usd=$(awk -v i="$inp" -v o="$out" -v cr="$cr" -v w1="$w1h" -v w5="$w5m" -v wb="$web" \
              -v pi="$pin" -v po="$pout" 'BEGIN{
        printf "%.4f", (i*pi + o*po + cr*pi*0.1 + w1*pi*2 + w5*pi*1.25)/1000000 + wb*0.01
      }')
    grand=$(awk -v a="$grand" -v b="$usd" 'BEGIN{printf "%.4f", a+b}')

    local name; name=$(echo "$model" | sed 's/^claude-//')
    [ "$speed" = "fast" ] && name="$name ⚡"
    printf '%-26s %6s %10s %10s %10s %10s %9s\n' \
      "$name" "$reqs" "$out" "$cr" "$((w1h + w5m))" "$think" "\$$usd"
  done

  printf '%s\n' "────────────────────────────────────────────────────────────────────────────────────"
  printf '\033[1m%-26s %54s\033[0m\n' "합계" "\$$grand"
}

# ── 대상 결정
target="${1:-}"
dir=$(proj_dir)

if [ "$target" = "all" ]; then
  tmp=$(mktemp)
  for f in "$dir"/*.jsonl; do [ -e "$f" ] && collect "$f"; done > "$tmp"
  aggregate < "$tmp" | report "프로젝트 전체 ($(ls "$dir"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')개 세션) — $(pwd)"
  rm -f "$tmp"
elif [ -n "$target" ] && [ -f "$target" ]; then
  collect "$target" | aggregate | report "세션 $(basename "$target" .jsonl)"
elif [ -n "$target" ]; then
  collect "$dir/$target.jsonl" | aggregate | report "세션 $target"
else
  latest=$(ls -t "$dir"/*.jsonl 2>/dev/null | head -1)
  [ -z "$latest" ] && { echo "세션 파일을 못 찾음: $dir"; exit 1; }
  collect "$latest" | aggregate | report "최근 세션 $(basename "$latest" .jsonl)"
fi

printf '\n\033[2m※ 토큰 수는 API가 돌려준 실측값. 단가는 Anthropic 1st-party API 정가 기준이며\n'
printf '   Max 구독이라 실제 청구액은 아님. 웹검색은 1000회당 $10로 계산.\033[0m\n'
