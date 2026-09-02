# YantraFleet in Docker

One image runs the whole loopback demo — simulator, incident detector,
notifier, Sarathi copilot, and the web console — against the embedded
in-memory backend. No Supabase, no keys.

## TL;DR (Linux)

```bash
# from the repo root
docker build -f docker/Dockerfile -t yantrafleet .
docker run --rm --init --network host yantrafleet
```

Watch for the `✔ READY` line and open the printed console URL
(`http://127.0.0.1:<port>/index.html?supa=...`) in your browser. Ctrl-C (or
`docker stop`) shuts the stack down cleanly.

Or with compose:

```bash
docker compose -f docker/docker-compose.yml up --build
```

## Why `--network host`? (the honest limitation)

`yantraops up` was designed as a local dev convenience: every service —
the fake PostgREST backend, the Sarathi API, and the console's HTTP
server — binds **127.0.0.1** on an **ephemeral port** chosen at startup,
and the console URL embeds the backend's `127.0.0.1:<port>` address as a
query parameter that *your browser* must be able to reach.

That defeats normal `docker run -p` port publishing twice over:

1. the ports are not known before the container starts, so there is
   nothing stable to `-p`-map (and nothing meaningful to `EXPOSE`);
2. even if the ports were mapped, the console URL would still point your
   browser at addresses that only exist inside the container's loopback.

Making the stack bind-configurable (a `YANTRA_BIND`/fixed-ports mode)
would require coordinated changes in `ops/`, `e2e/fakerest.py`, and the
console URL construction, so it is deliberately **not** patched in from
the Docker layer. With `--network host` the container shares the host's
loopback, and everything works exactly as it does when run natively.

### macOS / Windows (Docker Desktop)

`--network host` on Docker Desktop requires the *"Enable host networking"*
option (Settings → Resources → Network, Docker Desktop 4.34+). Even then,
loopback semantics differ per platform, so on macOS/Windows we recommend
the native install instead:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Run   # Windows
```

```bash
./install.sh --run                                          # macOS/Linux
```

## Headless / CI use

The image is still useful without a browser — as a smoke test that the
whole stack starts, reaches READY, and shuts down cleanly:

```bash
docker run --rm --init yantrafleet \
  python -m yantraops up --loopback --no-open --duration 30
```

(No `--network host` needed: everything stays inside the container.)

## Real Supabase instead of loopback

```bash
docker run --rm --init --network host \
  -e SUPABASE_URL=https://YOURPROJECT.supabase.co \
  -e SUPABASE_KEY=your-anon-key \
  yantrafleet python -m yantraops up --supabase --no-open
```

Apply the migrations in `supabase/` to your project first (they are not
baked into the image).

## Notes

- `HEALTHCHECK` runs `python -m yantraops status`, which pings every
  service port recorded in the orchestrator's state file.
- `--init` (or compose's `init: true`) gives the container a real PID-1
  reaper; `yantraops` spawns five child processes.
- The container runs as a non-root `yantra` user.
