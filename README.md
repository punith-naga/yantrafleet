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
incidents(...)  — read/patched by console and copilot
```

## Components

| Directory    | Package       | What it does |
|--------------|---------------|--------------|
| `sim/`       | `yantrasim`   | Deterministic 10-AMR warehouse simulator (3 vendors, 8x5 waypoint grid, chargers, faults). Emits VDA 5050 v2.1 `state` messages; publishes to Supabase (default), MQTT (`[mqtt]` extra), or stdout. |
| `connector/` | `yantrabridge`| Pure VDA 5050 v2.1 → Supabase row translation, alert dedup (inactive→active edges, battery hysteresis), and a batched PostgREST sink. Sources: JSONL file or MQTT. |
| `copilot/`   | `sarathi`     | FastAPI service (`POST /ask` on :8001) that answers fleet questions with cited evidence. Degrades through tiers: `grounded` (LLM + live tools) → `llm_only` → `offline` (template answers, no LLM needed). |
| `console/`   | —             | Single-file web console (`index.html`, no build step): live map, robot detail, alerts, incidents, and a Copilot panel that calls sarathi first and falls back to a local rule engine. |

## Quickstart (Windows)

Prereqs: Python 3.11+, a modern browser. From the repo root:

```bat
:: 1) install
pip install -e sim -e connector
pip install -r copilot\requirements.txt

:: 2) start the simulator (writes robots/alerts/fleet_meta to Supabase)
py -m yantrasim --supabase

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
cd sim & py -m pytest
cd connector & py -m pytest
cd copilot & py -m pytest
cd console & py -m pytest   :: needs Node for the inline-script syntax check
```

See `TEST-REPORT.md` for the latest verification run.
