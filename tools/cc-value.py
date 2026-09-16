#!/usr/bin/env python3
"""cc-value.py — 클로드코드 세션의 "쓴 돈 대비 실제로 남은 결과"를 잰다.

핵심은 '살아남은 줄'이다. 편집 이벤트를 그냥 더하면 3턴 뒤에 갈아엎은 코드도
성과로 잡힌다. 파일별로 세션 시작 상태와 끝 상태를 복원해서 그 둘의 차이만
세면 헛수고가 저절로 상쇄된다.

분석 대상 고르기 (이 도구는 claudeusage 에 있지만 볼 데이터는 보통 다른 폴더다):
  --project <폴더경로>   그 프로젝트의 세션
  --all                 모든 프로젝트
  (아무것도 안 주면 현재 폴더 기준)

보기 고르기:
  (기본)      살아남은 줄 vs 순진하게 센 것
  --waste     낭비를 돈으로
  --mix       활동별 비용 분해 + 코딩몫 기준 효율
  --export    서버로 보낼 payload + 유출 검사
  --files     파일별 상세
  <세션ID>     특정 세션만

토큰 수는 API 가 돌려준 실측값이고, 단가만 아래 표에서 가져온다. 단가는 상태바
값과 요청 단위로 대조해 확인했다(오차 $0.0000). 다만 로그에 안 남는 백그라운드
호출이 있어서 이 계산은 실제보다 약 6% 적게 나온다.
Max 구독이라 실제 청구액은 아니며 "API 정가로 냈다면" 환산값이다.
"""
import json, sys, os, glob, difflib, unicodedata, re
from collections import defaultdict

# ── 모델별 100만 토큰당 단가 (입력, 출력)
#
# 순서가 중요하다. 앞에서부터 부분 문자열로 맞춰 보므로 반드시 긴 이름이 먼저여야
# 한다. fable-5-1 을 fable-5 뒤에 두면 claude-fable-5-1 이 fable-5 에 먼저 걸려
# 비용이 정확히 2배가 된다. 100% 단일 모델 세션에서 상태바 누계와 대조해 역산한 값:
#     claude-fable-5-1  →  $5.04 / $25.21     claude-opus-5  →  $5.04 / $25.20
#     claude-fable-5    →  $10.02 / $50.10
RATES = [
    ("fable-5-1", 5, 25), ("mythos-5-1", 5, 25),
    ("fable-5", 10, 50), ("mythos", 10, 50),
    ("opus-5", 5, 25), ("opus-4-8", 5, 25), ("opus-4-7", 5, 25), ("opus-4-6", 5, 25),
    ("sonnet-5", 2, 10), ("sonnet-4-6", 3, 15), ("haiku", 1, 5),
]
FAST = (10, 50)          # fast 모드(Opus 5/4.8) 프리미엄 요율
DEFAULT = (5, 25)

def rate_for(model, speed):
    if speed == "fast":
        return FAST
    for key, i, o in RATES:
        if key in (model or ""):
            return i, o
    return DEFAULT


def load(path):
    """깨진 서로게이트 페어가 섞인 줄은 건너뛴다."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return out


def session_entries(main_path):
    """메인 기록 + 서브에이전트 기록을 합쳐서 읽는다."""
    entries = load(main_path)
    sub_dir = main_path[:-len(".jsonl")] + "/subagents"
    for p in sorted(glob.glob(os.path.join(sub_dir, "*.jsonl"))):
        entries.extend(load(p))
    return entries


# ────────────────────────────────────────────── 비용

def cost_of(entries):
    """한 응답이 content block 수만큼 여러 줄로 기록되므로 message.id 로 중복 제거."""
    seen, micro = set(), 0.0
    for e in entries:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        u, mid = msg.get("usage"), msg.get("id")
        if not u or mid in seen:
            continue
        seen.add(mid)
        pin, pout = rate_for(msg.get("model"), u.get("speed"))
        cc = u.get("cache_creation") or {}
        micro += (
            u.get("input_tokens", 0) * pin
            + u.get("output_tokens", 0) * pout
            + u.get("cache_read_input_tokens", 0) * pin * 0.1
            + cc.get("ephemeral_1h_input_tokens", 0) * pin * 2      # 1시간 TTL = 입력가 2배
            + cc.get("ephemeral_5m_input_tokens", 0) * pin * 1.25   # 5분 TTL = 1.25배
        )
    return micro / 1_000_000, len(seen)


# ────────────────────────────────────────────── 편집 복원

def edit_events(entries):
    """파일 편집 이벤트를 시간순으로. 타임스탬프 동률은 기록 순서로 깬다."""
    evs = []
    for i, e in enumerate(entries):
        r = e.get("toolUseResult")
        if not isinstance(r, dict) or "structuredPatch" not in r or not r.get("filePath"):
            continue
        evs.append({
            "seq": i,
            "ts": e.get("timestamp") or "",
            "src": e.get("sourceToolAssistantUUID"),   # 이 편집을 시킨 요청
            "path": r["filePath"],
            "orig": r.get("originalFile"),
            "old": r.get("oldString"),
            "new": r.get("newString"),
            "all": bool(r.get("replaceAll")),
            "content": r.get("content"),
            "is_write": "content" in r,
            "patch": r.get("structuredPatch") or [],
        })
    evs.sort(key=lambda x: (x["ts"], x["seq"]))
    return evs


def after_state(ev):
    """편집을 적용한 뒤의 파일 내용. 복원 불가면 None."""
    if ev["is_write"]:
        return ev["content"]
    orig, old, new = ev["orig"], ev["old"], ev["new"]
    if orig is None or old is None or new is None:
        return None
    if ev["all"]:
        return orig.replace(old, new)
    if orig.count(old) != 1:
        return None          # 대상이 유일하지 않으면 어느 쪽인지 알 수 없음
    return orig.replace(old, new, 1)


def diff_lines(before, after):
    """두 내용 사이의 추가/삭제 줄 수."""
    a = before.splitlines() if before else []
    b = after.splitlines() if after else []
    add = dele = 0
    for line in difflib.unified_diff(a, b, n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            dele += 1
    return add, dele


def had_content(ev):
    """이 이벤트 시점에 파일이 이미 있었나. 패치에 삭제줄이 있으면 있던 파일이다."""
    return any(l.startswith("-")
               for h in ev["patch"] for l in (h.get("lines") or []))


def measure(entries):
    """순진한 집계와 살아남은 집계를 같은 잣대로 낸다.

    structuredPatch 는 새 파일 Write 일 때 비어 있어서 그대로 세면 신규 파일이
    0줄로 잡힌다. 그래서 양쪽 다 '내용을 복원해서 diff' 방식으로 통일한다.
      순진한 집계 = 편집마다의 변경량을 전부 더한 값
      살아남은 집계 = 세션 처음 상태와 마지막 상태의 차이
    """
    evs = edit_events(entries)
    by_file = defaultdict(list)
    for ev in evs:
        by_file[ev["path"]].append(ev)

    naive_add = naive_del = 0
    for ev in evs:
        after = after_state(ev)
        if after is None:
            continue
        a, d = diff_lines(ev["orig"] or "", after)
        naive_add += a
        naive_del += d

    surv_add = surv_del = 0
    unresolved = 0
    per_file = []
    for path, group in by_file.items():
        first, last = group[0], group[-1]
        end = after_state(last)
        # originalFile 이 없는데 패치에 삭제줄이 있으면 기존 파일을 덮어쓴 것이다.
        # 시작 상태를 알 수 없으므로 빈 파일로 치면 전체를 신규로 과대계상한다.
        # 복원 불가한 파일은 패치 합계로 대신하지 않고 그냥 뺀다.
        # 대신 세보니 한 번에 만 줄을 지운 패치가 그대로 성과로 들어와,
        # 삭제로 점수를 올리는 구멍이 되살아났다. 못 센 채로 두는 편이 낫다.
        if end is None or (first["orig"] is None and had_content(first)):
            unresolved += 1
            continue
        start = first["orig"] or ""             # null = 세션 중 새로 만든 파일
        a, d = diff_lines(start, end)
        surv_add += a
        surv_del += d
        per_file.append({"path": path, "edits": len(group), "add": a, "del": d})

    return {
        "naive_add": naive_add, "naive_del": naive_del,
        "surv_add": surv_add, "surv_del": surv_del,
        "files": len(by_file), "edits": len(evs),
        "unresolved": unresolved, "per_file": per_file,
    }


def waste_signals(entries):
    err = denial = compact = 0
    for e in entries:
        msg = e.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    err += 1
        if e.get("toolDenialKind"):
            denial += 1
        if e.get("compactMetadata"):
            compact += 1
    return err, denial, compact


# ────────────────────────────────────────────── 낭비를 돈으로 (W3)

def usage_cost(msg):
    u = msg.get("usage") or {}
    pin, pout = rate_for(msg.get("model"), u.get("speed"))
    cc = u.get("cache_creation") or {}
    return (u.get("input_tokens", 0) * pin
            + u.get("output_tokens", 0) * pout
            + u.get("cache_read_input_tokens", 0) * pin * 0.1
            + cc.get("ephemeral_1h_input_tokens", 0) * pin * 2
            + cc.get("ephemeral_5m_input_tokens", 0) * pin * 1.25) / 1_000_000


def cost_index(entries):
    """요청 하나하나의 비용을 낸다.

    한 응답이 여러 줄로 쪼개져 기록되므로 비용은 message.id 당 한 번만 센다.
    도구 결과는 uuid 로 응답을 가리키므로 uuid→message.id 다리도 같이 만든다.
    """
    uuid_to_mid, mid_cost, mid_calls = {}, {}, defaultdict(int)
    for e in entries:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mid, uid = msg.get("id"), e.get("uuid")
        if not mid:
            continue
        if uid:
            uuid_to_mid[uid] = mid
        if msg.get("usage") and mid not in mid_cost:
            mid_cost[mid] = usage_cost(msg)
        for b in (msg.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                mid_calls[mid] += 1
    return uuid_to_mid, mid_cost, mid_calls


def waste_report(entries):
    """낭비를 종류별로 돈으로 환산한다."""
    uuid_to_mid, mid_cost, mid_calls = cost_index(entries)
    total = sum(mid_cost.values())

    def share(entry):
        """도구 호출 하나가 물고 있는 비용. 한 응답이 여러 호출을 하면 나눠 가진다."""
        mid = uuid_to_mid.get(entry.get("sourceToolAssistantUUID"))
        if not mid:
            return 0.0
        return mid_cost.get(mid, 0.0) / max(mid_calls.get(mid, 1), 1)

    err_cost, err_n = 0.0, 0
    den_cost, den_n = 0.0, 0
    for e in entries:
        msg = e.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
                for b in content):
            err_cost += share(e)
            err_n += 1
        if e.get("toolDenialKind"):
            den_cost += share(e)
            den_n += 1

    # 재작업: 파일별로 살아남지 못한 비율만큼 그 파일 편집 비용에서 떼어낸다.
    evs = edit_events(entries)
    by_file = defaultdict(list)
    for ev in evs:
        by_file[ev["path"]].append(ev)
    rework_cost = 0.0
    naive_all = surv_all = 0
    for path, group in by_file.items():
        n_add = n_del = 0
        for ev in group:
            after = after_state(ev)
            if after is None:
                continue
            a, d = diff_lines(ev["orig"] or "", after)
            n_add += a
            n_del += d
        naive = n_add + n_del
        end = after_state(group[-1])
        if end is None or (group[0]["orig"] is None and had_content(group[0])):
            continue
        a, d = diff_lines(group[0]["orig"] or "", end)
        surv = a + d
        naive_all += naive
        surv_all += surv
        if naive > surv:
            wasted_ratio = 1 - (surv / naive)
            spent = sum(share({"sourceToolAssistantUUID": ev["src"]}) for ev in group)
            rework_cost += spent * wasted_ratio

    # 컨텍스트 비대: 매 요청이 다시 읽는 양이 중앙값에서 멈췄다면 얼마였을까.
    # 세션을 더 일찍 끊었을 때 아꼈을 돈의 대략치다.
    per_req = []
    seen = set()
    for e in entries:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mid, u = msg.get("id"), msg.get("usage")
        if not u or mid in seen:
            continue
        seen.add(mid)
        pin, _ = rate_for(msg.get("model"), u.get("speed"))
        per_req.append((u.get("cache_read_input_tokens", 0), pin))
    bloat = 0.0
    if per_req:
        vals = sorted(r for r, _ in per_req)
        med = vals[len(vals) // 2]
        bloat = sum(max(0, r - med) * pin * 0.1 for r, pin in per_req) / 1_000_000

    return {
        "total": total,
        "rework": rework_cost, "rework_ratio": (1 - surv_all / naive_all) if naive_all else 0,
        "errors": err_cost, "errors_n": err_n,
        "denials": den_cost, "denials_n": den_n,
        "bloat": bloat,
        "compacts": sum(1 for e in entries if e.get("compactMetadata")),
    }


# ────────────────────────────────────────────── 활동별 비용 분해 (W2)

# 세션에 이름표를 하나 붙이려 했지만 실데이터가 거의 다 섞여 있었다.
# 한 세션에서 코드 짜고, 브라우저로 확인하고, 고치는 걸 다 한다.
# 그래서 분류 대신 돈이 어느 활동으로 갔는지를 쪼갠다.
ACTIVITY = {
    "쓰기": {"Edit", "Write", "NotebookEdit"},
    "읽기": {"Read", "Grep", "Glob", "ToolSearch"},
    "조사": {"WebSearch", "WebFetch"},
    "실행": {"Bash", "BashOutput", "KillShell"},
    "위임": {"Agent", "Workflow", "SendMessage", "ListAgents"},
    "대화": {"AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "Skill"},
}


# Bash 는 뭐든 다 한다. 명령어를 보고 나눠야 실제 활동이 잡힌다.
# 실측: 호출의 28%가 탐색·검색(Read·Grep 과 같은 일), 23%가 빌드·테스트·git.
_BASH_READ = re.compile(
    r"^(ls|find|cat|head|tail|wc|tree|du|stat|file|pwd|which|grep|rg|ag|jq|sort|uniq|diff)\b")
_BASH_BUILD = re.compile(
    r"^(npm|npx|yarn|pnpm|bun)\s+(run\s+)?(build|test|lint|typecheck|tsc|dev|start|serve)"
    r"|^(pytest|go\s+test|cargo\s+(test|build)|make|tsc)\b"
    r"|^git\s|^(npm|yarn|pnpm|bun|pip3?)\s+(i|install|add)\b|^(npx\s+)?vercel\b")
_CD_PREFIX = re.compile(r'^cd\s+(?:"[^"]*"|\'[^\']*\'|\S+)\s*&&\s*(.*)$', re.S)


def _bash_core(cmd):
    """cd ... && 접두사를 벗겨 실제 명령을 꺼낸다. 이걸 안 하면 90%가 미분류가 된다."""
    c = (cmd or "").strip()
    while True:
        m = _CD_PREFIX.match(c)
        if not m:
            return c
        c = m.group(1).strip()


def activity_of(tool_name, tool_input=None):
    if tool_name.startswith("mcp__claude-in-chrome__") or tool_name.startswith("mcp__playwright__"):
        return "브라우저"
    if tool_name == "Bash":
        c = _bash_core((tool_input or {}).get("command", ""))
        if _BASH_BUILD.search(c):
            return "빌드·검증"     # 코드를 만드는 일의 일부로 본다
        if _BASH_READ.search(c):
            return "읽기"
        return "실행"
    for name, tools in ACTIVITY.items():
        if tool_name in tools:
            return name
    return "기타"


def cost_by_activity(entries):
    """요청 비용을 그 요청이 부른 도구들에게 나눠 주고, 활동별로 합친다.

    비용의 대부분은 매 요청마다 대화를 다시 읽는 값이라 요청 수에 거의 비례한다.
    그래서 요청 비용을 그 요청의 도구 호출 수로 나누는 근사가 쓸 만하다.
    도구를 하나도 안 부른 응답(사용자에게 말만 한 턴)은 '대화'로 친다.
    """
    _, mid_cost, _ = cost_index(entries)
    # 한 응답이 여러 줄로 쪼개지면 도구 호출도 그 줄들에 나뉘어 담긴다.
    # 첫 줄만 보면 호출을 놓치므로 message.id 로 먼저 모은다.
    calls_by_mid = defaultdict(list)
    for e in entries:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mid = msg.get("id")
        if not mid:
            continue
        for b in (msg.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                calls_by_mid[mid].append((b.get("name", ""), b.get("input")))

    by_act = defaultdict(float)
    for mid, cost in mid_cost.items():
        names = calls_by_mid.get(mid) or []
        if not names:
            by_act["대화"] += cost          # 도구 없이 사용자에게 말만 한 턴
            continue
        each = cost / len(names)
        for n, inp in names:
            by_act[activity_of(n, inp)] += each
    return dict(by_act)


# ────────────────────────────────────────────── 출력

def project_dir(target=None):
    """분석할 프로젝트의 기록 폴더. target 이 없으면 현재 위치 기준."""
    # macOS 는 한글 경로를 자모로 분해(NFD)해 돌려주므로 합쳐서(NFC) 비교·변환한다.
    # 안 하면 '피'가 'ㅍ'+'ㅣ' 두 글자로 세어져 대시 개수가 어긋난다.
    here = unicodedata.normalize("NFC", os.path.abspath(target or os.getcwd()))
    # isalnum() 은 한글도 문자로 보므로 ascii 검사를 같이 건다.
    guess = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in here)
    root = os.path.expanduser("~/.claude/projects")
    cand = os.path.join(root, guess)
    if os.path.isdir(cand):
        return cand
    for d in sorted(glob.glob(os.path.join(root, "*/"))):     # 기록 안의 cwd 로 역추적
        files = sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime, reverse=True)
        for f in files[:3]:
            for e in load(f):
                c = e.get("cwd")
                if c and unicodedata.normalize("NFC", c) == here:
                    return d.rstrip("/")
    return cand


def print_waste(paths):
    """세션별 낭비 진단을 돈으로 보여준다."""
    print(f"\n\033[1m낭비 진단\033[0m  —  {globals().get('_LABEL', os.getcwd())}\n")
    agg = defaultdict(float)
    for p in paths:
        entries = session_entries(p)
        if not entries:
            continue
        w = waste_report(entries)
        if w["total"] < 0.01:
            continue
        for k in ("total", "rework", "errors", "denials", "bloat"):
            agg[k] += w[k]
        sid = os.path.basename(p)[:8]
        print(f"\033[1m{sid}\033[0m   총 ${w['total']:.2f}")

        rows = [
            ("재작업",       w["rework"],  f"편집의 {w['rework_ratio']*100:.0f}%가 나중에 덮어써짐"),
            ("실패한 호출",   w["errors"],  f"{w['errors_n']}번 실패"),
            ("거부당한 호출", w["denials"], f"{w['denials_n']}번 거부"),
        ]
        recoverable = 0.0
        for name, cost, note in rows:
            if cost < 0.005 and "0번" in note:
                continue
            pct = cost / w["total"] * 100 if w["total"] else 0
            recoverable += cost
            print(f"    {name:<12} ${cost:>7.2f}  {pct:>4.0f}%   {note}")
        print(f"    {'─'*46}")
        print(f"    {'되찾을 수 있던 돈':<12} ${recoverable:>7.2f}  "
              f"{recoverable/w['total']*100 if w['total'] else 0:>4.0f}%")
        print(f"    \033[2m컨텍스트 비대  ${w['bloat']:>7.2f}  "
              f"{w['bloat']/w['total']*100 if w['total'] else 0:>4.0f}%   "
              f"대화가 길어져 매번 더 읽은 값 (위 항목과 겹침)"
              + (f" · 압축 {w['compacts']}회" if w["compacts"] else "") + "\033[0m")
        print()

    t = agg["total"]
    if not t:
        return
    print("═" * 62)
    print(f"\033[1m전체 ${t:.2f}\033[0m 중")
    for name, key in (("재작업", "rework"), ("실패한 호출", "errors"),
                      ("거부당한 호출", "denials"), ("컨텍스트 비대", "bloat")):
        print(f"  {name:<14} ${agg[key]:>7.2f}   {agg[key]/t*100:>4.1f}%")
    small = agg["rework"] + agg["errors"] + agg["denials"]
    print(f"\n  \033[2m재작업·실패·거부를 다 합쳐도 {small/t*100:.1f}%. "
          f"컨텍스트 비대가 {agg['bloat']/t*100:.1f}%로 그 {agg['bloat']/small:.0f}배다.\033[0m")


ACT_ORDER = ["쓰기", "빌드·검증", "읽기", "조사", "실행", "브라우저", "위임", "대화", "기타"]


# ────────────────────────────────────────────── 내보내기 (W4)
#
# 편집 기록에는 소스코드 본문(originalFile·oldString·newString·content)과 파일
# 경로가 그대로 들어 있다. 순위 기능을 만들려면 서버로 뭔가 보내야 하는데,
# 전체 기록에서 위험한 걸 지우는 방식(금지 목록)은 스키마가 바뀔 때마다 샌다.
# 그래서 반대로 간다: 필요한 숫자만 새로 담고, 담은 것만 나간다.

def _salt():
    """기기마다 고정된 소금. 같은 프로젝트는 늘 같은 ID 가 되지만 되돌릴 수는 없다."""
    p = os.path.expanduser("~/.claude/.cc-value-salt")
    try:
        with open(p) as f:
            s = f.read().strip()
            if s:
                return s
    except OSError:
        pass
    s = os.urandom(16).hex()
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(s)
    except OSError:
        pass
    return s


def anon(value):
    """되돌릴 수 없는 안정 ID. 경로나 세션 ID 를 그대로 내보내지 않기 위한 것."""
    import hashlib
    return hashlib.sha256((_salt() + "|" + str(value)).encode()).hexdigest()[:16]


def build_export(paths):
    """보낼 payload 를 숫자만으로 새로 짓는다. 원본 기록을 걸러 만드는 게 아니다."""
    sessions = []
    for p in paths:
        entries = session_entries(p)
        if not entries:
            continue
        cost, reqs = cost_of(entries)
        if cost < 0.01:
            continue
        m = measure(entries)
        w = waste_report(entries)
        act = cost_by_activity(entries)
        err, denial, compact = waste_signals(entries)

        tok = defaultdict(int)
        models = defaultdict(int)
        seen = set()
        first_ts = None
        for e in entries:
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            u, mid = msg.get("usage"), msg.get("id")
            if not u or mid in seen:
                continue
            seen.add(mid)
            models[canon_model(msg.get("model"))] += 1
            cc = u.get("cache_creation") or {}
            tok["input"] += u.get("input_tokens", 0)
            tok["output"] += u.get("output_tokens", 0)
            tok["thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
            tok["cache_read"] += u.get("cache_read_input_tokens", 0)
            tok["cache_write_1h"] += cc.get("ephemeral_1h_input_tokens", 0)
            tok["cache_write_5m"] += cc.get("ephemeral_5m_input_tokens", 0)
            ts = e.get("timestamp")
            if ts and (first_ts is None or ts < first_ts):
                first_ts = ts

        sessions.append({
            "id": anon(os.path.basename(p)),
            "started_hour": (first_ts or "")[:13] + ":00:00Z" if first_ts else None,
            "models": dict(models),
            "cost_usd": round(cost, 4),
            "requests": reqs,
            "tokens": dict(tok),
            "lines": {"naive_add": m["naive_add"], "naive_del": m["naive_del"],
                      "surv_add": m["surv_add"], "surv_del": m["surv_del"]},
            "files_measured": len(m["per_file"]),
            "files_unmeasured": m["unresolved"],
            "edits": m["edits"],
            "activity_share": {k: round(v / cost, 4) for k, v in sorted(act.items()) if cost},
            "waste_usd": {"rework": round(w["rework"], 4), "errors": round(w["errors"], 4),
                          "denials": round(w["denials"], 4), "bloat": round(w["bloat"], 4)},
            "counts": {"errors": err, "denials": denial, "compacts": compact},
        })

    return {
        "schema": 1,
        "project": anon(globals().get("_LABEL", os.getcwd())),
        "sessions": sessions,
    }


# 문자열은 "모양"이 아니라 "값"으로 검사한다. 모양으로 하면(예: 소문자+점+대시)
# 파일명 storage.ts, 원본 세션ID, sk-ant-... 같은 게 전부 통과해버린다. 실제로 뚫렸다.
_OK_MODELS = {k for k, _, _ in RATES} | {"other"}
_OK_WORDS = set(ACT_ORDER)
_OK_HOUR = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$")
_OK_ID = re.compile(r"^[0-9a-f]{16}$")
_OK_KEY = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def canon_model(model):
    """모델명을 아는 값으로만 접는다. 모르는 건 other. 나갈 수 있는 문자열이 10개뿐이 된다."""
    for key, _, _ in RATES:
        if key in (model or ""):
            return key
    return "other"


def audit_export(payload):
    """payload 에 숫자·허용된 문자열 말고 다른 게 섞였는지 훑는다.

    키 이름이 아니라 값의 모양을 본다. 스키마가 바뀌어 새 필드가 딸려 들어와도
    경로나 코드 조각이면 여기서 걸린다.
    """
    problems = []

    def ok_key(k):
        return isinstance(k, str) and (
            _OK_KEY.match(k) or k in _OK_WORDS or k in _OK_MODELS)

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if not ok_key(k):
                    problems.append((path, f"허용되지 않은 키: {str(k)[:50]}"))
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, bool) or isinstance(node, (int, float)) or node is None:
            return
        elif isinstance(node, str):
            if (node in _OK_MODELS or node in _OK_WORDS
                    or _OK_HOUR.match(node) or _OK_ID.match(node)):
                return
            problems.append((path, f"허용되지 않은 문자열: {node[:50]}"))
        else:
            problems.append((path, f"알 수 없는 타입: {type(node).__name__}"))

    walk(payload, "$")
    return problems


def print_export(paths):
    payload = build_export(paths)
    problems = audit_export(payload)
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    print(f"\n\033[1m내보낼 데이터 전부\033[0m  ({len(text):,}자, 세션 {len(payload['sessions'])}개)\n")
    print(text)

    print(f"\n\033[1m유출 검사\033[0m")
    if problems:
        print(f"  \033[31m{len(problems)}건 걸림 — 내보내면 안 됩니다\033[0m")
        for path, why in problems[:20]:
            print(f"    {path}  {why}")
    else:
        print("  \033[32m통과\033[0m — 숫자와 허용된 문자열만 있습니다")
        print("  경로·파일명·코드 조각·명령어·프롬프트는 하나도 담기지 않았습니다")


def print_mix(paths):
    """돈이 어느 활동으로 갔는지, 그리고 코딩 몫만 따진 효율."""
    print(f"\n\033[1m돈이 어디로 갔나\033[0m  —  {globals().get('_LABEL', os.getcwd())}\n")
    head = "".join(f"{a:>8}" for a in ACT_ORDER)
    print(f"{'세션':<10}{'비용':>9}{head}")
    print("─" * (19 + 8 * len(ACT_ORDER)))

    rows = []
    for p in paths:
        entries = session_entries(p)
        if not entries:
            continue
        cost, _ = cost_of(entries)
        if cost < 0.01:
            continue
        act = cost_by_activity(entries)
        m = measure(entries)
        rows.append((os.path.basename(p)[:8], cost, act, m))

    for sid, cost, act, m in sorted(rows, key=lambda r: -r[1]):
        cells = "".join(
            f"{(str(round(act.get(a, 0) / cost * 100)) + '%') if act.get(a, 0) / cost >= 0.005 else '·':>8}"
            for a in ACT_ORDER)
        print(f"{sid:<10}${cost:>8.2f}{cells}")

    print("\n\033[1m코딩 효율 — 코드를 만드는 데 쓴 돈만 따로 떼서\033[0m\n")
    print(f"{'세션':<10}{'전체비용':>10}{'코딩몫':>9}{'비중':>6}"
          f"{'살아남은줄':>10}{'전체기준':>10}{'코딩몫기준':>11}")
    print("─" * 68)
    for sid, cost, act, m in sorted(rows, key=lambda r: -r[1]):
        surv = m["surv_add"] + m["surv_del"]
        # 코드를 만드는 일 = 고치기 + 빌드·테스트·git + 그러려고 읽기.
        # 브라우저 검증과 외부 조사는 뺀다.
        coding = act.get("쓰기", 0) + act.get("빌드·검증", 0) + act.get("읽기", 0)
        if surv == 0:
            print(f"{sid:<10}${cost:>9.2f}${coding:>8.2f}{coding/cost*100:>5.0f}%"
                  f"{'—':>10}{'측정 불가':>12}")
            continue
        print(f"{sid:<10}${cost:>9.2f}${coding:>8.2f}{coding/cost*100:>5.0f}%"
              f"{surv:>10}{'$'+format(cost/surv,'.4f'):>10}"
              f"{'$'+format(coding/surv,'.4f'):>11}")
    print(f"\n\033[2m전체기준 = 세션 비용을 전부 코드에 물린 값 (전 단계 방식)")
    print(f"코딩몫기준 = 고치기 + 빌드·테스트·git + 그러려고 읽기에 쓴 돈만 물린 값.")
    print(f"             브라우저 검증·외부 조사·분류 안 된 셸 작업은 뺐다\033[0m")


def opt(name, default=None):
    """--이름 값 형태의 인자를 꺼낸다."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
            return sys.argv[i + 1]
    return default


def main():
    flags = {"--files", "--waste", "--mix", "--export", "--all", "--project"}
    args, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "--project":
            skip = True
            continue
        if a.startswith("--"):
            continue
        args.append(a)
    detail = "--files" in sys.argv

    # 이 도구는 claudeusage 폴더에 있지만 분석 대상은 보통 다른 프로젝트다.
    if "--all" in sys.argv:
        root = os.path.expanduser("~/.claude/projects")
        paths = sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))
        label = "모든 프로젝트"
        if not paths:
            sys.exit(f"세션 기록을 못 찾음: {root}")
    else:
        target = opt("--project")
        d = project_dir(target)
        if not os.path.isdir(d):
            sys.exit(f"세션 기록을 못 찾음: {d}\n"
                     f"다른 프로젝트를 보려면  --project <폴더경로>  또는  --all")
        label = os.path.abspath(target) if target else os.getcwd()
        paths = ([os.path.join(d, a + ".jsonl") for a in args] if args
                 else sorted(glob.glob(os.path.join(d, "*.jsonl"))))

    globals()["_LABEL"] = label

    if "--waste" in sys.argv:
        print_waste(paths)
        return
    if "--mix" in sys.argv:
        print_mix(paths)
        return
    if "--export" in sys.argv:
        print_export(paths)
        return

    print(f"\n\033[1m쓴 돈 대비 살아남은 결과\033[0m  —  {globals().get('_LABEL', os.getcwd())}\n")
    print(f"{'세션':<10}{'비용':>9}{'편집':>6}"
          f"{'  순진하게 센 것':>18}{'  살아남은 것':>16}{'   $/살아남은줄':>16}{'  에러':>6}")
    print("─" * 84)

    tot_cost = tot_naive = tot_surv = 0.0
    rows = []
    for p in paths:
        entries = session_entries(p)
        if not entries:
            continue
        cost, reqs = cost_of(entries)
        m = measure(entries)
        err, denial, compact = waste_signals(entries)
        naive = m["naive_add"] + m["naive_del"]
        surv = m["surv_add"] + m["surv_del"]
        per = cost / surv if surv else 0
        rows.append((os.path.basename(p)[:8], cost, m, naive, surv, per, err, denial, compact))
        tot_cost += cost
        tot_naive += naive
        tot_surv += surv

    for sid, cost, m, naive, surv, per, err, denial, compact in sorted(rows, key=lambda r: -r[1]):
        shrink = f"({surv/naive*100:.0f}%)" if naive else "     "
        print(f"{sid:<10}${cost:>8.2f}{m['edits']:>6}"
              f"{'+'+str(m['naive_add']):>9}{'-'+str(m['naive_del']):>8}"
              f"{'+'+str(m['surv_add']):>8}{'-'+str(m['surv_del']):>7}"
              f"{shrink:>7}{('$'+format(per,'.4f')) if surv else '—':>10}{err:>6}")

    print("─" * 84)
    keep = f"{tot_surv/tot_naive*100:.0f}%" if tot_naive else "—"
    print(f"{'합계':<10}${tot_cost:>8.2f}{'':>6}"
          f"{int(tot_naive):>17}{int(tot_surv):>15}{keep:>7}"
          f"{('$'+format(tot_cost/tot_surv,'.4f')) if tot_surv else '—':>10}")
    print(f"\n\033[2m순진하게 센 것 = 편집 이벤트를 전부 더한 값 (기존 방식)")
    print(f"살아남은 것   = 세션 시작·끝 상태를 복원해 그 차이만 센 값 (W1)\033[0m")

    if detail and rows:
        for sid, cost, m, *_ in rows:
            if not m["per_file"]:
                continue
            print(f"\n\033[1m{sid} 파일별\033[0m")
            for f in sorted(m["per_file"], key=lambda x: -(x["add"] + x["del"]))[:12]:
                print(f"  {os.path.basename(f['path']):<38}"
                      f"{f['edits']:>3}회 편집   +{f['add']:<6}-{f['del']}")
            if m["unresolved"]:
                print(f"  \033[2m복원 실패 {m['unresolved']}개 파일은 제외\033[0m")


if __name__ == "__main__":
    main()
