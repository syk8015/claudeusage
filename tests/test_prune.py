# -*- coding: utf-8 -*-
"""
test_prune.py — `claudeusage prune` 이 하는 계산 검사. 이 저장소의 첫 테스트다.

지키는 것은 하나다. **prune 이 한도 역산이 보는 값을 바꾸지 않는다.** 표본을 읽는
함수(cc_limit.load_samples)와 그 뒤의 창 묶기·비용 합산 결과가 정리 전/후에 똑같아야
한다. 그 위에 "파일을 잃지 않는다"(백업·수집기와 겹침·되돌릴 수 없는 실수)를 건다.

여기 표본은 손으로 만든 것이다. 실데이터로 잰 검증은 tools/verify_prune.py 와
docs/prune-검증.md 에 있다.

    python -m pytest
"""

import json, os, time

import pytest

from claudeusage import cc_limit as L
from claudeusage import prune

RESET5, RESET7 = 1788254400, 1788274800


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """--apply 가 수집기를 기다리며 자는 시간을 뺀다. 논리는 시간이 흘러야 하지 않는다."""
    monkeypatch.setattr(prune.time, "sleep", lambda s: None)


# ── 표본 만들기 ─────────────────────────────────────────────────────

def stamp(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def row(sid, days_ago, pct5, pct7, cost, **more):
    """수집기가 남기는 줄과 같은 모양. 한도 역산이 안 읽는 칸이 잔뜩 붙어 있다."""
    e = {"logged_at": stamp(days_ago), "session_id": sid,
         "transcript_path": "/p/a.jsonl", "cwd": "/p", "workspace": {"current_dir": "/p"},
         "model": {"id": "m", "display_name": "M"},
         "cost": {"total_cost_usd": cost, "total_duration_ms": 9, "total_lines_added": 1},
         "context_window": {"used_percentage": 12, "current_usage": {"input_tokens": 3}},
         "prompt_cache": {"warm": True, "hit_ratio": 0.9},
         "rate_limits": {"five_hour": {"used_percentage": pct5, "resets_at": RESET5},
                         "seven_day": {"used_percentage": pct7, "resets_at": RESET7}}}
    e.update(more)
    return e


def sample_rows():
    """읽는 쪽이 다르게 다루는 줄을 골고루 넣었다."""
    return [
        row("A", 30, 1, 1, 0.10), row("A", 29.9, 2, 1, 0.20), row("B", 29.8, 3, 2, 0.50),
        row("A", 29.7, 3, 2, 0.30, model={"id": "n"}),
        row("C", 25, 4, 3, 0.90, agent_type="Explore"),                   # 서브에이전트: 읽는 쪽이 버린다
        row("D", 25, 4, 3, 0.90, agent={"name": "helper", "x": 1}),       # 이것도
        row("test-preview", 25, 1, 1, 0.0),                               # 합성 줄: 버린다
        {"logged_at": stamp(24), "session_id": "E", "cost": {"total_cost_usd": 2.0}},   # 게이지가 없다
        {"logged_at": stamp(24), "session_id": "F",                                      # 5시간 값이 없다
         "rate_limits": {"seven_day": {"used_percentage": 7, "resets_at": RESET7}}},
        row("A", 3, 5, 3, 1.00), row("B", 2, 6, 3, 1.50), row("A", 0.5, 7, 4, 2.00),    # 최근
    ]


def write(path, rows, raw_lines=()):
    with open(path, "wb") as f:
        for e in rows:
            f.write((json.dumps(e, separators=(",", ":")) + "\n").encode())
        for r in raw_lines:
            f.write(r)
    return str(path)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def view(path):
    """읽는 쪽이 두 창에서 보는 것 전부. 표본 → 창 묶기 → 변화점 → 비용까지 이어 본다."""
    out = {}
    for field, wl in (("five_hour", 5 * 3600), ("seven_day", 7 * 86400)):
        rows, skip, n_raw = L.load_samples(path, field)
        wins, steps = L.windows_and_steps(rows, wl) if rows else ([], [])
        out[field] = (rows, dict(skip), n_raw, wins, steps, dict(L.cost_by_window(rows, wl)))
    return out


def run(args, capsys):
    rc = prune.main(args)
    return rc, capsys.readouterr().out


def backups(folder):
    return sorted(n for n in os.listdir(folder) if ".bak-" in n)


def leftovers(folder):
    return [n for n in os.listdir(folder) if n.endswith((".prune-lock", ".prune-tmp"))]


# ── 칸을 걷어내는 계산 ────────────────────────────────────────────────

def test_slim_keeps_exactly_what_the_loader_reads(tmp_path):
    rows = sample_rows()
    raw = write(tmp_path / "raw.jsonl", rows)
    slim = write(tmp_path / "slim.jsonl", [prune.slim(e) for e in rows])
    assert view(raw) == view(slim)
    # "같다"만 보면 아무것도 안 줄여도 통과한다. 실제로 줄었는지도 건다.
    assert os.path.getsize(slim) < os.path.getsize(raw) * 0.5


def test_slim_output_has_only_the_read_fields():
    s = prune.slim(row("A", 30, 1, 1, 0.1))
    assert set(s) == {"logged_at", "session_id", "cost", "rate_limits"}
    assert s["cost"] == {"total_cost_usd": 0.1}
    assert s["rate_limits"] == {"five_hour": {"used_percentage": 1, "resets_at": RESET5},
                                "seven_day": {"used_percentage": 1, "resets_at": RESET7}}


def test_slim_keeps_agent_type_and_only_the_agent_name():
    s = prune.slim(row("D", 1, 1, 1, 0, agent={"name": "h", "id": 9}, agent_type="Explore"))
    assert s["agent"] == {"name": "h"} and s["agent_type"] == "Explore"


def test_keep_lists_extra_top_level_fields():
    s = prune.slim(row("A", 30, 1, 1, 0.1), extra=("model", "prompt_cache"))
    assert "model" in s and "prompt_cache" in s and "workspace" not in s


def test_shapes_the_reader_might_treat_differently_are_left_alone():
    e = {"logged_at": stamp(30), "session_id": "A", "cost": 5, "rate_limits": None, "agent": "x"}
    assert prune.slim(e) == e


# ── 줄마다 무엇을 할지 ────────────────────────────────────────────────

def test_only_rows_older_than_the_cutoff_are_slimmed(tmp_path):
    p = write(tmp_path / "l.jsonl", [row("A", 3, 1, 1, 0.1), row("A", 1, 2, 1, 0.2)])
    with open(p, "rb") as f:
        s = prune.scan(f, os.path.getsize(p), time.time() - 2 * 86400)
    assert (s["rows"], s["old"], s["changed"]) == (2, 1, 1)


def test_recent_lines_are_copied_byte_for_byte(tmp_path):
    odd = b'{ "logged_at" : "%s", "session_id":"Z",   "junk": [1, 2 ,3] }\n' % stamp(0.1).encode()
    p = write(tmp_path / "l.jsonl", [row("A", 30, 1, 1, 0.1)], raw_lines=[odd])   # 공백 제멋대로
    out = tmp_path / "out.jsonl"
    with open(p, "rb") as f, open(out, "wb") as o:
        prune.scan(f, os.path.getsize(p), time.time() - 86400, out=o)
    assert read(out).endswith(odd)                       # 최근 줄: 한 바이트도 안 바뀐다
    assert os.path.getsize(out) < os.path.getsize(p)     # 오래된 줄은 줄었다


def test_lines_that_cannot_be_read_or_dated_are_left_alone(tmp_path):
    junk = [b"not json at all\n", b"\n", b"[1,2,3]\n",
            b'{"session_id":"A","logged_at":"garbage","x":"' + b"y" * 50 + b'"}\n']
    p = write(tmp_path / "l.jsonl", [], raw_lines=junk)
    out = tmp_path / "o.jsonl"
    with open(p, "rb") as f, open(out, "wb") as o:
        s = prune.scan(f, os.path.getsize(p), time.time() + 1, out=o)   # 날짜만 읽히면 전부 오래된 줄
    assert read(out) == read(p) and s["changed"] == 0


def test_scan_reports_the_bytes_it_writes(tmp_path):
    p = write(tmp_path / "l.jsonl", sample_rows())
    out = tmp_path / "o.jsonl"
    with open(p, "rb") as f, open(out, "wb") as o:
        s = prune.scan(f, os.path.getsize(p), time.time(), out=o)
    assert s["bytes_in"] == os.path.getsize(p) and s["bytes_out"] == os.path.getsize(out)


def test_line_end_stops_after_the_last_newline(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"aa\nbbb\ncc")
    with open(p, "rb") as f:
        assert prune.line_end(f, 9) == 7                 # 덜 써진 "cc" 는 뺀다
    p.write_bytes(b"no newline yet")
    with open(p, "rb") as f:
        assert prune.line_end(f, 14) == 0


# ── 명령: 미리보기와 --apply ──────────────────────────────────────────

def test_preview_changes_nothing(tmp_path, capsys):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    before, mtime = read(p), os.stat(p).st_mtime_ns
    rc, _ = run(["--older-than", "0", "--log", p], capsys)
    assert rc == 0 and read(p) == before and os.stat(p).st_mtime_ns == mtime
    assert os.listdir(tmp_path) == ["ratelimit-log.jsonl"]           # 백업도 임시 파일도 없다


def test_apply_makes_a_backup_first_and_prints_its_path(tmp_path, capsys):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    original = read(p)
    rc, out = run(["--apply", "--older-than", "0", "--log", p], capsys)
    (name,) = backups(tmp_path)
    b = os.path.join(os.path.realpath(tmp_path), name)
    assert rc == 0
    assert read(b) == original                           # 백업은 원본 그대로다
    assert b in out                                      # 경로를 화면에 찍는다
    assert os.path.getsize(p) < len(original)            # 실제로 줄었다
    assert view(p) == view(b)                            # 읽는 쪽이 보는 것은 같다
    assert not leftovers(tmp_path)


def test_apply_keeps_every_row_and_leaves_recent_ones_untouched(tmp_path, capsys):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    before = read(p).splitlines()
    run(["--apply", "--older-than", "1", "--log", p], capsys)     # 마지막 줄(0.5일 전)만 최근이다
    after = read(p).splitlines()
    assert len(after) == len(before)                     # 줄을 지우지 않는다
    assert after[-1] == before[-1]                       # 최근 줄은 한 바이트도 안 바뀐다
    assert len(after[0]) < len(before[0])                # 오래된 줄은 줄었다


def test_a_second_apply_does_nothing(tmp_path, capsys):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    run(["--apply", "--older-than", "0", "--log", p], capsys)
    after_first, n_backups = read(p), len(backups(tmp_path))
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    assert rc == 0 and read(p) == after_first and len(backups(tmp_path)) == n_backups


def test_keep_flag_keeps_the_extra_fields_on_disk(tmp_path, capsys):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    run(["--apply", "--older-than", "0", "--keep", "model", "--log", p], capsys)
    e = json.loads(read(p).splitlines()[0])
    assert "model" in e and "prompt_cache" not in e


def test_abbreviated_and_negative_options_are_rejected(tmp_path):
    p = write(tmp_path / "l.jsonl", sample_rows())
    original = read(p)
    for bad in (["--ap", "--log", p], ["--older-than", "-1", "--log", p]):
        with pytest.raises(SystemExit) as e:
            prune.main(bad)
        assert e.value.code == 2
    assert read(p) == original and os.listdir(tmp_path) == ["l.jsonl"]


# ── 실패해도 표본을 잃지 않는다 ────────────────────────────────────────

def test_apply_stops_when_the_backup_cannot_be_made(tmp_path, capsys, monkeypatch):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    original = read(p)

    def boom(*a, **k):
        raise OSError("disk full (simulated)")
    monkeypatch.setattr(prune, "make_backup", boom)
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    assert rc == 1 and read(p) == original
    assert os.listdir(tmp_path) == ["ratelimit-log.jsonl"]           # 임시·잠금 파일도 안 남는다


def test_a_backup_that_already_exists_is_never_overwritten_or_deleted(tmp_path, capsys, monkeypatch):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    original = read(p)
    monkeypatch.setattr(prune.time, "strftime", lambda fmt, *a: "20260101-000000")   # 같은 이름이 나온다
    other = tmp_path / "ratelimit-log.bak-20260101-000000.jsonl"
    other.write_bytes(b"someone else's backup")
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    assert rc == 1 and read(p) == original
    assert other.read_bytes() == b"someone else's backup"
    assert not leftovers(tmp_path)


def test_apply_stops_when_the_slimmed_file_reads_differently(tmp_path, capsys, monkeypatch):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    original = read(p)
    real = prune.slim                                    # 읽는 칸 하나를 빠뜨린 남길 칸 목록을 흉내 낸다
    monkeypatch.setattr(prune, "slim",
                        lambda e, extra=(): {k: v for k, v in real(e, extra).items() if k != "cost"})
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    assert rc == 1 and read(p) == original               # 자리를 바꾸지 않았다
    assert len(backups(tmp_path)) == 1 and not leftovers(tmp_path)   # 백업은 남는다


def test_a_lock_file_stops_a_second_prune(tmp_path, capsys):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    original, lock = read(p), p + ".prune-lock"
    open(lock, "w").close()
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    assert rc == 1 and read(p) == original
    assert os.path.exists(lock)                          # 남의 잠금을 지우지 않는다
    assert not backups(tmp_path)


# ── 수집기가 도는 중에도 줄이 안 사라진다 ───────────────────────────────

def test_rows_appended_while_pruning_survive(tmp_path, capsys, monkeypatch):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    marker = json.dumps(row("APPENDED", 0, 9, 9, 9.9), separators=(",", ":")).encode() + b"\n"
    real = prune.same_view

    def collector_writes_meanwhile(a, b):                # 새 파일을 쓰고 검사하는 사이 수집기가 한 줄 붙인다
        with open(p, "ab") as f:
            f.write(marker)
        return real(a, b)
    monkeypatch.setattr(prune, "same_view", collector_writes_meanwhile)
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    data = read(p)
    assert rc == 0 and data.count(marker) == 1 and data.endswith(marker)


def test_a_row_written_to_the_old_file_after_the_swap_is_carried_over(tmp_path, capsys, monkeypatch):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    late = json.dumps(row("LATE", 0, 9, 9, 9.9), separators=(",", ":")).encode() + b"\n"
    real_replace = os.replace

    def swap_with_a_writer_in_flight(src, dst):
        h = open(dst, "ab")                              # 수집기가 바꾸기 전에 열어 둔 파일
        real_replace(src, dst)
        h.write(late)                                    # 바꾼 뒤에야 도착한 줄은 옛 파일로 간다
        h.close()
    monkeypatch.setattr(prune.os, "replace", swap_with_a_writer_in_flight)
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    assert rc == 0 and read(p).count(late) == 1


def test_a_half_written_last_line_is_carried_not_lost_or_cut(tmp_path, capsys):
    p = write(tmp_path / "ratelimit-log.jsonl", sample_rows())
    half = b'{"logged_at":"2026'                          # 수집기가 쓰는 도중
    with open(p, "ab") as f:
        f.write(half)
    rc, _ = run(["--apply", "--older-than", "0", "--log", p], capsys)
    assert rc == 0 and read(p).endswith(half)
