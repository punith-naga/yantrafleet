# Changelog

## v0.7.0 — 2026-09-02
- **One-command real-Supabase onboarding**: `yantraops migrate --db-url ...`
  applies supabase/*.sql in order with tracking/checksums/dry-run;
  `up --supabase` preflights the schema and tells you exactly what to run.
- **`yantraops doctor`**: PASS/WARN/FAIL environment preflight ending in the
  exact next command.
- **Installers**: `install.ps1` (Windows, PS 5.1-safe) and `install.sh` —
  venv + all packages + doctor in one run (`-Run` starts the demo);
  `docker/` packaging for Linux/CI.
- **Console**: printable **Shift Report** (Print/Save-as-PDF + copy-as-
  Markdown), **command palette** (Ctrl/Cmd+K: navigate, jump to robot,
  ask Sarathi, ack all info; copilot moved to Ctrl/Cmd+J), **first-run
  guided tour** (5 steps, replay via ?).
- 318 tests green across 9 suites (27 console incl. 18 real-browser).

## v0.6.0 — 2026-08-26
- **`yantraops` one-command orchestrator**: `python -m yantraops up --loopback`
  boots the entire platform (fakerest backend, sim, detector, notifier,
  Sarathi, console server) with zero cloud/config — the 60-second demo;
  `--supabase` for the real backend; `status` subcommand; graceful shutdown.
- **Real browser tests**: 10 Playwright/Chromium tests drive the actual
  console against the fake backend (live chip, ack flow, command → approval
  round trip, offline fallback) + 9 preserved static tests.
- **LLM hardening**: injection seam + scripted-fake tests for tiers 1-2
  (agent loop, bounded tool calls, degradation on failure); **grounding
  guard** — numbers in LLM answers verified against tool payloads, responses
  carry grounding=verified|unverified|computed; richer /health.
- **Multi-site groundwork**: migration 0005 adds site_id (+indexes) across
  all data tables; writers stamp it via yantracore.site_id() (env
  YANTRA_SITE_ID); notifier filters per-site with --all-sites escape;
  console accepts ?site= (and ?supa=/&key= backend overrides).
- 278 tests green across 9 suites.

## v0.5.0 — 2026-09-01
- **Missions** (`sim/yantrasim`): the simulator keeps a rolling pool of up to
  3 concurrent missions (2–4 robots each, 10–20 planned tasks, deterministic
  rolling names like "Outbound wave #1"); task completions credit the owning
  mission (`Queued → Running → Done`, integer `prog` percent, clock-string
  ETA). Each tick the snapshot is upserted into the existing `missions`
  table (`on_conflict=id`, merge) so retries are idempotent; `Done` missions
  linger 6 ticks in the snapshot and persist forever in the table.
- **Predictive maintenance** (`detector/yantradetect`): new pure
  `MaintenanceEngine` reads a mixed-robot `robot_telemetry` window and emits
  open/clear actions for three heuristics — motor-temp linear trend
  (≥1.5 °C/hr, r²≥0.5, ≥6 samples → *drive motor*), battery drain per
  active-minute ≥1.5× fleet median (*battery*), late-vs-early active-speed
  decline ≥15% (*drivetrain*). Deterministic `MF-XXXX` ids, one open finding
  per (robot, component), clears PATCH `state=Cleared` (never delete).
  `MaintenanceSink` writes the new `maintenance_findings` table
  (`supabase/0004_maintenance.sql`); run with `python -m yantradetect
  --maintenance [--window-hours 6]`.
- **Notifier** (`notifier/`, package `yantranotify`): polls unacked
  `crit`/`serious` alerts and `Open` incidents and pushes them through
  console/webhook/WhatsApp (Twilio) channels with per-event dedup, optional
  `--state-file` persistence, digest batching, and `--dry-run`.
  `python -m yantranotify --interval 10`. Channels without credentials
  print instead of send — no secrets required.
- **MCP server** (`copilot/sarathi/mcp_server.py`): Sarathi's Toolbox
  exposed over the Model Context Protocol (official `mcp` SDK, stdio) as
  `sarathi-fleet` — tools `fleet_summary`, `query_robots`, `query_alerts`,
  `query_incidents`, `query_commands`, `robot_history`; every result is the
  full `ToolResult` JSON including the `source_id` citation key. See
  `copilot/README-MCP.md`. `python -m sarathi.mcp_server`.
- **Console**: Missions view goes live (pulls `missions` every ~6 s, local +
  POST insert for new missions), and the Maintenance view renders real
  `maintenance_findings` rows when present.
- **E2E**: fake PostgREST gains `missions` and `maintenance_findings`
  tables; 3 new tests prove the sim publishes schema-shaped mission rows
  (idempotent upserts, progress never regresses) and the maintenance
  engine round-trips open → dedup-seed → clear against real localhost HTTP.

## v0.4.0 — 2026-09-01
- **Incident detector** (`detector/`, package `yantradetect`): polls the
  `robots` table and maintains the `incidents` table with industry patterns —
  PagerDuty-style dedup (one open incident per robot, deterministic
  `INC-XXXX` ids so retried upserts are idempotent), Prometheus-style pending
  window and clear-hold hysteresis, re-open window with flap counting, and
  stale auto-resolve. Pure engine (`IncidentEngine.observe(rows, now) ->
  [Action]`, no I/O) + `PostgRESTSink`/`DryRunSink`. `python -m yantradetect
  --interval 5`, `--once`, `--dry-run`. 35 offline tests.
- **Offline end-to-end suite** (`e2e/`): `fakerest.py` is an in-process fake
  PostgREST (stdlib HTTP server: upserts with `on_conflict` + `Prefer`
  resolution, `eq./neq./in./lt./gt./is.` filters, `order`/`limit`/`select`,
  PATCH merges). `test_e2e.py` drives the real sim transport, the command
  approval gate, sarathi's toolbox, and the detector sink over genuine
  localhost httpx — 10 tests, zero network egress.
- **Copilot telemetry tool**: `query_telemetry(robot_id, minutes=30,
  limit=500)` reads `robot_telemetry` and returns rows + stats (battery
  min/max/avg, speed avg, motor-temp max, status transitions), with the time
  window anchored on the newest sample so replayed/simulated clocks work.
- **Console real incident replay**: opening an incident now fetches the src
  robot's recorded `robot_telemetry` around `created_at` and drives the
  replay map/scrubber/charts from real samples (LTTB-downsampled, positions
  forward-filled); the scripted INC-1042 demo remains the fallback when no
  telemetry is recorded.
- Consistency fixes from release verification: detector open rows now carry
  `created_at` (console orders incidents by it; engine `seed()` restores
  `opened_at` from it), and the console shows placeholder RCA/fix copy for
  detector-created incidents instead of `null`.

## v0.3.0 — 2026-08-31
- Telemetry history: `robot_telemetry` table (`supabase/0003_telemetry.sql`)
  sampled every Nth sim tick, with `purge_old_telemetry()` retention helper.
- Copilot approval awareness (`query_commands`) and Python 3.10 compatibility.

## v0.2.0 — 2026-08-26
- **Canonical status vocabulary** (`core/yantracore`): active | idle | charging |
  paused | estop | degraded | fault — enforced by a DB CHECK constraint
  (`supabase/0002_commands.sql`); sim and connector both emit it, the console
  displays estop/degraded distinctly.
- **Human-in-the-loop command gate**: console queues pause/resume/charge/estop
  as `pending` rows in the new `commands` table; an operator approves/rejects
  in the Overview "Pending approvals" card; the simulator polls approved
  commands, executes them in the world (robots actually pause/hold/route to
  charger/e-stop), and marks them executed/failed with a reason. Full audit
  trail preserved.
- **Writer precedence**: a live `yantrasim` feed always outranks console tabs
  for fleet_meta leadership; console tabs fall back to viewer mode.
- Project hygiene: git repo, MIT license, GitHub Actions CI (4 python suites +
  console syntax check), CONTRIBUTING, this changelog.

## v0.1.0 — 2026-08-26
- Initial four components built via multi-agent workflow: yantrasim (VDA 5050
  v2.1 simulator), yantrabridge (VDA→Supabase connector), sarathi (3-tier
  copilot service), console (single-file ops console). 102 offline tests.
