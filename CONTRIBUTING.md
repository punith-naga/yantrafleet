# Contributing to YantraFleet

## Setup

```bash
pip install -e core -e sim -e connector -e detector -e notifier -e ops
pip install -r copilot/requirements.txt
pip install pytest
```

For the console browser tests only: `pip install playwright pytest-playwright`
and a Chromium binary (see the suite matrix below).

## Rules (the Quantum Lab standard)

1. Every change ships with offline tests — no test may touch the network
   (use `httpx.MockTransport`; the e2e suite talks only to the in-process
   fake PostgREST on 127.0.0.1). `supabase.co` is unreachable in CI by design.
2. All robot statuses use the canonical vocabulary in `core/yantracore` —
   never invent a new spelling; extend the enum + a migration instead.
3. Robot-affecting actions go through the `commands` table (approval gate),
   never direct state writes.
4. Schema changes are new numbered files in `supabase/` (idempotent SQL);
   never edit an already-applied migration — `yantraops migrate` checksums
   them.

## Test suites

Nine suites, all offline. Run each **from its own directory** (test module
basenames collide across components):

| Suite | Directory | Run | Notes |
|---|---|---|---|
| core | `core/` | `python -m pytest tests -q` | canonical statuses, site helper |
| sim | `sim/` | `python -m pytest tests -q` | VDA emission, world, commands, missions |
| connector | `connector/` | `python -m pytest tests -q` | translate/dedup/sink, MQTT, MCAP import, command publisher |
| copilot | `copilot/` | `python -m pytest tests -q` | tiers with fake LLMs, grounding guard, MCP |
| detector | `detector/` | `python -m pytest tests -q` | incident + maintenance engines |
| notifier | `notifier/` | `python -m pytest tests -q` | channels, retries, digests, signing |
| ops | `ops/` | `python -m pytest tests -q` | orchestrator smoke (spawns real subprocesses on ephemeral ports), migrate, doctor |
| console | `console/` | `python -m pytest tests -q` | static checks (need Node for the inline-script syntax pass) **+ Playwright/Chromium browser tests** — install `playwright pytest-playwright`, then either `python -m playwright install chromium` or point `PLAYWRIGHT_BROWSERS_PATH` at a preinstalled cache (e.g. `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python -m pytest tests -q`). The browser tests skip themselves cleanly when no Chromium is available. |
| e2e | `e2e/` | `python -m pytest tests -q` | full stack over localhost HTTP vs the fake PostgREST |

Before pushing, run every suite you touched plus e2e, and:

```bash
node scripts/check_console.mjs
```

Update `TEST-REPORT.md` totals when your change adds or removes tests.

## File ownership (parallel work convention)

Multiple people (and agents) work this repo in parallel. To keep merges
trivial we partition by file, not by feature:

- **Claim a disjoint file set up front.** A workstream owns whole modules or
  whole files (e.g. "detector/ + supabase/0006"), declared in the PR/issue.
  Two open workstreams must never share a file — if you need to touch a file
  someone else owns, ask them to make the change or wait for their merge.
- **Cross-module contracts change in `core/` (or `supabase/`) first.** New
  statuses, row shapes, or shared helpers land as their own small PR that
  everyone rebases on, instead of leaking a private copy into a module.
- **One directory = one owner at a time** also applies to docs: e.g.
  `docs/index.html` and `docs/SECURITY.md` can be written concurrently by
  different authors because they are separate files.
- The console is a **single file** (`console/index.html`) by design — treat
  it as one ownership unit; coordinate before parallel edits, they will not
  merge cleanly.
