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

## Configuration

All config lives in `index.html` (documented in the comment block at the top):

- `Copilot.SARATHI_URL` — copilot endpoint, default `http://localhost:8001/ask`
- `Copilot.SARATHI_TIMEOUT_MS` — request timeout, default `4000`
- `SUPA_URL` / `SUPA_KEY` — optional Supabase sync for multi-tab live viewing
  (leader-elected sim; publishable anon key, client-safe). If unreachable, the
  console runs its local simulation and shows "Local sim (offline)" in the
  header.

## Tests

Offline pytest suite (no network, no browser) verifies the file structure, the
sarathi bridge wiring, the untouched fallback engine, and that the inline
script parses under `node --check`:

```bash
pytest tests/ -v
```
