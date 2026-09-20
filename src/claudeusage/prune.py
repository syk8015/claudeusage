#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prune.py — 오래된 한도 표본을 줄인다. 한도 역산이 읽는 칸만 남기고 나머지를 버린다.

상태바 수집기가 하루 1~5MB 씩 쌓는다. 그런데 줄 하나의 80% 는 한도 역산이 한 번도
안 읽는 칸이다(프롬프트 캐시 상태·컨텍스트 창·작업 경로 …). 그래서 **줄을 지우지
않고 칸만 걷어낸다.** 줄 수·순서·시각이 그대로라 `limit`·`chat`·`check` 가 보는 것이
한 글자도 안 바뀐다. 표본을 솎아내면(예: 10분에 하나) 게이지가 처음 오른 시각이 밀려
귀속 구간이 달라지고 적합이 변한다. 그래서 솎지 않는다(docs/prune-검증.md 6절).

    claudeusage prune                       미리보기. 아무것도 안 바꾼다
    claudeusage prune --older-than 30       30일보다 오래된 줄만 (기본 14)
    claudeusage prune --keep model,prompt_cache   그 칸도 남긴다
    claudeusage prune --apply               백업을 먼저 만들고 실제로 줄인다
    claudeusage prune --log 경로            다른 표본 파일

--apply 가 하는 일, 순서대로:
  1. 원본 앞부분(줄 경계까지)을 그대로 복사해 백업한다. 실패하면 여기서 멈춘다. 원본은 그대로다.
  2. 원본을 읽어 새 파일(임시)을 쓴다. 최근 줄은 바이트 그대로 옮긴다.
  3. 새 파일과 백업을 **진짜 읽는 함수(load_samples)** 로 읽어 결과가 같은지 본다. 다르면 멈춘다.
  4. 그 사이 수집기가 덧붙인 줄을 새 파일 끝에 붙이고, 이름을 바꿔 자리를 바꾼다.
  5. 바꾼 직후 옛 파일로 늦게 들어온 줄이 있으면 마저 옮긴다.

4·5 가 있는 이유: 수집기는 이 파일을 열어 한 줄 덧붙이고 닫기를 세션마다 계속한다.
그냥 읽어서 다시 쓰면 그 사이 붙은 줄이 사라진다. 표본은 한 번 잃으면 되살릴 수 없다.
"""

import argparse, hashlib, json, os, shutil, sys, time
from collections import namedtuple

from .i18n import t
from . import paths
from . import cc_limit as L

B, D, R, Y, X = (("\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[0m")
                 if sys.stdout.isatty() else ("",) * 5)

DEFAULT_DAYS = 14

# 남길 칸. cc_limit.load_samples 가 실제로 읽는 칸과 정확히 같다(짐작이 아니라 그 함수를
# 읽고 적었다). check_limit·cc_chat 도 표본은 전부 load_samples 로 읽는다.
#
#   session_id                                   세션 구분. 없거나 test-preview 면 버려진다
#   agent_type · agent.name                      서브에이전트 렌더를 거른다
#   rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}   게이지
#   logged_at                                    시각
#   cost.total_cost_usd                          세션 누계 비용 (cost_by_window)
#
# load_samples 가 읽는 칸을 바꾸면 여기도 고쳐야 한다. 잊어도 --apply 직전의 동일성
# 검사(same_view)가 잡아서 멈춘다.
KEEP_TOP = ("logged_at", "session_id", "agent_type")
KEEP_AGENT = ("name",)
KEEP_WINDOWS = ("five_hour", "seven_day")
KEEP_WINDOW = ("used_percentage", "resets_at")
KEEP_COST = ("total_cost_usd",)

Row = namedtuple("Row", "new e small old")


def dumps(o):
    # ensure_ascii 기본값(True)이 맞다. 깨진 서로게이트가 섞여 있어도 안전하게 나가고
    # 읽으면 똑같은 문자열로 돌아온다.
    return json.dumps(o, separators=(",", ":"))


def _pick(v, keys):
    """dict 면 keys 만 남긴다. 다른 모양은 읽는 쪽이 어떻게 볼지 모르니 손대지 않는다."""
    if not isinstance(v, dict):
        return v
    return {k: v[k] for k in keys if k in v}


def slim(e, extra=()):
    """줄 하나에서 읽히는 칸만 남긴다. 순서는 원래대로."""
    out = {}
    for k, v in e.items():
        if k in KEEP_TOP or k in extra:
            out[k] = v
        elif k == "agent":
            out[k] = _pick(v, KEEP_AGENT)
        elif k == "cost":
            out[k] = _pick(v, KEEP_COST)
        elif k == "rate_limits":
            out[k] = ({w: _pick(x, KEEP_WINDOW) for w, x in v.items() if w in KEEP_WINDOWS}
                      if isinstance(v, dict) else v)
    return out


def _parse(raw):
    try:
        e = json.loads(raw)
    except ValueError:                       # 깨진 줄·깨진 UTF-8. load() 도 건너뛰는 줄이다
        return None
    return e if isinstance(e, dict) else None


def rewrite(raw, cutoff, extra=()):
    """줄 하나를 어떻게 할지 정한다.

    손대지 않는 줄은 new 가 raw 그대로다(바이트 그대로 옮긴다). 손대는 줄은 줄인 쪽이
    더 짧을 때만 바꾼다. 이미 줄인 줄을 다시 쓰면 같은 크기로 돌아오므로 두 번째 실행은
    아무것도 안 바꾼다.
    """
    e = _parse(raw)
    if e is None:
        return Row(raw, None, None, False)
    ts = L.to_epoch(e.get("logged_at"))
    if ts is None or ts >= cutoff:           # 최근 줄이거나 시각을 못 읽는 줄
        return Row(raw, e, None, False)
    small = slim(e, extra)
    new = (dumps(small) + "\n").encode("ascii")
    if len(new) >= len(raw):
        return Row(raw, e, None, True)
    return Row(new, e, small, True)


def _account(fields, e, small):
    """어느 칸에서 몇 바이트가 빠졌나. 다시 직렬화해 재므로 근사값이다."""
    for k, v in e.items():
        before = len(dumps(v)) + len(k) + 4
        after = len(dumps(small[k])) + len(k) + 4 if k in small else 0
        if before > after:
            fields[k] = fields.get(k, 0) + before - after


def scan(f, end, cutoff, extra=(), out=None):
    """f 의 앞 end 바이트(줄 경계)를 훑는다. out 이 있으면 결과 줄을 거기 쓴다."""
    s = {"rows": 0, "old": 0, "changed": 0, "bad": 0, "bytes_in": 0, "bytes_out": 0,
         "old_in": 0, "old_out": 0, "fields": {}, "t_min": None, "t_max": None}
    f.seek(0)
    pos = 0
    while pos < end:
        raw = f.readline()
        if not raw:
            break
        pos += len(raw)
        r = rewrite(raw, cutoff, extra)
        s["rows"] += 1
        s["bytes_in"] += len(raw)
        s["bytes_out"] += len(r.new)
        if r.e is None:
            s["bad"] += 1
        else:
            ts = L.to_epoch(r.e.get("logged_at"))
            if ts is not None:
                s["t_min"] = ts if s["t_min"] is None else min(s["t_min"], ts)
                s["t_max"] = ts if s["t_max"] is None else max(s["t_max"], ts)
        if r.old:
            s["old"] += 1
            s["old_in"] += len(raw)
            s["old_out"] += len(r.new)
        if r.small is not None:
            s["changed"] += 1
            _account(s["fields"], r.e, r.small)
        if out is not None:
            out.write(r.new)
    return s


def would_change(f, end, cutoff, extra=()):
    """줄일 줄이 하나라도 있나. 처음 하나에서 멈춘다."""
    f.seek(0)
    pos = 0
    while pos < end:
        raw = f.readline()
        if not raw:
            break
        pos += len(raw)
        if rewrite(raw, cutoff, extra).small is not None:
            return True
    return False


def line_end(f, size):
    """size 이하에서 마지막 개행 바로 뒤. 수집기가 쓰는 도중의 덜 끝난 줄은 뺀다."""
    back = min(size, 1 << 20)
    f.seek(size - back)
    i = f.read(back).rfind(b"\n")
    return size - back + i + 1 if i >= 0 else 0


def make_backup(src, end, log):
    """원본 앞 end 바이트를 그대로 복사하고 다시 읽어 같은지 확인한다. 실패하면 OSError."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # 이름이 .jsonl 로 끝나야 .gitignore 의 data/*.jsonl 에 걸린다(사용량 이력이라 올리면 안 된다)
    path = os.path.join(os.path.dirname(log), "ratelimit-log.bak-%s.jsonl" % stamp)
    # 이미 있는 이름이면 여기서 FileExistsError. 남의 백업이므로 덮어쓰지도, 지우지도 않는다.
    # 아래 except 의 삭제는 우리가 방금 만든 파일에만 닿아야 해서 이 줄을 try 밖에 뒀다.
    out = open(path, "xb")
    h = hashlib.sha256()
    try:
        src.seek(0)
        left = end
        with out:
            while left:
                chunk = src.read(min(1 << 20, left))
                if not chunk:
                    raise OSError("original is shorter than expected")
                out.write(chunk)
                h.update(chunk)
                left -= len(chunk)
            out.flush()
            os.fsync(out.fileno())
        h2, n = hashlib.sha256(), 0
        with open(path, "rb") as chk:
            for chunk in iter(lambda: chk.read(1 << 20), b""):
                h2.update(chunk)
                n += len(chunk)
        if n != end or h2.digest() != h.digest():
            raise OSError("backup does not match the original")
    except BaseException:
        try:
            os.remove(path)                  # 반쪽짜리 백업을 남기지 않는다
        except OSError:
            pass
        raise
    return path, h.hexdigest()


def same_view(a, b):
    """두 표본 파일을 읽는 쪽(load_samples)이 똑같이 보는지. 다른 창 이름을 돌려준다."""
    for field in KEEP_WINDOWS:
        if L.load_samples(a, field) != L.load_samples(b, field):
            return field
    return None


def read_tail(src, end):
    """end 이후를 전부 읽는다. 마지막 줄이 덜 써졌으면(개행이 없으면) 잠깐 기다린다."""
    tail = b""
    for _ in range(20):
        src.seek(end)
        tail = src.read()
        if not tail or tail.endswith(b"\n"):
            break
        time.sleep(0.1)
    return tail


def _mib(n):
    return "%.1f MiB" % (n / 2 ** 20)


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "-"


def _row(label, value):
    print("  %s%-10s%s %s" % (D, label, X, value))


def preview(log, cutoff, days, extra):
    with open(log, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        end = line_end(f, size)
        s = scan(f, end, cutoff, extra)
    print("\n%s%s%s\n" % (B, t("prune — preview only. Nothing was changed.",
                               "prune — 미리보기. 아무것도 바꾸지 않았다."), X))
    _row(t("file", "파일"), log)
    _row(t("now", "지금"), t("%s · %s rows · %s → %s", "%s · %s줄 · %s → %s")
         % (_mib(end), format(s["rows"], ","), _day(s["t_min"]), _day(s["t_max"])))
    if s["bad"]:
        _row("", t("%d unreadable lines are left exactly as they are",
                   "읽을 수 없는 줄 %d개는 그대로 둔다") % s["bad"])
    _row(t("cutoff", "기준"), t("rows logged before %s (older than %g days)",
                              "%s 이전에 찍힌 줄 (%g일보다 오래됨)")
         % (time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(cutoff)), days))
    if not s["changed"]:
        print("\n  " + t("Nothing to slim: every older row is already as small as it gets.",
                         "줄일 게 없다. 오래된 줄이 이미 다 줄어 있다."))
        return 0
    saved = s["bytes_in"] - s["bytes_out"]
    _row(t("slim", "줄임"), t("%s rows: %s → %s", "%s줄: %s → %s")
         % (format(s["changed"], ","), _mib(s["old_in"]), _mib(s["old_out"])))
    _row(t("keep", "그대로"), t("%s newer rows, byte for byte", "%s줄은 바이트 그대로")
         % format(s["rows"] - s["old"], ","))
    _row(t("after", "결과"), "%s → %s%s  (−%s, −%.0f%%)%s"
         % (_mib(s["bytes_in"]), B, _mib(s["bytes_out"]), _mib(saved),
            100.0 * saved / s["bytes_in"], X))
    top = sorted(s["fields"].items(), key=lambda kv: -kv[1])[:8]
    print("\n  %s%s%s" % (D, t("dropped from the slimmed rows (≈):", "걷어내는 칸 (≈):"), X))
    for k, v in top:
        print("    %-22s %s" % (k, _mib(v)))
    if len(s["fields"]) > len(top):
        print("    %s" % t("… and %d more", "… 외 %d개") % (len(s["fields"]) - len(top)))
    kept = ", ".join(KEEP_TOP + ("agent.name", "rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}",
                                 "cost.total_cost_usd"))
    print("\n  %s%s %s%s" % (D, t("kept (exactly what limit --fit reads):",
                                  "남기는 칸 (limit --fit 이 읽는 것 전부):"), kept, X))
    if extra:
        print("  %s%s %s%s" % (D, t("also kept (--keep):", "더 남기는 칸 (--keep):"), ", ".join(extra), X))
    print("\n  %s" % t("To do it:  claudeusage prune --apply   (makes a backup first; stops if it cannot)",
                       "실행:  claudeusage prune --apply   (백업을 먼저 만든다. 못 만들면 멈춘다)"))
    print("  %s%s%s" % (D, t("More:      --older-than 0 slims every row · --keep model,prompt_cache keeps extra fields",
                            "다른 값:  --older-than 0 은 전부 줄임 · --keep model,prompt_cache 는 그 칸도 남김"), X))
    return 0


def apply(log, cutoff, days, extra):
    log = os.path.realpath(log)
    lock, tmp = log + ".prune-lock", log + ".prune-tmp"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        print("%s%s%s" % (R, t("Another prune is running, or one died and left %s. Delete it if none is running.",
                               "다른 prune 이 돌고 있거나 죽으면서 %s 를 남겼다. 돌고 있지 않다면 지운다.") % lock, X))
        return 1
    except OSError as ex:
        print("%s%s%s" % (R, t("Cannot write next to the log (%s). Nothing was changed.",
                               "로그 옆에 쓸 수 없다 (%s). 아무것도 바꾸지 않았다.") % ex, X))
        return 1
    try:
        return _apply(log, tmp, cutoff, days, extra)
    finally:
        for p in (lock, tmp):
            try:
                os.remove(p)
            except OSError:
                pass


def _apply(log, tmp, cutoff, days, extra):
    def fail(msg):
        print("%s%s%s" % (R, msg, X))
        return 1

    with open(log, "rb") as src:
        end = line_end(src, os.fstat(src.fileno()).st_size)
        if not end or not would_change(src, end, cutoff, extra):
            print(t("Nothing to slim. No backup made, nothing changed.",
                    "줄일 게 없다. 백업도 안 만들었고 아무것도 안 바꿨다."))
            return 0
        free = shutil.disk_usage(os.path.dirname(log)).free
        if free < 2 * end + (16 << 20):
            return fail(t("Not enough disk space for a backup plus the new file (%s free). Nothing was changed.",
                          "백업과 새 파일을 둘 디스크 여유가 없다 (%s 남음). 아무것도 바꾸지 않았다.")
                        % _mib(free))

        # 1. 백업. 못 만들면 원본은 그대로 두고 멈춘다.
        try:
            backup, digest = make_backup(src, end, log)
        except OSError as ex:
            return fail(t("Backup failed (%s). Nothing was changed.",
                          "백업을 못 만들었다 (%s). 아무것도 바꾸지 않았다.") % ex)
        print("%s %s%s%s" % (t("backup:", "백업:"), B, backup, X))
        print("  %ssha256 %s%s" % (D, digest[:16] + "…", X))

        # 2. 새 파일. 아직 원본 자리에 안 놓는다.
        with open(tmp, "wb") as out:
            s = scan(src, end, cutoff, extra, out)

            # 3. 읽는 쪽이 백업과 똑같이 보는지. 안 그러면 바꾸지 않는다.
            out.flush()
            bad = same_view(backup, tmp)
            if bad:
                return fail(t("The slimmed file reads differently from the backup (%s window). "
                              "Nothing was replaced. The backup is at the path above.",
                              "줄인 파일이 백업과 다르게 읽힌다 (%s 창). 바꾸지 않았다. "
                              "백업은 위 경로에 있다.") % bad)

            # 4. 검사하는 동안 수집기가 덧붙인 줄
            tail = read_tail(src, end)
            out.write(tail)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(log, tmp)
        os.replace(tmp, log)

        # 5. 바꾼 직후 옛 파일(src 가 아직 붙잡고 있다)로 들어온 줄. 수집기가 바꾸기 전에
        #    파일을 열어 둔 채 쓰면 여기로 온다.
        carried, late = end + len(tail), 0
        for _ in range(2):
            time.sleep(0.3)
            src.seek(carried)
            more = src.read()
            if more:
                with open(log, "ab") as g:
                    g.write(more)
                carried += len(more)
                late += len(more)

    saved = s["bytes_in"] - s["bytes_out"]
    print("\n%s%s%s" % (B, t("Done.", "끝났다."), X))
    _row(t("size", "크기"), "%s → %s  (−%s)" % (_mib(s["bytes_in"]), _mib(s["bytes_out"]), _mib(saved)))
    _row(t("rows", "줄 수"), t("%s slimmed · %s kept byte for byte", "%s줄 줄임 · %s줄 그대로")
         % (format(s["changed"], ","), format(s["rows"] - s["changed"], ",")))
    _row(t("checked", "확인"), t("limit's view of the file (5h and 7d) is identical to the backup",
                               "한도 표본을 읽는 결과(5시간·7일)가 백업과 똑같다"))
    if tail or late:
        _row(t("carried", "옮김"), t("%d bytes collected while pruning were carried over",
                                   "정리하는 동안 쌓인 %d바이트를 옮겼다") % (len(tail) + late))
    _row(t("undo", "되돌리기"), 'cp "%s" "%s"' % (backup, log))
    print("  %s%s%s" % (D, t("(rows collected after the backup exist only in the current file — copy those first "
                            "if you need them. prune never deletes a backup.)",
                            "(백업 뒤에 쌓인 줄은 지금 파일에만 있다. 필요하면 먼저 따로 떼어 둘 것. "
                            "prune 은 백업을 지우지 않는다.)"), X))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="claudeusage prune", allow_abbrev=False,   # --apply 를 줄임말로 실수로 부르지 못하게
        description=t("Shrink old rate-limit samples, keeping only what `limit --fit` reads. "
                      "Without --apply it only shows what would happen.",
                      "오래된 한도 표본을 줄인다. `limit --fit` 이 읽는 칸만 남긴다. "
                      "--apply 가 없으면 미리보기만 한다."))
    ap.add_argument("--apply", action="store_true",
                    help=t("really do it (backup first)", "실제로 한다 (백업 먼저)"))
    ap.add_argument("--older-than", type=float, default=DEFAULT_DAYS, metavar="DAYS",
                    help=t("only rows older than this (default %d; 0 = all rows)",
                           "이보다 오래된 줄만 (기본 %d, 0 이면 전부)") % DEFAULT_DAYS)
    ap.add_argument("--keep", default="", metavar="FIELDS",
                    help=t("extra top-level fields to keep, comma separated (e.g. model,prompt_cache)",
                           "더 남길 칸, 쉼표로 (예: model,prompt_cache)"))
    ap.add_argument("--log", default=None, metavar="PATH",
                    help=t("sample file (default: the collector's log)", "표본 파일 (기본: 수집기가 쓰는 것)"))
    args = ap.parse_args(argv)
    if args.older_than < 0:
        ap.error(t("--older-than must be 0 or more", "--older-than 은 0 이상이어야 한다"))

    log = args.log or paths.ratelimit_log()
    if not os.path.exists(log):
        print("%s%s%s" % (R, t("No samples to prune: %s", "줄일 표본이 없다: %s") % log, X))
        return 1
    extra = tuple(k.strip() for k in args.keep.split(",") if k.strip())
    cutoff = time.time() - args.older_than * 86400
    if args.apply:
        return apply(log, cutoff, args.older_than, extra)
    return preview(log, cutoff, args.older_than, extra)


if __name__ == "__main__":
    sys.exit(main())
