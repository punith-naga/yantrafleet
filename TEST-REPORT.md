# YantraFleet — Release Verification Report

Date: 2026-08-26 · Environment: Linux, Python 3.11, pytest 9.1, Node available
Verifier: release-verification pass over sim/, connector/, copilot/, console/

## 1. Test runs

Dependencies installed per component (`pip install -e sim[mqtt,dev]`,
`pip install -e connector[dev]`, `pip install -r copilot/requirements.txt`).

| Component  | Suite                       | Result        |
|------------|-----------------------------|---------------|
| sim        | `sim/tests` (6 files)       | **43 passed** |
| connector  | `connector/tests` (3 files) | **32 passed** |
| copilot    | `copilot/tests`             | **18 passed** |
| console    | `console/tests` (structure, sarathi bridge, fallback, `node --check`) | **9 passed** |

**Total: 102 passed, 0 failed.** All suites run fully offline (httpx
MockTransport / StaticTransport / stdlib only). No pre-existing failures were
found; fixes below were consistency fixes, and all suites were re-run green
after them.

## 2. Console inline-script check

`console/index.html` contains exactly one inline `<script>` block. It was
extracted and passed `node --check` (also enforced continuously by
`console/tests/test_console.py::test_inline_script_parses`). Re-verified
after the edits below.

## 3. Cross-component consistency

Checked against the shared schema
`robots(id, vendor, status, battery, pos, speed, task_kind, health,
motor_temp, tasks_done, fault_msg)`:

Consistent (no change needed):
- **Columns**: sim `robot_row()`, connector `translate_state()`, the console's
  `Sync.push()`, and copilot's `query_robots`/`get_fleet_summary` all use
  exactly the schema columns (plus `updated_at`). Connector correctly omits
  `motor_temp`/`tasks_done` when the vendor extension is absent so upserts
  never clobber other writers.
- **Tables**: all writers/readers agree on `robots`, `alerts(id,sev,msg,src,
  tlabel,ack,created_at)`, `fleet_meta(id=1,...)`; copilot additionally reads
  `incidents`, which the console patches — compatible.
- **Supabase project**: identical URL + publishable key defaults in sim,
  connector, copilot, and console; all overridable via
  `SUPABASE_URL`/`SUPABASE_KEY`.
- **Copilot /ask contract**: returns `{answer, evidence[], tier}` with tiers
  `grounded|llm_only|offline`; the console's `tierPill()` maps exactly those
  ids (plus the spec aliases `live_agent`/`model_only`).

Inconsistencies found and **fixed**:
1. **Status vocabulary** — sim writes `working/moving/safety_stop/degraded`,
   connector writes `estop/active`, but the console's `STATUS` display map
   only knew `active|charging|idle|fault|paused`; an unknown status crashed
   `stBadge()`/`updateMap()` (destructuring `undefined`). Fix (console/
   index.html): added `ST_NORM` normalisation (`working/moving/transit/
   degraded→active`, `estop/safety_stop→fault`, unknown→`idle`), applied in
   `Sync.pull()`, plus defensive `STATUS[...]||STATUS.idle` lookups. The
   writers' richer VDA-style vocabulary is intentional (their tests pin it),
   so normalisation lives at the display edge.
2. **Position units** — sim publishes `pos` in metres (grid ≈ 28×16 m) while
   the console's map uses SVG pixels (≈960×420). Follower mode would have
   drawn all robots in the top-left corner. Fix: `posToSvg()` heuristic in
   the console scales metre-range coordinates into the map viewport.
3. **Null-tolerance in console pull** — connector rows may carry `null`
   battery/pos or omit `motor_temp`/`tasks_done`; the console coerced these
   with `+d.x` → `NaN`. Fix: `Sync.pull()` now keeps the previous value when
   a field is null/absent.
4. **Copilot entry point** — the documented quickstart command
   `py -m uvicorn sarathi.server:app --port 8001` pointed at a module that
   did not exist (the app lives in `sarathi/app.py`). Fix: added
   `copilot/sarathi/server.py` re-exporting `app` (import verified).

## 4. Known gaps

- **No live end-to-end run**: Supabase, MQTT brokers, and LLM APIs are
  unreachable in this offline environment. Everything network-facing is
  exercised via mock transports; the actual PostgREST calls, CORS from
  `file://`, and LLM tiers 1–2 are untested against real services.
- **Status vocabulary is normalised, not unified**: the DB stores whichever
  writer's vocabulary ran last (`working` vs `active`, `estop` vs
  `safety_stop`). Copilot answers echo the raw stored value, and
  `get_fleet_summary().faulted_ids` counts only `status=="fault"` — an
  `estop`/`safety_stop` robot is not counted as faulted. Consumers all
  render safely, but a shared status enum (or moving normalisation into the
  writers) would be cleaner for v0.2.
- **Console `posToSvg` is a heuristic** (coords ≤ 40 treated as metres); a
  writer legitimately publishing small pixel coordinates would be rescaled.
  Fine for the current map, but the schema should eventually declare units.
- **Console tests are structural** (regex + `node --check`), not behavioural
  — no DOM/browser test of the render paths.
- **Multi-writer contention**: sim, connector, and a leader console all
  upsert `robots`; last-write-wins per row is by design, but concurrent
  writers will visibly fight. `fleet_meta.writer_id` leader election covers
  the console only.
- `pip install --break-system-packages` was used (root container); use a
  venv in normal development.
