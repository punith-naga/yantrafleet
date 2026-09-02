# Academy content packs

This directory holds the JSON content packs that `academy/index.html` loads.
Each pack is a self-contained course: tracks (role-based lesson sequences),
lessons (reading + quiz + hands-on practical), and one checkride (a scored,
scenario-based certification).

- `pack-physical-ai.json` — the flagship **Physical AI Operations** pack
  (Operator / Fleet Admin / Integrator tracks, 12 lessons, one checkride).
- `validate.py` — stdlib-only schema validator. Run it before committing any
  pack change.

```bash
python academy/content/validate.py                     # validates every pack-*.json here
python academy/content/validate.py my-pack.json        # or specific files
```

It prints `PASS` (exit 0) or a list of errors (exit 1).

## Schema

One top-level object:

```jsonc
{
  "pack": {
    "id": "pack-physical-ai",          // stable slug, matches the filename
    "title": "Physical AI Operations",
    "version": "1.0.0",                // bump on any content change
    "tracks":   [ Track, ... ],
    "lessons":  [ Lesson, ... ],
    "checkride": Checkride
  }
}
```

### Track

```jsonc
{
  "id": "operator",                    // referenced by Lesson.track
  "title": "Operator",
  "role": "One-line description of who this track is for",
  "lesson_ids": ["lesson-01-...", ...] // ordered; must exist in pack.lessons
}
```

Cross-reference rules (enforced by `validate.py`):

- every id in `lesson_ids` must name a lesson in `pack.lessons`;
- every lesson must appear in exactly one track's `lesson_ids`;
- a lesson's `track` field must match the track that lists it.

### Lesson

```jsonc
{
  "id": "lesson-03-alarm-discipline",  // stable slug, unique in the pack
  "title": "Alarm Discipline",
  "minutes": 15,                       // integer estimated reading+practical time
  "track": "operator",                 // a declared track id
  "body_html": "<h3>...</h3><p>...</p><ul><li>...</li></ul>",
  "key_points": ["...", ...],          // 4-6 short takeaway strings
  "quiz": [ Question, ... ],           // 3-4 questions
  "practical": {
    "instructions_html": "<p>...</p><ul><li>...</li></ul>",
    "verify": Verify
  },
  "tutor_context": "..."               // <=1500 chars, dense factual summary
}
```

- `body_html`: 400–700 words. Allowed tags only: `<h3>`, `<p>`, `<ul>`/`<li>`,
  `<code>` (plus inline `<em>`/`<strong>`). No links, images, scripts, styles,
  or attributes.
- `tutor_context` is what the AI tutor sees when a learner asks questions
  inside the lesson. Pack it with concrete platform facts (table/column names,
  thresholds, commands, file paths) — not prose. Hard cap 1500 characters.

### Question

```jsonc
{
  "q": "The question text?",
  "options": ["A", "B", "C", "D"],     // >=2 options; one correct
  "answer_idx": 1,                     // 0-based index into options
  "explain": "Why that answer is right (shown after answering)."
}
```

### Verify (practical + checkride steps)

Two kinds:

```jsonc
// Checked automatically against the live PostgREST backend:
{ "kind": "backend_check",
  "table": "alerts",                   // one of the live tables (below)
  "filter": "ack=eq.true&select=id&limit=1",   // PostgREST query string
  "expect": "rows>0" }                 // "rows>0" | "rows==0" | "field_equals"

// field_equals additionally requires:
{ "kind": "backend_check", "table": "...", "filter": "...",
  "expect": "field_equals", "field": "status", "value": "executed" }

// When the backend cannot observe the outcome:
{ "kind": "self_check",
  "prompt": "Question the learner answers honestly about what they did." }
```

Live tables a `backend_check` may target: `robots`, `alerts`, `incidents`,
`commands`, `missions`, `robot_telemetry`, `maintenance_findings`,
`fleet_meta`. Keep filters to what both real PostgREST and the loopback fake
support: `eq.`, `in.(...)`, `select=`, `limit=`. Always add `&limit=1` when
you only need existence.

### Checkride

```jsonc
{
  "id": "checkride-operator-cert",
  "title": "Operator Certification — Checkride",
  "steps": [
    { "instruction_html": "<p>...</p>",
      "verify": Verify,                // usually backend_check
      "rubric_points": ["...", "..."]  // 2-3 observable criteria per step
    }, ...
  ],
  "pass_threshold": 0.8                // fraction of rubric points required, (0, 1]
}
```

## Authoring a new pack

1. Copy the shape of `pack-physical-ai.json`; name the file `pack-<slug>.json`
   and set `pack.id` to match.
2. **Ground every claim in the real platform.** Read the source before you
   write: `README.md`, `docs/SECURITY.md`, `core/yantracore/status.py`,
   `supabase/*.sql`, and the component READMEs. Do not invent features,
   thresholds, table columns, or CLI flags — learners will check, and so will
   the checkride.
3. Design practicals to be doable on the zero-config loopback platform
   (`python -m yantraops up --loopback`). Prefer `backend_check` verifies —
   they make progress objective. The simulator's scripted, deterministic
   AMR-07 localization fault is your friend for repeatable scenarios.
4. Choose filters whose truth persists: history tables (`robot_telemetry`,
   `incidents`, `commands`) beat live-state snapshots (`robots.status`
   changes seconds later).
5. Keep the writing tight and professional; teach vocabulary exactly as the
   platform enforces it (e.g. the seven canonical statuses).
6. Run `python academy/content/validate.py` until it prints `PASS`.
7. Bump `pack.version` on every content change.
