# YantraFleet

A small, end-to-end fleet-operations stack for multi-vendor AMR (autonomous
mobile robot) fleets. A simulator emits VDA 5050 v2.1 state, a connector
translates it into a shared Supabase schema, an AI copilot answers questions
grounded in that data, and a single-file web console visualises and controls
the fleet.

All four components share one Supabase (PostgREST) backend and one `robots`
row shape:

```
robots(id, vendor, status, battery, pos jsonb [x,y], speed, task_kind,
       health, motor_temp, tasks_done, fault_msg, updated_at)
alerts(id, sev, msg, src, tlabel, ack, created_at)
fleet_meta(id=1, writer_id, sim_min, throughput, updated_at)
robot_telemetry(id, robot_id, ts, battery, speed, motor_temp, status, pos)
incidents(id, sev, title, src, tlabel, state, impact, rca, fix, dur,
          created_at)  — written by the detector, read/patched by console
                         and copilot (rca/fix nullable)
commands(id, robot_id, cmd, status, requested_by, decided_by, ..., note)
```

## Components

| Directory    | Package       | What it does |
|--------------|---------------|--------------|
| `sim/`       | `yantrasim`   | Deterministic 10-AMR warehouse simulator (3 vendors, 8x5 waypoint grid, chargers, faults). Emits VDA 5050 v2.1 `state` messages; publishes to Supabase (default), MQTT (`[mqtt]` extra), or stdout. |
| `connector/` | `yantrabridge`| Pure VDA 5050 v2.1 → Supabase row translation, alert dedup (inactive→active edges, battery hysteresis), and a batched PostgREST sink. Sources: JSONL file or MQTT. |
| `copilot/`   | `sarathi`     | FastAPI service (`POST /ask` on :8001) that answers fleet questions with cited evidence. Degrades through tiers: `grounded` (LLM + live tools) → `llm_only` → `offline` (template answers, no LLM needed). |
| `console/`   | —             | Single-file web console (`index.html`, no build step): live map, robot detail, alerts, incidents (with recorded-telemetry replay), and a Copilot panel that calls sarathi first and falls back to a local rule engine. |
| `detector/`  | `yantradetect`| Incident detector: polls `robots`, maintains `incidents` — PagerDuty-style dedup with deterministic idempotent `INC-XXXX` ids, Prometheus-style pending window + clear hold, re-open window with flap counting, stale auto-resolve. `python -m yantradetect --interval 5` (or `--once`, `--dry-run`). |
| `e2e/`       | —             | Offline end-to-end suite: `fakerest.py` (in-process fake PostgREST on 127.0.0.1) + `test_e2e.py` driving sim transport → command gate → copilot toolbox → detector sink over real localhost HTTP, zero network egress. |

## Quickstart (Windows)

Prereqs: Python 3.11+, a modern browser. From the repo root:

```bat
:: 1) install
pip install -e core -e sim -e connector -e detector
pip install -r copilot\requirements.txt

:: 2) start the simulator (writes robots/alerts/fleet_meta/telemetry to Supabase)
py -m yantrasim --supabase

:: 2b) start the incident detector (optional; maintains the incidents table)
py -m yantradetect --interval 5

:: 3) start the copilot service (new terminal, from copilot\)
py -m uvicorn sarathi.server:app --port 8001

:: 4) open the console
start console\index.html
```

The console auto-elects leader/follower against `fleet_meta.writer_id`; with
the simulator running it follows the simulator's data. The Copilot panel uses
`http://localhost:8001/ask` (4 s timeout) and silently falls back to its
built-in rule engine when sarathi is not running.

Optional:

```bat
:: replay recorded VDA 5050 state through the connector
py -m yantrabridge --file connector\sample.jsonl

:: offline demo without Supabase
py -m yantrasim --stdout --ticks 20
```

Configuration: `SUPABASE_URL` / `SUPABASE_KEY` override the embedded
free-tier project in every component. Copilot LLM tiers activate with
`GEMINI_API_KEY` or `OPENAI_API_KEY` (or `SARATHI_MODEL`); without a key it
serves grounded template answers (`tier: offline`).

## Tests

Each Python component has an offline pytest suite (no network, no browser):

```bat
cd core & py -m pytest
cd sim & py -m pytest
cd connector & py -m pytest
cd copilot & py -m pytest
cd detector & py -m pytest
cd console & py -m pytest   :: needs Node for the inline-script syntax check
cd e2e & py -m pytest       :: offline end-to-end (fake PostgREST loopback)
```

Run each suite from its own directory (test module basenames collide across
components). See `TEST-REPORT.md` for the latest verification run.

## v0.4 — incident detector, real replay & e2e

Apply `supabase/0003_telemetry.sql` if you have not already (v0.3 schema).
Then:

1. **Detector** — `py -m yantradetect --interval 5` polls `robots` and
   opens/resolves `incidents` rows: a robot seen in `fault`/`estop` for 2
   consecutive polls opens an incident (`sev` crit/serious); recovery
   resolves it with duration and impact. Deterministic ids make retried
   writes idempotent; `--dry-run` prints actions without writing.
2. **Incident replay from recorded telemetry** — opening an incident in the
   console fetches the robot's `robot_telemetry` rows in a window around the
   incident's `created_at` and drives the replay map, scrubber, and
   battery/speed charts from real samples (alerts in the window become event
   dots). With fewer than 10 recorded samples the scripted INC-1042 demo
   replay is shown instead, with a note.
3. **Copilot telemetry** — ask sarathi about a robot's recent telemetry; the
   `query_telemetry` tool returns windowed samples plus battery/speed/
   motor-temp stats and status-transition counts.

## v0.2 — command gate & canonical statuses

Apply `supabase/0002_commands.sql` in the Supabase SQL Editor (after the v0.1
schema). Then:

1. In the console, robot actions (pause / send-to-charge / e-stop) no longer
   act directly when a live backend is connected — they queue as `pending`
   rows in the `commands` table.
2. The **Pending approvals** card on Overview lets an operator approve or
   reject each command (full audit: who asked, who decided, when, outcome).
3. A running `py -m yantrasim --supabase` polls approved commands, executes
   them in the world, and marks them `executed`/`failed` with a reason.
4. Robot statuses everywhere use one vocabulary (`core/yantracore`):
   `active · idle · charging · paused · estop · degraded · fault` — enforced
   by a DB CHECK constraint.

A live `yantrasim` feed outranks console tabs: consoles automatically become
viewers while the simulator (or a future robot adapter) is publishing.
