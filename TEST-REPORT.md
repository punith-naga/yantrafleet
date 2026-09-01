# YantraFleet — Release Verification Report (v0.4)

Date: 2026-09-01 · Environment: Linux, Python 3.11.15, pytest 9.1.1,
httpx 0.28.1, Node v22.22.2
Verifier: release-verification pass over core/, sim/, connector/, copilot/,
detector/, console/, e2e/

## 1. Test runs

Installed: `pip install -e core -e sim -e connector -e detector`,
`pip install -r copilot/requirements.txt -r e2e/requirements.txt`
(`--break-system-packages` in the root container; use a venv normally).
Both requirements files already existed.

| Component | Suite                                  | Result         |
|-----------|----------------------------------------|----------------|
| core      | `core/tests`                           | **4 passed**   |
| sim       | `sim/tests`                            | **51 passed**  |
| connector | `connector/tests`                      | **32 passed**  |
| copilot   | `copilot/tests`                        | **28 passed**  |
| detector  | `detector/tests`                       | **35 passed**  |
| console   | `console/tests` (structure + `node --check`) | **9 passed** |
| e2e       | `e2e/test_e2e.py` (fake PostgREST loopback)  | **10 passed** |

**Total: 169 passed, 0 failed.** All suites run fully offline. The e2e suite
uses real httpx over 127.0.0.1 sockets against `e2e/fakerest.py` (in-process
fake PostgREST); everything else uses mock/static transports or stdlib.

Note: suites must be run from each component's own directory — test module
basenames collide across components (`test_cli.py`, `test_sink.py` in both
sim and detector), so a single repo-root `pytest core sim ...` invocation
fails at collection. This matches CI, which runs per-directory.

`node scripts/check_console.mjs`: **OK** — 1 inline script block parses clean.

## 2. Cross-component consistency (v0.4 checks)

Consistent (verified, no change needed):

- **Detector → incidents schema**: `IncidentEngine` open rows carry
  `id, sev, title, src, tlabel, state, impact, dur, created_at`; patches set
  `state/dur/impact`. The console's incidents reader consumes exactly
  `id, sev, title, src, tlabel, state, impact, rca, fix, dur, created_at`
  (rca/fix nullable — see fix 2 below), and copilot's `query_incidents`
  filters on `state`/`sev`. `sev` values (`crit`/`serious`) are within the
  console's badge vocabulary (`crit`/`serious`/`warn`). Verified end-to-end
  by the new `e2e/test_e2e.py::test_detector_incident_roundtrip` (detector
  engine + `PostgRESTSink` against the fake PostgREST, read back with the
  console's column expectations, including upsert idempotency).
- **Console replay fetch ↔ robot_telemetry**: `loadRealReplay()` selects
  `ts,battery,speed,motor_temp,status,pos` with `robot_id=eq.` +
  `ts=gte./lte.` + `order=ts.asc&limit=2000` — all columns exist in
  `supabase/0003_telemetry.sql`, and the filter/order shapes are exercised
  against the fake in `test_telemetry_accumulates_downsampled`.
- **Copilot query_telemetry ↔ schema**: selects `*` from `robot_telemetry`
  with `robot_id=eq.`, `order=ts.desc`, `limit`; stats read only
  `battery/speed/motor_temp/status/ts`. Window anchored on the newest
  sample's `ts` (not wall clock), so simulated/replayed data works.
- **Canonical statuses**: sim writes only `yantracore.CANONICAL` (asserted in
  e2e), detector classifies via `yantracore.normalize` + `NOT_OPERATING`
  (legacy spellings like `safety_stop` covered by its tests), copilot
  summaries assert `by_status ⊆ CANONICAL`, and the console's `STATUS` map
  covers all 7 canonical values with `ST_NORM` normalisation at the display
  edge.

Inconsistencies found and **fixed** (all suites re-run green after):

1. **Detector open rows lacked `created_at`**
   (`detector/yantradetect/engine.py`). The console orders
   `incidents?order=created_at.desc`, the real-replay window is anchored on
   `inc.created_at` (without it, detector-created incidents could never show
   a real replay), and the detector's own `seed()` restores `opened_at` from
   `created_at` after a restart — but the row relied on a DB column default
   that the repo's migrations never define. The engine now emits
   `created_at` (ISO of `opened_at`) explicitly; deterministic, so upsert
   idempotency is preserved. Locked in by `test_open_row_fault` and the new
   e2e round-trip test.
2. **Console rendered `null` RCA for detector incidents**
   (`console/index.html`). Detector rows leave `rca`/`fix` null (RCA is the
   copilot's job), but the incident detail card interpolated them directly
   ("null" in the UI). The `Sync` incidents mapping now falls back to
   placeholder copy ("Automated detection … root-cause analysis pending" /
   generic recommended actions).

## 3. Changes made in this pass

- `detector/yantradetect/engine.py` — open rows include `created_at`.
- `detector/tests/test_engine.py` — assert `created_at` in the open row.
- `console/index.html` — null-safe `rca`/`fix` fallbacks in the incidents
  mapping.
- `e2e/test_e2e.py` — new `test_detector_incident_roundtrip` (detector →
  fake PostgREST → console-shape read-back, 9 → 10 tests).
- `CHANGELOG.md` — v0.4.0 entry (plus the previously missing v0.3.0 entry).
- `README.md` — schema block expanded (telemetry/incidents/commands),
  detector + e2e component rows, detector quickstart step, per-directory
  test instructions, v0.4 section (detector, real replay, query_telemetry).
- This report.

## 4. Known gaps

- **No live end-to-end run**: Supabase, MQTT brokers, and LLM APIs are
  unreachable offline. The fake PostgREST covers upsert/filter/patch shapes,
  but real PostgREST behaviour (RLS, CHECK constraints, `created_at` column
  defaults, CORS from `file://`) and LLM tiers 1–2 remain untested against
  real services.
- **No SQL migration for `incidents`**: the table predates the repo's
  migration files (created with the v0.1 dashboard schema); its authoritative
  DDL lives only in Supabase. The detector now sends every column it needs
  explicitly, but a checked-in `000N_incidents.sql` (with a CHECK on `state`
  and `sev`) would make the contract enforceable.
- **Detector vs console patch races**: the console's guided recovery patches
  `incidents.state='Resolved'` directly; a running detector will keep its own
  view and may patch duration afterwards. Last-write-wins per column — benign
  today, but a single writer of `state` would be cleaner.
- **e2e does not run the detector CLI loop or sarathi's FastAPI layer** —
  engine+sink and toolbox+offline engine are exercised in-process instead.
- **Console tests are structural** (regex + `node --check`), not behavioural;
  the replay module is verified by parse + consistency review only.
- `pip install --break-system-packages` was used (root container); use a
  venv in normal development.
