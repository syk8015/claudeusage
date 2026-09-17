#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-usage.py — 앤트로픽이 직접 알려주는 한도 수치를 받아 기록한다.

상태바가 주는 한도값은 **정수 %** 가 한계다. 1%p 가 해상도 바닥이라 변화점 하나하나가
거칠고, 모델별 주간 한도(weekly_scoped)는 아예 안 온다. 같은 값의 원본인
`api.anthropic.com/api/oauth/usage` 는 둘 다 준다.

    claudeusage usage             지금 한도를 보여준다
    claudeusage usage --log       data/usage-log.jsonl 에 한 줄 덧붙인다
    claudeusage usage --shape     응답에 뭐가 들어 있는지 구조만 (값 없이)
    claudeusage usage --tail 20   쌓인 기록 마지막 20줄

토큰 취급:
  맥 로그인 키체인의 "Claude Code-credentials" 에서 그때그때 읽어 앤트로픽에만 보낸다.
  화면에 찍지 않고 파일에 쓰지 않는다. 기록에 남기는 건 아래 허용 목록뿐이다.

기록에 남기는 것 (금지 목록이 아니라 허용 목록. W4 의 교훈):
  받은 시각 · 한도 종류 · 대상 모델 이름 · 소진율 · 리셋 시각.
  제품별 분해(Claude Code / 채팅 / Cowork)의 이름과 비율.
  계정 ID, 조직 ID, 이메일 같은 건 애초에 담지 않는다.

제품별 분해(seven_day_breakdown)가 이 도구의 가장 큰 값어치다. 우리는 로컬 로그로
"한도의 10% 는 클로드코드 밖에서 먹었다"를 추정했는데(README.ko.md 한도 절 5번), 여기엔
**앤트로픽이 직접 계산한 정답**이 온다. 2026-09-16 첫 확인: 클로드코드 91% · 채팅 9%.
다만 주간 창 하나치만 오고 지나가면 사라지므로 --log 로 모아 둬야 한다.
"""

import json, os, sys, subprocess, urllib.request, urllib.error

from .i18n import t, is_ko
from . import paths

LOG = paths.usage_log()

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"

B, D, R, Y, X = "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[0m"

# 기록에 나갈 수 있는 필드. 여기 없는 건 안 나간다.
# coarse_percent 는 limits[] 가 주는 정수값. 실수값과 정말 다른지 대조하려고 같이 남긴다.
KEEP_LIMIT = ("kind", "group", "model", "percent", "coarse_percent",
              "resets_at", "severity", "is_active")
KEEP_SHARE = ("key", "name", "percent")


def token():
    """키체인에서 액세스 토큰을 읽는다. 돌려주기만 하고 어디에도 남기지 않는다."""
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit("%s%s%s" % (R, t("Could not read the keychain: %s",
                                          "키체인을 못 읽었다: %s") % e, X))
    if raw.returncode != 0:
        raise SystemExit("%s%s%s" % (R, t('No "%s" entry in the keychain.',
                                          '키체인에 "%s" 항목이 없다.') % KEYCHAIN_SERVICE, X))
    try:
        d = json.loads(raw.stdout)
    except ValueError:
        raise SystemExit("%s%s%s" % (R, t("The keychain value is not JSON.",
                                          "키체인 값이 JSON 이 아니다."), X))
    o = d.get("claudeAiOauth") or d
    tok = o.get("accessToken")
    if not tok:
        raise SystemExit("%s%s%s" % (R, t("No accessToken. You may need to sign in to Claude Code again.",
                                          "accessToken 이 없다. 클로드코드에 다시 로그인해야 할 수 있다."), X))
    return tok


def fetch():
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token(),
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            raise SystemExit("%s%s%s" % (R, t("The token has expired (401). Using Claude Code once refreshes it.",
                                              "토큰이 만료됐다(401). 클로드코드를 한 번 쓰면 갱신된다."), X))
        raise SystemExit("%s%s%s" % (R, t("Request failed %s: %s",
                                          "요청 실패 %s: %s") % (e.code, body), X))
    except urllib.error.URLError as e:
        raise SystemExit("%s%s%s" % (R, t("Connection failed: %s",
                                          "연결 실패: %s") % e.reason, X))


# ────────────────────────────────────────────── 응답 정규화

def to_epoch(v):
    """resets_at 을 초 단위 숫자로. 실제 응답은 ISO 문자열로 온다.

    상태바는 epoch 정수를 주는데 이 엔드포인트는 '2026-09-04T08:00:00.000000+00:00'
    같은 32자 문자열을 준다. 같은 값인데 모양이 달라서 맞춰 줘야 cc-limit.py 가 쓴다.
    """
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            import datetime
            d = datetime.datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            return d.timestamp()
        except ValueError:
            continue
    return None


def limits_of(data):
    """응답에서 한도 목록만 꺼낸다.

    실측한 응답에는 두 표현이 같이 들어 있다.
      limits[] — kind/group/severity/scope 가 붙지만 percent 가 **정수**
      five_hour·seven_day — utilization 이 **실수**
    같은 한도를 두 번 말하는 것이므로 합쳐서, 소진율은 실수 쪽을 우선한다.
    실수 쪽이 실제로 더 정밀한지는 표본이 쌓여야 안다.
    """
    fine = {}
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        w = data.get(key)
        if isinstance(w, dict):
            fine[kind] = _num(w.get("utilization"), w.get("used_percentage"))

    out, seen = [], set()
    for lim in (data.get("limits") or []):
        if not isinstance(lim, dict):
            continue
        kind = lim.get("kind")
        seen.add(kind)
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        out.append({
            "kind": kind,
            "group": lim.get("group"),
            "model": model,
            "percent": _num(fine.get(kind), lim.get("percent"), lim.get("utilization")),
            "coarse_percent": _num(lim.get("percent")),
            "resets_at": to_epoch(lim.get("resets_at")),
            "severity": lim.get("severity"),
            "is_active": lim.get("is_active"),
        })

    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        w = data.get(key)
        if isinstance(w, dict) and kind not in seen:
            out.append({"kind": kind, "group": None, "model": None,
                        "percent": fine.get(kind), "coarse_percent": None,
                        "resets_at": to_epoch(w.get("resets_at")),
                        "severity": None, "is_active": None})

    # 나머지 한도. 응답에는 seven_day_opus 같은 이름 말고 nimbus_quill 처럼 뜻 모를
    # 이름도 섞여 있고 대부분 null 이다. 이름을 미리 정해 두면 새 게 생겼을 때 놓치므로,
    # utilization 을 가진 딕셔너리는 전부 담는다. 돈 얘기(extra_usage·spend)는 뺀다.
    for key, w in data.items():
        if key in ("five_hour", "seven_day", "extra_usage", "spend", "limits"):
            continue
        if not isinstance(w, dict) or _num(w.get("utilization")) is None:
            continue
        out.append({"kind": key, "group": "other",
                    "model": key.replace("seven_day_", "") if key.startswith("seven_day_") else None,
                    "percent": _num(w.get("utilization")), "coarse_percent": None,
                    "resets_at": to_epoch(w.get("resets_at")),
                    "severity": None, "is_active": None})
    return out


def breakdown_of(data):
    """주간 한도를 제품별로 쪼갠 것. 채팅이 한도를 얼마나 먹었나의 정답이다.

    응답 모양: seven_day_breakdown = {as_of, window_started_at, rows:[{key, display_name,
    percent}]}. percent 는 **주간 한도 소진분 중의 비중**이지 주간 한도의 % 가 아니다.
    즉 주간이 84% 찼고 채팅이 9% 면, 채팅이 먹은 건 주간 한도의 약 7.6%p 다.
    (rows 합이 100 인 것으로 확인한다 — 아니면 뜻이 다른 것이므로 그대로 두고 기록만 한다.)
    """
    bd = data.get("seven_day_breakdown")
    if not isinstance(bd, dict):
        return None
    rows = []
    for r in (bd.get("rows") or []):
        if not isinstance(r, dict):
            continue
        p = _num(r.get("percent"), r.get("utilization"))
        if p is None:
            continue
        rows.append({"key": r.get("key"), "name": r.get("display_name"), "percent": p})
    if not rows:
        return None
    return {"as_of": to_epoch(bd.get("as_of")),
            "window_started_at": to_epoch(bd.get("window_started_at")),
            "rows": rows}


def _num(*vals):
    for v in vals:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
    return None


def precision_note(limits):
    """소수점이 오는지 본다. 이 도구를 만든 이유가 그거였다.

    2026-09-04 첫 실행 결과: 안 온다. five_hour.utilization 이 float 타입이긴 한데
    값은 26.0 이었고, limits[].percent 는 아예 int 다. 같은 시각 상태바도 26~27 이었다.
    상태바 표본 5,537개도 전부 정수였다(28.000000000000004 는 28 의 부동소수 찌꺼기).

    즉 **엔드포인트가 더 정밀하지 않다.** 1% 가 양쪽 다 바닥이다. 한도 분석을 더
    날카롭게 하려면 더 정밀한 눈금이 아니라 더 긴 기간이 필요하다.
    이 도구의 남은 값어치는 모델별 한도(group=scoped)와, 세션별 낡은 스냅샷이 섞이지
    않은 깨끗한 한 번의 읽기다.
    """
    nums = [l["percent"] for l in limits if l["percent"] is not None]
    if not nums:
        return t("Could not read any percentage", "소진율을 못 읽었다")
    if [n for n in nums if abs(n - round(n)) > 1e-9]:
        return t("Fractional values arrived — finer than the status line (integers). Worth recording.",
                 "소수점이 왔다. 상태바(정수 %)보다 정밀하다 — 기록해 둘 것")
    return t("All integers — same resolution as the status line, as on the first run. No precision "
             "to gain here. The value is the per-model limit and one clean, unmixed reading.",
             "전부 정수다. 상태바와 같은 해상도이고, 첫 실행 때도 그랬다. "
             "정밀도로는 얻을 게 없다. 쓸모는 모델별 한도와 낡은 값 안 섞인 읽기.")


# ────────────────────────────────────────────── 출력

def show(data):
    lims = limits_of(data)
    if not lims:
        print("%s%s%s" % (Y, t("No limit entries found. Try --shape to see the response structure.",
                               "한도 항목을 못 찾았다. --shape 로 응답 구조를 보라."), X))
        return
    print("\n%s%s%s" % (B, t("Limits right now", "지금 한도"), X))
    print("%s%s%s" % (D, t("kind                 group    scope       used   coarse  active    resets in",
                           "종류                 묶음     대상      소진율  거친값  살아있나  리셋까지"), X))
    import time
    now = time.time()
    for l in sorted(lims, key=lambda x: (x["kind"] or "", x["model"] or "")):
        left = "-"
        if isinstance(l["resets_at"], (int, float)):
            s = l["resets_at"] - now
            left = (t("%dh %dm", "%d시간 %d분") % (s // 3600, (s % 3600) // 60)
                    if s > 0 else t("passed", "지남"))
        pct = "%9.4f%%" % l["percent"] if l["percent"] is not None else "        -"
        coarse = "%5.0f%%" % l["coarse_percent"] if l["coarse_percent"] is not None else "    -"
        act = {True: t("yes", "예"), False: t("no", "아니오"), None: "-"}.get(l.get("is_active"), "-")
        print("%-20s %-8s %-9s %s %s  %-8s %s"
              % (l["kind"] or "?", l.get("group") or "-", l["model"] or t("all", "전체"),
                 pct, coarse, act, left))
    print("\n%s%s%s" % (D, precision_note(lims), X))
    show_breakdown(data, lims)


def show_breakdown(data, lims):
    """제품별 분해. 비중(%)과, 주간 한도로 환산한 %p 를 같이 보여준다."""
    bd = breakdown_of(data)
    if not bd:
        print("%s%s%s" % (Y, t("No per-product breakdown (seven_day_breakdown) in the response.",
                               "제품별 분해(seven_day_breakdown)가 응답에 없다."), X))
        return
    weekly = next((l["percent"] for l in lims
                   if l["kind"] == "weekly_all" and l["percent"] is not None), None)
    tot = sum(r["percent"] for r in bd["rows"])
    print("\n%s%s%s   %s %s"
          % (B, t("Weekly limit, by product", "주간 한도를 제품별로"), X,
             t("window opened", "창 시작"), _utc(bd["window_started_at"])))
    for r in sorted(bd["rows"], key=lambda r: -r["percent"]):
        bar = "█" * int(round(r["percent"] / 4))
        extra = ""
        if weekly is not None and abs(tot - 100) < 1e-6:
            extra = t("   %.1f%%p of the weekly limit", "   주간 한도의 %.1f%%p") % (
                weekly * r["percent"] / 100.0)
        print("      %-12s %5.1f%%  %-25s%s" % (r["name"] or r["key"], r["percent"], bar, extra))
    if abs(tot - 100) > 1e-6:
        print("%s      %s%s"
              % (Y, t("Rows sum to %.1f, so this may not be 'share of what was burned'. Re-check the meaning.",
                      "합이 %.1f 이라 '소진분 중 비중'이 아닐 수 있다. 뜻을 다시 볼 것.") % tot, X))


def _utc(t):
    import time
    return time.strftime("%m-%d %H:%MZ", time.gmtime(t)) if t else "-"


def shape(o, path="", depth=0):
    """값 대신 구조만 찍는다. 응답에 뭐가 들어 있는지 안전하게 보려고.

    리스트는 항목을 **전부** 편다. 첫 항목만 보면 limits[] 안에서 모델별 한도가
    몇 번째에 들어 있는지 놓친다(처음에 그렇게 만들어서 실제로 놓쳤다).
    """
    if depth > 7:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            shape(v, path + "." + str(k), depth + 1)
    elif isinstance(o, list):
        print("  %s[]  항목 %d개" % (path, len(o)))
        for i, v in enumerate(o):
            shape(v, "%s[%d]" % (path, i), depth + 1)
    else:
        t = type(o).__name__
        if o is None or isinstance(o, (bool, int, float)):
            print("  %s: %s = %s" % (path, t, o))
        else:
            s = str(o)
            # 문자열 값은 안 찍는다. --shape 는 "구조만" 보는 용도인데 값을 찍으면
            # 계정·조직 식별자가 화면에 나올 수 있다. 시각처럼 모양만 알면 되는 건
            # 길이로 충분하다(보안 점검에서 잡힌 것, 2026-09-17).
            print("  %s: %s (%d자)" % (path, t, len(s)))


def append_log(data):
    import time
    row = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "limits": [{k: l.get(k) for k in KEEP_LIMIT} for l in limits_of(data)]}
    bd = breakdown_of(data)
    if bd:
        row["breakdown"] = {"window_started_at": bd["window_started_at"],
                            "rows": [{k: r.get(k) for k in KEEP_SHARE} for r in bd["rows"]]}
    if not row["limits"]:
        print("%s%s%s" % (Y, t("Limit list was empty, nothing recorded.",
                               "한도 항목이 비어 기록하지 않았다."), X))
        return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    n = sum(1 for _ in open(LOG, encoding="utf-8", errors="replace"))
    print(t("Appended 1 sample → %s (%d total)", "기록 1줄 추가 → %s (총 %d줄)")
          % (LOG, n))


def tail(n):
    if not os.path.exists(LOG):
        print("%s%s%s" % (Y, t("No samples yet. Start collecting with --log.",
                               "아직 기록이 없다. --log 로 모으기 시작하라."), X))
        return
    lines = open(LOG, encoding="utf-8", errors="replace").readlines()
    shown, total = min(n, len(lines)), len(lines)
    print("%s%s%s" % (D, t("last %d of %d samples", "총 %d줄 중 마지막 %d줄")
                      % ((shown, total) if not is_ko() else (total, shown)), X))
    for line in lines[-n:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        bits = " · ".join(
            "%s%s %s" % (l.get("kind"), "(%s)" % l["model"] if l.get("model") else "",
                         ("%.4f%%" % l["percent"]) if l.get("percent") is not None else "-")
            for l in r.get("limits", []))
        share = " · ".join("%s %.0f%%" % (s.get("key"), s["percent"])
                           for s in ((r.get("breakdown") or {}).get("rows") or [])
                           if s.get("percent"))
        print("%s  %s%s" % (r.get("fetched_at"), bits, ("  │ " + share) if share else ""))


def main():
    if "--tail" in sys.argv:
        i = sys.argv.index("--tail")
        n = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit() else 20
        tail(n)
        return 0
    data = fetch()
    if "--shape" in sys.argv:
        print("%s%s%s" % (B, t("Response structure (only numbers are shown as values)",
                               "응답 구조 (값은 숫자만 보여준다)"), X))
        shape(data)
        return 0
    if "--log" in sys.argv:
        append_log(data)
        return 0
    show(data)
    print("%s%s%s" % (D, t("--log record · --shape response structure · --tail N recorded samples",
                           "--log 기록 · --shape 응답 구조 · --tail N 쌓인 기록"), X))
    return 0


if __name__ == "__main__":
    sys.exit(main())
