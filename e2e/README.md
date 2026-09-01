# e2e — offline end-to-end loopback harness

Proves the whole YantraFleet stack **without Supabase**: a tiny in-process
fake PostgREST (`fakerest.py`, stdlib `http.server` on a `127.0.0.1`
ephemeral port, in-memory dict tables) stands in for the cloud, and the
*real* transports talk genuine httpx to it over localhost sockets. No
mocking of the components under test — only the database is faked.

```
yantrasim.FleetSim ── SupabaseTransport (real httpx) ──┐
                                                       ▼
                                    fakerest.FakePostgREST  (127.0.0.1:ephemeral)
                                                       ▲    tables: robots, alerts,
sarathi.Toolbox ── sarathi SupabaseTransport (httpx) ──┘    incidents, commands,
        │                                                   fleet_meta, robot_telemetry
   OfflineEngine (tier-3 keyword intents, no LLM)
```

## What `fakerest.py` implements

The PostgREST subset the components actually use:

| Verb  | Feature |
|-------|---------|
| POST  | bulk insert; `?on_conflict=col` + `Prefer: resolution=merge-duplicates` (upsert) or `resolution=ignore-duplicates` (insert-if-absent); plain insert auto-assigns a serial `id` |
| GET   | filters `eq. neq. in. lt. lte. gt. gte. is.`, `order=col.asc|desc`, `limit=N`, `select=` projection |
| PATCH | same filters select rows; JSON body merged in; 204 |

## What `test_e2e.py` proves (one shared world, tests in story order)

1. **Sim publish loop** — 8 ticks of `FleetSim(seed=9)` through
   `yantrasim.transports.supabase.SupabaseTransport`:
   * `robots` holds exactly 10 upserted rows, every `status` in the
     canonical vocabulary (`yantracore.CANONICAL`);
   * `robot_telemetry` accumulates downsampled history (every 3rd tick →
     20 rows), readable with `eq.`/`in.` filters (the replay query shape);
   * `alerts` are present (seed 9 deterministically faults AMR-09 at tick
     4 and clears it at tick 8) and re-posting them is idempotent under
     `ignore-duplicates`;
   * `fleet_meta` stays a single `id=1` heartbeat row.
2. **Human-in-the-loop gate** — a `pending` `pause AMR-01` command is
   inserted (console path), ignored by `poll_commands` while pending,
   approved via PATCH, then executed via `poll_commands` +
   `yantrasim.commands.apply_command`: the sim robot is `paused` *and*
   the row becomes `executed` with a grounded `note`; one more published
   tick round-trips `paused` into the `robots` table through the VDA
   translation.
3. **Copilot** — `sarathi.Toolbox` over sarathi's own httpx
   `SupabaseTransport` pointed at the fake, answered by `OfflineEngine`:
   * “what is waiting for approval?” lists the still-pending
     `charge AMR-02` and counts the executed pause;
   * “fleet status” numbers match the fake's tables row-for-row;
   * robot detail and alert questions are grounded too.

## Run

```sh
pip install --break-system-packages -r e2e/requirements.txt
pip install --break-system-packages -e ./core -e ./sim
python3 -m pytest e2e/ -v
```

Everything binds only `127.0.0.1`; no DNS, no network egress — safe for
fully offline CI.
