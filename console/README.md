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
