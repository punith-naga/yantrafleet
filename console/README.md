# YantraFleet Console

Single-file fleet operations console (`index.html`) — the FleetMind live demo
app extended with a bridge to the **sarathi** copilot service. No build step,
no dependencies.

## How to open

Option 1 — just open the file:

```bash
xdg-open index.html        # linux
open index.html            # macOS
```

Option 2 — serve it (recommended, avoids any `file://` quirks and keeps the
page on plain http so the localhost copilot call is not blocked as mixed
content):

```bash
cd console
python3 -m http.server 8080
# then browse to http://localhost:8080/
```

Everything works offline out of the box: the console runs its own in-browser
fleet simulation (10 robots, incidents, missions, analytics). Press `Cmd/Ctrl+K`
to open the Copilot panel.

## How it pairs with the sarathi service

The Copilot module tries the live agent first and falls back to its built-in
local answer engine on any failure — the console never breaks if sarathi is
not running.

```
Copilot.ask(question)
  ├─ 1) POST http://localhost:8001/ask   {"question": "..."}   (4 s timeout)
  │      response: {"answer": str,
  │                 "evidence": [str | {"label": str, "ref": str}],
  │                 "tier": "grounded" | "llm_only" | "offline"}
  │      → answer rendered in the chat, evidence as clickable-style chips,
  │        tier as a small pill: "live agent" / "model only" / "offline"
  └─ 2) on ANY failure (service down, non-2xx, timeout, malformed JSON)
         → the original local rule-based engine answers from the live
           in-browser sim state, exactly as in the standalone demo.
```

Start the sarathi service (see `../copilot/`) on port 8001, then just ask a
question in the Copilot panel — no console configuration needed. The tier pill
tells you which path answered:

| Pill         | Meaning                                                        |
|--------------|----------------------------------------------------------------|
| `live agent` | sarathi tier-1: LLM agent grounded in live Supabase fleet data |
| `model only` | sarathi tier-2: LLM answer without live data (lower confidence)|
| `offline`    | sarathi tier-3: deterministic template answers from tool data  |
| *(no pill)*  | sarathi unreachable → console's built-in local engine answered |

## Incident replay (real telemetry, v0.3)

Opening an incident on the **Incidents** view now tries to replay the actual
recorded telemetry of the incident's source robot:

- When the Supabase sync is connected (`Sync.mode !== 'local'`) and the
  incident row has a `created_at`, the console fetches `robot_telemetry` for
  that robot over the window `[start − 2 min, start + max(dur, 10) min]`
  (PostgREST, `order=ts.asc`, `limit=2000`).
- With **≥ 10 samples**, the existing replay UI is driven by real data:
  - map marker + trail from each row's `pos` (metre coordinates are scaled
    into the SVG floorplan via the same `posToSvg` heuristic the live map
    uses; missing fixes are held at the last known position, never invented);
  - the two charts become **battery (%)** and **speed (m/s)** traces with a
    shared vertical time cursor;
  - timeline dots come from that robot's `alerts` rows inside the window —
    click a dot to jump the scrubber there.
- Samples are downsampled to ≤ 300 points with LTTB (largest-triangle
  three-buckets), so spikes stay visible while scrubbing stays cheap.
- With **< 10 samples**, or on any fetch failure, or offline: the scripted
  INC-1042 demo replay is kept as a fallback and a small note reads
  *"scripted demo replay (no telemetry recorded)"*.

## Real data in Missions / Maintenance / Analytics (v0.4)

When the Supabase sync is connected (header chip shows "Cloud: LIVE"), three
formerly all-mock views pull or compute real data; offline they keep the
original local-sim demo content unchanged.

- **Missions** — rows come from the `missions` table (pulled every 3rd sync
  cycle, ~6 s). *＋ New mission* prompts for a name and inserts a Queued row
  `{id: "M-<suffix>", robots: [], state: "Queued", prog: 0}` locally and (when
  live) into the cloud table, so other tabs see it. The intent-based tasking
  card stays a mock of the planned Lattice-style planner.
- **Maintenance** — the "Predicted failures" card tries the
  `maintenance_findings` table and renders open findings with their
  `rul_days` / `confidence`; if the table doesn't exist (or returns nothing)
  it gracefully falls back to the demo tickets and is labelled `demo`. The
  robot-health table is always driven by the (live-synced) robots rows.
- **Analytics** — tiles are labelled honestly: *Tasks completed*
  (Σ `robots.tasks_done`) and *Fleet utilization* (active / total robots) are
  marked `computed` when live; *Time lost by cause* becomes
  Σ `incidents.dur` grouped by title keyword (localization / charge / other)
  when incident durations exist; SLA, cost-per-task and the ROI table are
  marked `demo` (illustrative). The throughput chart was already real.
- **Alerts** — acks already sync; additionally, when more than 5 alerts are
  unacked, a text-only hint chip appears above the Live-alerts feed noting
  that a notifier would page the on-call operator.

## Configuration

All config lives in `index.html` (documented in the comment block at the top):

- `Copilot.SARATHI_URL` — copilot endpoint, default `http://localhost:8001/ask`
- `Copilot.SARATHI_TIMEOUT_MS` — request timeout, default `4000`
- `SUPA_URL` / `SUPA_KEY` — optional Supabase sync for multi-tab live viewing
  (leader-elected sim; publishable anon key, client-safe). If unreachable, the
  console runs its local simulation and shows "Local sim (offline)" in the
  header.

The sync backend is also configurable per-tab via URL query params (defaults
unchanged when absent), so the same file can point at any PostgREST-compatible
endpoint — e.g. a local fake for testing:

```
index.html?supa=<PostgREST base url>&key=<anon key>&site=<site id>
```

- `supa` overrides `SUPA_URL`, `key` overrides `SUPA_KEY`
- `site` is exposed as `window.SITE` (reserved for per-site filtering; the
  console does not filter by it yet)

## Tests

Two pytest suites live in `tests/`:

- `test_console_static.py` — offline checks (no network, no browser): file
  structure, sarathi bridge wiring, the untouched fallback engine, and that
  the inline script parses under `node --check`.
- `test_console.py` — real-browser suite: headless Chromium (Python
  Playwright) drives the console against the in-process fake PostgREST from
  `e2e/fakerest.py` (seeded robots / alerts / commands / incidents; everything
  on localhost sockets). Covers boot without JS errors, the cloud chip going
  LIVE, backend rows rendering, alert acks and command approve PATCHing the
  backend, `cmdRobot()` queuing pending commands, and the offline fallback to
  the local sim. Skips itself cleanly when no Chromium binary is available.

```bash
pip install playwright pytest-playwright   # browser suite deps
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers pytest tests/ -v   # if preinstalled there
```
