# Contributing to YantraFleet

## Setup
```bash
pip install -e core -e sim -e connector
pip install -r copilot/requirements.txt
```

## Rules (the Quantum Lab standard)
1. Every change ships with offline tests — no test may touch the network
   (use `httpx.MockTransport`). `supabase.co` is unreachable in CI by design.
2. All robot statuses use the canonical vocabulary in `core/yantracore` —
   never invent a new spelling; extend the enum + migration instead.
3. Robot-affecting actions go through the `commands` table (approval gate),
   never direct state writes.
4. Run before pushing:
   ```bash
   (cd core && python -m pytest tests -q)
   (cd sim && python -m pytest tests -q)
   (cd connector && python -m pytest tests -q)
   (cd copilot && python -m pytest tests -q)
   node scripts/check_console.mjs
   ```
