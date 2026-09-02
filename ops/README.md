# yantraops

One-command orchestrator for the YantraFleet demo stack.

```bash
pip install -e ops/
python -m yantraops up            # full zero-cloud demo (loopback backend)
python -m yantraops status        # ping the running stack, print a table
```

## What `up` starts

| # | Service      | Command                                        | Backend wiring            |
|---|--------------|------------------------------------------------|---------------------------|
| 0 | backend      | in-process `e2e/fakerest.py` (loopback mode)   | 127.0.0.1, ephemeral port |
| 1 | yantrasim    | `python -m yantrasim --supabase --url --key`   | CLI flags                 |
| 2 | yantradetect | `python -m yantradetect --interval 5`          | `SUPABASE_URL/KEY` env    |
| 3 | yantranotify | `python -m yantranotify --dry-run`             | `SUPABASE_URL/KEY` env    |
| 4 | sarathi      | `python -m uvicorn sarathi.app:app` (ephemeral port) | `SUPABASE_URL/KEY` env |
| 5 | console      | `python -m http.server` on `console/` (ephemeral port) | `?supa=...&key=...` URL params |

In `--loopback` mode (the default) the fake PostgREST starts **first**, then
every child is pointed at it — no cloud, no DNS, no credentials. The startup
banner prints each service plus the console URL with `?supa=...&key=...`
query params already filled in, so opening it in a browser talks to the same
backend.

## Modes and flags

```
python -m yantraops up [--loopback|--supabase] [--no-copilot] [--duration N]
                       [--url URL] [--key KEY]
                       [--sim-interval S] [--detect-interval S] [--notify-interval S]
                       [--state-file PATH] [--quiet]
python -m yantraops status [--state-file PATH]
```

* `--loopback` (default): zero-cloud demo against the in-process fake PostgREST.
* `--supabase`: real Supabase; config precedence is flag > env
  (`SUPABASE_URL`/`SUPABASE_KEY`) > the client-safe defaults embedded in the
  packages.
* `--no-copilot`: skip the sarathi FastAPI service.
* `--duration N`: shut the stack down cleanly after N seconds (handy for
  demos and CI).
* `--state-file`: where `up` records ports/pids (default
  `~/.yantraops-state.json`, override with `YANTRAOPS_STATE`). `status` reads
  the same file, pings every port, and exits non-zero if anything is down.
  The file only ever contains the client-safe anon key.

## Shutdown

Ctrl-C or SIGTERM terminates the children in reverse start order, waits up
to 8 s, kills any straggler, then stops the loopback backend and removes the
state file. `--duration` takes the same path.

## Tests

```bash
python -m pytest ops/tests -q
```

The loopback smoke test starts the whole stack on ephemeral ports, polls the
fake backend until robot rows and an alert/incident exist, asks sarathi a
question and asserts an offline-tier grounded answer, then asserts every
child exited cleanly. No hardcoded ports anywhere.
