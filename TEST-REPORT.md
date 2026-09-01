# YantraFleet — Release Verification Report (v0.5)

Date: 2026-09-01 · Environment: Linux, Python 3.11.15, pytest 9.1.1,
httpx 0.28.1, mcp 1.x, Node v22.22.2
Verifier: release-verification pass over core/, sim/, connector/, copilot/,
detector/, notifier/, console/, e2e/

## 1. Test runs

Installed: `pip install -e core -e sim -e connector -e detector -e notifier`,
`pip install -r copilot/requirements.txt -r e2e/requirements.txt`,
`pip install mcp` (`--break-system-packages` in the root container; use a
venv normally).

| Component | Suite                                        | Result         |
|-----------|----------------------------------------------|----------------|
| core      | `core/tests`                                 | **4 passed**   |
| sim       | `sim/tests`                                  | **59 passed**  |
| connector | `connector/tests`                            | **32 passed**  |
| copilot   | `copilot/tests` (incl. `test_mcp_server.py`) | **33 passed**  |
| detector  | `detector/tests` (incl. maintenance)         | **60 passed**  |
| notifier  | `notifier/tests`                             | **25 passed**  |
| console   | `console/tests` (structure + `node --check`) | **9 passed**   |
| e2e       | `e2e/test_e2e.py` (fake PostgREST loopback)  | **13 passed**  |

**Total: 235 passed, 0 failed.** All suites run fully offline; no failures
needed fixing this pass — every suite was green on the first run. The e2e
suite uses real httpx over 127.0.0.1 sockets against `e2e/fakerest.py`
(in-process fake PostgREST); everything else uses mock/static transports
or stdlib.

Note: suites must be run from each component's own directory — test module
basenames collide across components (`test_cli.py`, `test_sink.py`, …), so
a single repo-root `pytest core sim ...` invocation fails at collection.
This matches CI, which runs per-directory.

`node scripts/check_console.mjs`: **OK** — 1 inline script block parses clean.

## 2. E2E extensions (this pass)

`e2e/fakerest.py` gained the `missions` and `maintenance_findings` tables
(the generic filter/upsert/patch engine needed no changes). Three new tests
(10 → 13):

- `test_sim_publishes_missions_rows` — the shared 8-tick story world now
  proves the sim transport POSTs `missions` with `on_conflict=id`
  (idempotent upserts) and that every row matches the `0001_init.sql`
  shape: `M-###` id, non-empty name, 2–4 crew ids that all exist in
  `robots`, `state ∈ {Queued, Running, Done}`, integer `prog` 0–100,
  string `eta`, stamped `created_at`.
- `test_mission_progress_advances_and_upserts` — 12 more published ticks:
  one row per mission id survives (merge, not append), `prog` never
  regresses, and progress moves (or the rolling pool spawns replacements).
- `test_maintenance_engine_roundtrip` — seeds a 2-hour, 4-robot
  `robot_telemetry` window where only AMR-03's motor temp trends
  (+3 °C/hr); `MaintenanceSink.fetch_telemetry` reads it back through real
  HTTP, `MaintenanceEngine.observe` opens exactly one *drive motor*
  finding, and the written row carries every `0004_maintenance.sql`
  column within its CHECK constraints (`component`, `state`, `confidence`
  0–1, `rul_days`, `action`, `created_at`). A fresh engine seeded from
  `fetch_open_findings()` dedups instead of double-opening; a later
  healthy window PATCHes the same row to `Cleared` + `cleared_at`
  (never deletes).

## 3. Cross-component consistency (v0.5 checks)

Consistent (verified, no change needed):

- **Sim missions → `missions` schema**: `_missions_snapshot()` emits exactly
  `id, name, robots, state, prog, eta, created_at` — the full
  `0001_init.sql` column set; `robots` is a JSON list (jsonb column),
  `prog` an int, `eta`/`state` text with the schema's `'—'`/`'Queued'`
  defaults as values. Upserted with `on_conflict=id` + merge-duplicates
  (verified in e2e). Console `pullMissions()` maps the same columns and
  `insertMission()` POSTs only schema columns; console-minted ids
  (`M-` + base-36 time suffix) cannot collide with the sim's zero-padded
  `M-001…` sequence.
- **MaintenanceEngine → `0004_maintenance.sql`**: open rows carry every
  column; `component` values are exactly the migration's CHECK set
  (`drive motor`/`battery`/`drivetrain`), `state` only `Open`/`Cleared`,
  `confidence` clamped to [0, 1], clears PATCH rather than delete, and
  deterministic `MF-XXXX` ids keep retried upserts idempotent. The sink's
  telemetry read (`robot_id,ts,battery,speed,motor_temp,status`) selects
  only columns defined in `0003_telemetry.sql`.
- **Notifier reads real columns**: alert events use
  `id, sev, msg, src, tlabel` with `ack=eq.false` — all in the `alerts`
  DDL; incident events use `id, sev, title, impact` with `state=eq.Open` —
  all in the `incidents` DDL. The `sev=in.(crit,serious)` filter matches
  the real severity vocabulary: sim alerts emit `crit`, detector incidents
  emit `crit` (fault) and `serious` (estop), and both values are in the
  console's badge set.
- **MCP tools ↔ Toolbox**: all six tools (`fleet_summary`, `query_robots`,
  `query_alerts`, `query_incidents`, `query_commands`, `robot_history`)
  delegate to existing `Toolbox` methods whose signatures accept every
  exposed parameter; results serialize the full `ToolResult` (data +
  `source_id` + ts), and `TransportError` becomes a structured JSON error
  instead of a protocol failure. Covered by `copilot/tests/test_mcp_server.py`.
- **Console fetches ↔ tables**: every `Sync.q()` path targets a table
  defined in the checked-in migrations (`robots`, `alerts`, `incidents`,
  `missions`, `maintenance_findings`, `commands`, `fleet_meta`,
  `robot_telemetry`) with existing columns; `maintenance_findings` and
  `missions` reads tolerate the tables being absent (graceful fallback to
  demo data).

Inconsistency found and **fixed** (all suites re-run green after):

1. **Console maintenance card ignored the real `finding` column**
   (`console/index.html`). The predicted-failure card rendered
   `f.prediction || f.pred || f.msg`, but `0004_maintenance.sql` (and the
   engine) name the human-readable trend text `finding` — live rows would
   have shown the generic "anomaly detected" placeholder instead of
   "Motor temp trending +3.0 °C/hr". `f.finding` is now first in the
   fallback chain.

## 4. Changes made in this pass

- `e2e/fakerest.py` — `missions` + `maintenance_findings` tables.
- `e2e/test_e2e.py` — 3 new tests (missions publish/upsert-progress,
  maintenance open→seed-dedup→clear round-trip), 10 → 13.
- `console/index.html` — maintenance card reads the real `finding` column.
- `CHANGELOG.md` — v0.5.0 entry.
- `README.md` — schema block (+missions, +maintenance_findings), component
  table rows for notifier and detector `--maintenance`, updated quickstart
  (notifier + maintenance steps, notifier install/tests), new v0.5 section
  (missions, predictive maintenance, notifier, MCP server).
- This report.

## 5. Known gaps

- **No live end-to-end run**: Supabase, MQTT brokers, Twilio, webhooks and
  LLM APIs are unreachable offline. The fake PostgREST covers
  upsert/filter/patch shapes, but real PostgREST behaviour (RLS, CHECK
  constraints, jsonb coercion, CORS from `file://`), real webhook/WhatsApp
  delivery, and LLM tiers 1–2 remain untested against real services.
- **MCP server exercised in-process only**: `create_server()` +
  tool-function round-trips are tested; an actual stdio MCP host session
  (e.g. Claude Desktop) has not been driven offline.
- **No SQL migration for `incidents`** (unchanged from v0.4): its
  authoritative DDL lives only in Supabase; a checked-in
  `000N_incidents.sql` would make the contract enforceable.
- **Missions writes race between sim and console**: the sim upserts its
  snapshot every tick and the console can insert operator missions; the
  sim never touches console-minted ids, but console edits to sim missions
  would be overwritten next tick (single-writer per id today — benign).
- **Console tests are structural** (regex + `node --check`), not
  behavioural; missions/maintenance rendering is verified by parse +
  consistency review only.
- `pip install --break-system-packages` was used (root container); use a
  venv in normal development.
