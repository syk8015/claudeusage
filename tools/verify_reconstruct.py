#!/usr/bin/env python3
"""originalFile 이 잘리지 않은 완전한 파일 내용인지 검증한다.

같은 파일을 연속으로 고쳤다면:
    apply(편집N.originalFile, 편집N의 변경) == 편집N+1.originalFile
이 성립해야 한다. 성립하면 로그만으로 파일 최종 상태를 복원할 수 있다.
"""
import json, sys, glob, os
from collections import defaultdict

def load(path):
    """깨진 줄은 건너뛰고 JSONL 을 읽는다."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out

def edits_of(entries):
    """파일 편집 이벤트만 시간순으로 뽑는다."""
    evs = []
    for e in entries:
        r = e.get("toolUseResult")
        if not isinstance(r, dict) or "structuredPatch" not in r:
            continue
        evs.append({
            "ts": e.get("timestamp", ""),
            "path": r.get("filePath"),
            "orig": r.get("originalFile"),
            "old": r.get("oldString"),
            "new": r.get("newString"),
            "all": r.get("replaceAll", False),
            "content": r.get("content"),          # Write 인 경우 최종 내용
            "kind": "write" if "content" in r else "edit",
        })
    evs.sort(key=lambda x: x["ts"])
    return evs

def apply_edit(ev):
    """편집을 적용한 뒤의 파일 내용. 복원 불가면 None."""
    if ev["kind"] == "write":
        return ev["content"]
    if ev["orig"] is None or ev["old"] is None or ev["new"] is None:
        return None
    if ev["all"]:
        return ev["orig"].replace(ev["old"], ev["new"])
    if ev["orig"].count(ev["old"]) != 1:
        return None                                # 유일하지 않으면 판단 보류
    return ev["orig"].replace(ev["old"], ev["new"], 1)

def main(paths):
    ok = mismatch = skipped = 0
    examples = []
    for p in paths:
        by_file = defaultdict(list)
        for ev in edits_of(load(p)):
            if ev["path"]:
                by_file[ev["path"]].append(ev)
        for path, evs in by_file.items():
            for a, b in zip(evs, evs[1:]):
                if b["orig"] is None:              # Write 는 originalFile 이 없음
                    skipped += 1
                    continue
                got = apply_edit(a)
                if got is None:
                    skipped += 1
                elif got == b["orig"]:
                    ok += 1
                else:
                    mismatch += 1
                    if len(examples) < 3:
                        examples.append((os.path.basename(path), len(got), len(b["orig"])))
    total = ok + mismatch
    print(f"연속 편집 쌍 검증:  일치 {ok} / 불일치 {mismatch} / 판단보류 {skipped}")
    if total:
        print(f"일치율: {ok/total*100:.1f}%")
    for name, g, e in examples:
        print(f"  불일치 예: {name}  복원 {g}자 vs 기록 {e}자")

if __name__ == "__main__":
    d = os.path.expanduser("~/.claude/projects/-Users-Vive-Desktop------------")
    main(sorted(glob.glob(os.path.join(d, "*.jsonl"))))
