#!/usr/bin/env python3
"""Validate an Academy content pack against the schema academy/index.html loads.

Stdlib only. Usage:

    python academy/content/validate.py [pack-physical-ai.json ...]

With no arguments, validates every pack-*.json next to this script.
Exits 0 and prints PASS when every pack is valid; exits 1 otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ERRORS: list[str] = []

# Live PostgREST tables a backend_check may target (see repo supabase/*.sql).
KNOWN_TABLES = {
    "robots", "alerts", "incidents", "commands", "missions",
    "robot_telemetry", "maintenance_findings", "fleet_meta",
}
EXPECTS = {"rows>0", "rows==0", "field_equals"}
ALLOWED_TAGS = {"h3", "p", "ul", "li", "code", "em", "strong"}
TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)")


def err(path: str, msg: str) -> None:
    ERRORS.append(f"{path}: {msg}")


def need(obj: dict, key: str, typ, path: str, nonempty: bool = True):
    """Assert obj[key] exists and has type typ; return the value (or None)."""
    if not isinstance(obj, dict) or key not in obj:
        err(path, f"missing required field '{key}'")
        return None
    val = obj[key]
    if not isinstance(val, typ):
        err(path, f"'{key}' must be {getattr(typ, '__name__', typ)}, got {type(val).__name__}")
        return None
    if nonempty and isinstance(val, str) and not val.strip():
        err(path, f"'{key}' must not be empty")
    if nonempty and isinstance(val, list) and not val:
        err(path, f"'{key}' must not be an empty list")
    return val


def check_html(html: str | None, path: str) -> None:
    if not html:
        return
    for m in TAG_RE.finditer(html):
        tag = m.group(1).lower()
        if tag not in ALLOWED_TAGS:
            err(path, f"disallowed HTML tag <{tag}>")


def check_verify(v, path: str) -> None:
    if not isinstance(v, dict):
        err(path, "verify must be an object")
        return
    kind = need(v, "kind", str, path)
    if kind == "backend_check":
        table = need(v, "table", str, path)
        if table is not None and table not in KNOWN_TABLES:
            err(path, f"unknown table '{table}' (known: {sorted(KNOWN_TABLES)})")
        need(v, "filter", str, path)
        expect = need(v, "expect", str, path)
        if expect is not None and expect not in EXPECTS:
            err(path, f"expect must be one of {sorted(EXPECTS)}, got '{expect}'")
        if expect == "field_equals":
            need(v, "field", str, path)
            if "value" not in v:
                err(path, "expect 'field_equals' requires 'value'")
        for k in ("field", "value"):
            if k in v and expect != "field_equals":
                err(path, f"'{k}' is only valid with expect 'field_equals'")
    elif kind == "self_check":
        need(v, "prompt", str, path)
    elif kind is not None:
        err(path, f"verify.kind must be 'backend_check' or 'self_check', got '{kind}'")


def check_quiz(quiz, path: str) -> None:
    if not isinstance(quiz, list) or not quiz:
        err(path, "quiz must be a non-empty list")
        return
    if not 3 <= len(quiz) <= 4:
        err(path, f"quiz should have 3-4 questions, has {len(quiz)}")
    for i, q in enumerate(quiz):
        qp = f"{path}.quiz[{i}]"
        if not isinstance(q, dict):
            err(qp, "quiz entry must be an object")
            continue
        need(q, "q", str, qp)
        opts = need(q, "options", list, qp)
        need(q, "explain", str, qp)
        idx = need(q, "answer_idx", int, qp, nonempty=False)
        if isinstance(opts, list):
            if len(opts) < 2:
                err(qp, "options needs at least 2 entries")
            if not all(isinstance(o, str) and o.strip() for o in opts):
                err(qp, "every option must be a non-empty string")
            if isinstance(idx, int) and not 0 <= idx < len(opts):
                err(qp, f"answer_idx {idx} out of range for {len(opts)} options")


def check_lesson(lesson, track_ids: set[str], path: str) -> str | None:
    if not isinstance(lesson, dict):
        err(path, "lesson must be an object")
        return None
    lid = need(lesson, "id", str, path)
    need(lesson, "title", str, path)
    minutes = need(lesson, "minutes", int, path, nonempty=False)
    if isinstance(minutes, int) and not 1 <= minutes <= 120:
        err(path, f"minutes {minutes} out of sane range 1-120")
    track = need(lesson, "track", str, path)
    if track is not None and track not in track_ids:
        err(path, f"track '{track}' is not a declared track id")
    body = need(lesson, "body_html", str, path)
    check_html(body, f"{path}.body_html")
    if isinstance(body, str):
        words = len(re.sub(r"<[^>]+>", " ", body).split())
        if not 300 <= words <= 800:
            err(path, f"body_html has {words} words (target 400-700, hard bounds 300-800)")
    kps = need(lesson, "key_points", list, path)
    if isinstance(kps, list):
        if not 4 <= len(kps) <= 6:
            err(path, f"key_points should have 4-6 entries, has {len(kps)}")
        if not all(isinstance(k, str) and k.strip() for k in kps):
            err(path, "every key_point must be a non-empty string")
    check_quiz(lesson.get("quiz"), path)
    practical = need(lesson, "practical", dict, path)
    if isinstance(practical, dict):
        instr = need(practical, "instructions_html", str, f"{path}.practical")
        check_html(instr, f"{path}.practical.instructions_html")
        check_verify(practical.get("verify"), f"{path}.practical.verify")
    tc = need(lesson, "tutor_context", str, path)
    if isinstance(tc, str) and len(tc) > 1500:
        err(path, f"tutor_context is {len(tc)} chars (max 1500)")
    return lid


def check_checkride(cr, path: str) -> None:
    if not isinstance(cr, dict):
        err(path, "checkride must be an object")
        return
    need(cr, "id", str, path)
    need(cr, "title", str, path)
    steps = need(cr, "steps", list, path)
    if isinstance(steps, list):
        for i, step in enumerate(steps):
            sp = f"{path}.steps[{i}]"
            if not isinstance(step, dict):
                err(sp, "step must be an object")
                continue
            instr = need(step, "instruction_html", str, sp)
            check_html(instr, f"{sp}.instruction_html")
            check_verify(step.get("verify"), f"{sp}.verify")
            rps = need(step, "rubric_points", list, sp)
            if isinstance(rps, list):
                if not 2 <= len(rps) <= 3:
                    err(sp, f"rubric_points should have 2-3 entries, has {len(rps)}")
                if not all(isinstance(r, str) and r.strip() for r in rps):
                    err(sp, "every rubric_point must be a non-empty string")
    pt = cr.get("pass_threshold")
    if not isinstance(pt, (int, float)) or isinstance(pt, bool) or not 0 < pt <= 1:
        err(path, f"pass_threshold must be a number in (0, 1], got {pt!r}")


def check_pack(data, fname: str) -> None:
    pack = need(data, "pack", dict, fname)
    if pack is None:
        return
    p = f"{fname}:pack"
    need(pack, "id", str, p)
    need(pack, "title", str, p)
    need(pack, "version", str, p)

    tracks = need(pack, "tracks", list, p) or []
    lessons = need(pack, "lessons", list, p) or []

    track_ids: set[str] = set()
    referenced: list[str] = []
    for i, t in enumerate(tracks):
        tp = f"{p}.tracks[{i}]"
        if not isinstance(t, dict):
            err(tp, "track must be an object")
            continue
        tid = need(t, "id", str, tp)
        need(t, "title", str, tp)
        need(t, "role", str, tp)
        if tid:
            if tid in track_ids:
                err(tp, f"duplicate track id '{tid}'")
            track_ids.add(tid)
        lids = need(t, "lesson_ids", list, tp)
        if isinstance(lids, list):
            referenced.extend(x for x in lids if isinstance(x, str))
            if not all(isinstance(x, str) and x.strip() for x in lids):
                err(tp, "every lesson_id must be a non-empty string")

    lesson_ids: dict[str, str] = {}
    for i, lesson in enumerate(lessons):
        lp = f"{p}.lessons[{i}]"
        lid = check_lesson(lesson, track_ids, lp)
        if lid:
            if lid in lesson_ids:
                err(lp, f"duplicate lesson id '{lid}'")
            lesson_ids[lid] = lesson.get("track", "")

    # Cross-checks: every referenced lesson exists; every lesson is referenced
    # by exactly its own track's lesson_ids.
    for i, t in enumerate(tracks):
        if not isinstance(t, dict):
            continue
        for lid in t.get("lesson_ids") or []:
            if lid not in lesson_ids:
                err(f"{p}.tracks[{i}]", f"lesson_ids references unknown lesson '{lid}'")
            elif lesson_ids[lid] != t.get("id"):
                err(f"{p}.tracks[{i}]",
                    f"lesson '{lid}' is listed here but its track field is '{lesson_ids[lid]}'")
    if len(referenced) != len(set(referenced)):
        dupes = sorted({x for x in referenced if referenced.count(x) > 1})
        err(p, f"lesson ids referenced by more than one track: {dupes}")
    orphans = sorted(set(lesson_ids) - set(referenced))
    if orphans:
        err(p, f"lessons not referenced by any track: {orphans}")

    check_checkride(pack.get("checkride"), f"{p}.checkride")


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent
    files = [Path(a) for a in argv] or sorted(here.glob("pack-*.json"))
    if not files:
        print("no pack-*.json files found")
        return 1
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            err(str(f), f"cannot load: {exc}")
            continue
        check_pack(data, f.name)
        if isinstance(data.get("pack"), dict):
            n_lessons = len(data["pack"].get("lessons", []))
            n_tracks = len(data["pack"].get("tracks", []))
            n_steps = len((data["pack"].get("checkride") or {}).get("steps", []))
            print(f"{f.name}: {n_tracks} tracks, {n_lessons} lessons, "
                  f"{n_steps} checkride steps")
    if ERRORS:
        print(f"\nFAIL — {len(ERRORS)} error(s):")
        for e in ERRORS:
            print(f"  - {e}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
