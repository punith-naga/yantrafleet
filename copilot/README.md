# sarathi — YantraFleet Ops Copilot

Evidence-grounded Q&A service for the YantraFleet robot fleet. FastAPI on
port **8001**, one endpoint:

```
POST /ask   {"question": "..."}
        ->  {"answer": "...", "evidence": [{"label", "ref"}], "tier": "...", "latency_ms": 123}
GET  /health
```

Always HTTP 200 — clients render the `tier` caveat, never an error page.

## Three-tier degradation ladder

| Tier | Name | Needs | Behaviour |
|---|---|---|---|
| 1 | `grounded` | LLM key + Supabase | litellm agent loop over 4 tools; full evidence; system prompt forbids inventing numbers and a ground-check rail verifies every numeric token in the answer against tool data (flags unverified figures). |
| 2 | `llm_only` | LLM key (Supabase down) | Direct LLM answer, conceptual only, refuses live numbers, `evidence: []`, prefixed `[lower confidence — live fleet data unavailable]`. |
| 3 | `offline` | nothing (zero keys) | Intent classified by keywords/regex, tools executed directly, answer rendered from templates — every number is **computed** from tool data, no LLM at all. If Supabase is also down, an honest "no data" answer. |

Ladder: no LLM key → tier 3. Tier 1 attempted first; `TransportError`
(Supabase down) → tier 2; LLM failure → tier 3.

## Tools (all read Supabase PostgREST via a swappable transport)

- `get_fleet_summary()` — counts by status, avg/lowest battery, faulted ids, unacked alerts by sev, throughput, sim minute.
- `query_robots(status, vendor, max_battery, min_battery, robot_id, order, limit)`
- `query_alerts(sev, ack, limit)`
- `query_incidents(state, sev, limit)`

Every tool result carries a `source_id` (`tool:argshash:timestamp`) — the
citation key surfaced in `evidence[].ref`. Tools echo their filters into the
result data so even thresholds quoted in answers are grounded.

Transports (`sarathi/transport.py`):
- `SupabaseTransport` — httpx against `{SUPABASE_URL}/rest/v1` with the anon key.
- `StaticTransport` — in-memory snapshot honouring eq/neq/lt/lte/gt/gte/is,
  `order`, `limit` (used by tests and offline demos).

## Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `SUPABASE_URL` | `https://flwyvhsmgrrqpmhcqlzd.supabase.co` | demo project |
| `SUPABASE_KEY` | `sb_publishable_…` | anon/publishable key (client-safe) |
| `GEMINI_API_KEY` | — | enables tiers 1-2 with `gemini/gemini-2.5-flash` (free tier) |
| `OPENAI_API_KEY` | — | fallback provider, `gpt-4o-mini` |
| `SARATHI_MODEL` | auto | any litellm model id; overrides auto-detection |

With **no** keys set the service still works fully at tier 3.

## Run

```bash
pip install -r requirements.txt   # add --break-system-packages if needed
uvicorn sarathi.app:app --port 8001
curl -s localhost:8001/ask -X POST -H 'content-type: application/json' \
     -d '{"question":"Which robots are low on battery?"}'
```

CORS is wide open (credentials disabled), so a `file://` console can call it
directly (the `null` origin is covered by `*`).

## Tests & golden evals (fully offline)

`eval/golden.json` holds 8 golden Q/A cases written against the mocked fleet
fixture in `tests/conftest.py`. The pytest suite runs tier 3 against that
fixture and applies three deterministic evaluator layers: tool trajectory,
expected facts, and grounding (every number in the answer must exist in the
evidence data; every `ref` must be a real `source_id` from the run).

```bash
python -m pytest tests/ -q
```

No test touches the network — supabase.co is intentionally never called.

## Layout

```
sarathi/
  app.py        FastAPI app factory (+ CORS), POST /ask, GET /health
  service.py    degradation ladder orchestrator
  llm.py        tier 1 agent loop + tier 2, ground_check rail (litellm, lazy)
  offline.py    tier 3 intent classifier + templates (no LLM)
  tools.py      4 tools, ToolResult/source_id, OpenAI tool schemas
  transport.py  Transport interface, Supabase/Static/Failing transports
  config.py     env-resolved settings, model auto-detection
eval/golden.json  8 golden cases
tests/            offline pytest suite (transport, tools, golden, API, CORS)
```

## Caveats

- Tier 1/2 quality depends on the configured model; the ground-check rail
  appends a caution note rather than blocking when a figure can't be verified.
- The offline intent classifier is keyword-based by design (zero-dependency);
  unknown questions fall back to the fleet summary.
- `missions` has no dedicated tool yet (not needed by the golden set).
- Supabase RLS: the demo project is anon-readable; harden per the standard
  RLS checklist before production.
