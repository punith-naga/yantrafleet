"""CLI: ``python -m yantraops up|status|migrate|doctor``.

    python -m yantraops up                       # zero-cloud loopback demo
    python -m yantraops up --supabase            # against real Supabase
    python -m yantraops up --mqtt                # real VDA 5050 MQTT wire
    python -m yantraops up --supabase --mqtt --broker host:1883 --no-sim
    python -m yantraops up --no-copilot          # skip the sarathi API
    python -m yantraops up --duration 30         # exit cleanly after 30 s
    python -m yantraops status                   # ping the running stack
    python -m yantraops doctor                   # environment preflight
    python -m yantraops migrate --db-url URL     # apply supabase/*.sql
    python -m yantraops audit-security           # backend/RLS security audit
    python -m yantraops grant-role --db-url URL --email you@x.com --role admin
    python -m yantraops sandbox-mint --origin marketing   # zero-signup demo
    python -m yantraops sandbox-list                      # what's live here
    python -m yantraops sandbox-reap                      # cron: purge expired
    python -m yantraops sandbox-serve --port 8088         # the "Try it" door
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .orchestrator import (DEFAULT_STATE_FILE, FleetStack,
                           preflight_supabase_schema, resolve_supabase)
from .status import run_status

try:  # single source of the release version: core/yantracore/version.py
    from yantracore import __version__ as YF_VERSION
except Exception:  # yantracore not installed — standalone ops checkout
    YF_VERSION = "dev"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantraops",
        description="One-command orchestrator for the Yantrika demo stack")
    p.add_argument("--version", action="version",
                   version=f"Yantrika {YF_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="start the whole stack")
    mode = up.add_mutually_exclusive_group()
    mode.add_argument("--loopback", action="store_true",
                      help="in-process fake PostgREST backend (default)")
    mode.add_argument("--supabase", action="store_true",
                      help="use real Supabase (flags/env/package defaults)")
    up.add_argument("--no-copilot", action="store_true",
                    help="skip the sarathi copilot API")
    up.add_argument("--mqtt", action="store_true",
                    help="robot data over a real MQTT VDA 5050 wire: broker + "
                         "yantrasim --mqtt + yantrabridge (needs ops[mqtt])")
    up.add_argument("--broker", default=None, metavar="HOST[:PORT]",
                    help="external MQTT broker, e.g. a customer mosquitto "
                         "(default with --mqtt: embedded broker on an "
                         "ephemeral port)")
    up.add_argument("--no-sim", action="store_true",
                    help="do not start the simulator — real robots publish "
                         "VDA 5050 to the broker (requires --mqtt)")
    up.add_argument("--duration", type=float, default=None, metavar="N",
                    help="exit cleanly after N seconds (default: run until Ctrl-C)")
    up.add_argument("--url", default=None, help="Supabase URL (supabase mode)")
    up.add_argument("--key", default=None, help="Supabase anon key (supabase mode)")
    up.add_argument("--sim-interval", type=float, default=None,
                    help="yantrasim tick interval seconds — an explicit "
                         "value here always wins; omit it to let the "
                         "child pick up a live public.app_config "
                         "SIM_INTERVAL, else its own hardcoded default (2)")
    up.add_argument("--detect-interval", type=float, default=None,
                    help="yantradetect poll interval seconds — an "
                         "explicit value here always wins; omit it to "
                         "let the child pick up a live public.app_config "
                         "DETECTOR_INTERVAL, else its own hardcoded "
                         "default (5)")
    up.add_argument("--notify-interval", type=float, default=None,
                    help="yantranotify poll interval seconds — an "
                         "explicit value here always wins; omit it to "
                         "let the child pick up a live public.app_config "
                         "NOTIFIER_INTERVAL, else its own hardcoded "
                         "default (10)")
    up.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE,
                    help=f"where to record ports/pids (default {DEFAULT_STATE_FILE})")
    up.add_argument("--no-open", action="store_true",
                    help="do not auto-open the console in the default browser")
    up.add_argument("--verbose", action="store_true",
                    help="full INFO logs from every child (default: WARNING)")
    up.add_argument("--quiet", action="store_true",
                    help="suppress child output and banner")

    st = sub.add_parser("status", help="ping the ports of a running stack")
    st.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE,
                    help=f"state file written by `up` (default {DEFAULT_STATE_FILE})")

    mig = sub.add_parser(
        "migrate", help="apply supabase/*.sql migrations to a real database")
    mig.add_argument("--db-url", default=None, metavar="URL",
                     help='Postgres connection string, e.g. "postgresql://'
                          'postgres:<password>@db.<ref>.supabase.co:5432/postgres" '
                          "(see supabase/README.md)")
    mig.add_argument("--dir", type=Path, default=None, metavar="DIR",
                     help="migrations directory (default: <repo>/supabase)")
    mig.add_argument("--include-opt-in", action="store_true",
                     help="also apply OPT-IN lockdown migrations (0006/0007)")
    mig.add_argument("--dry-run", action="store_true",
                     help="connect, list what would be applied, change nothing")
    mig.add_argument("--force", action="store_true",
                     help="reapply migrations whose checksum changed since "
                          "they were first applied")
    mig.add_argument("--print-order", action="store_true",
                     help="just print the filename order (no DB, no psycopg)")

    sub.add_parser(
        "doctor", help="environment preflight: PASS/WARN/FAIL checks")

    from .audit import add_audit_parser
    add_audit_parser(sub)

    from .grant_role import DEFAULT_SITE, ROLES
    gr = sub.add_parser(
        "grant-role",
        help="grant/update a user's RBAC role (0007) — closes the "
             "first-admin bootstrap gap without hand-edited SQL")
    gr.add_argument("--db-url", default=None, metavar="URL",
                    help="Postgres connection string, same one you used for "
                         "`migrate` (see supabase/README.md)")
    gr.add_argument("--email", default=None,
                    help="the auth.users email to grant the role to — they "
                         "must have already signed up (or signed in once) "
                         "for a matching auth.users row to exist")
    gr.add_argument("--role", default="admin", choices=ROLES,
                    help="role to grant (default: admin — the usual reason "
                         "to reach for this command is bootstrapping the "
                         "first admin right after applying 0007_rbac.sql)")
    gr.add_argument("--site", default=DEFAULT_SITE,
                    help=f"site_id to grant the role at (default: {DEFAULT_SITE}"
                         "; an admin role at any site is global — see "
                         "docs/SECURITY.md)")

    add_sandbox_parsers(sub)
    return p


def add_sandbox_parsers(sub: Any) -> None:
    """``sandbox-mint|list|reap|drive`` — the zero-signup live demo (0009)."""
    from .sandbox import (DEFAULT_CONSOLE_URL, DEFAULT_MAX_LIVE,
                          DEFAULT_STATE_PATH)

    def backend(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--url", default=None,
                            help="Supabase/PostgREST URL (or env SUPABASE_URL)")
        parser.add_argument("--key", default=None,
                            help="API key (or env SUPABASE_KEY). sandbox-mint "
                                 "needs only the anon key — the sandbox is "
                                 "anon by design; sandbox-reap/--remote need "
                                 "the service key (or an admin session)")
        parser.add_argument("--state-file", type=Path, default=None,
                            metavar="PATH",
                            help="per-box registry of live sandboxes "
                                 f"(default {DEFAULT_STATE_PATH}, env "
                                 "YANTRAOPS_SANDBOX_STATE); it records site "
                                 "ids and pids, never tokens")
        parser.add_argument("--json", action="store_true", dest="json_output",
                            help="machine-readable output")

    mint = sub.add_parser(
        "sandbox-mint",
        help="mint a throwaway demo sandbox: ephemeral site + token, a seeded "
             "fleet with history, and a simulator scoped to it")
    backend(mint)
    mint.add_argument("--ttl", type=int, default=None, metavar="MIN",
                      help="requested lifetime in minutes (clamped by "
                           "demo_limits.ttl_minutes; default: the server's)")
    mint.add_argument("--robots", type=int, default=6, metavar="N",
                      help="robots to seed (clamped by "
                           "demo_limits.max_seed_robots; default 6)")
    mint.add_argument("--origin", default=None,
                      help="coarse marker recorded on the session, e.g. "
                           "'marketing'")
    mint.add_argument("--console", default=None, metavar="URL",
                      help="console base for the printed link (env "
                           f"YANTRA_CONSOLE_URL, default {DEFAULT_CONSOLE_URL})")
    mint.add_argument("--max-live", type=int, default=None, metavar="N",
                      help="ceiling on concurrent sandboxes ON THIS BOX "
                           f"(default {DEFAULT_MAX_LIVE}, env "
                           "YANTRAOPS_SANDBOX_MAX); exits 3 when hit")
    mint.add_argument("--no-sim", action="store_true",
                      help="mint and seed but do not start a simulator")
    mint.add_argument("--no-history", action="store_true",
                      help="skip the telemetry/incident/mission backfill")
    mint.add_argument("--sim-interval", type=float, default=2.0, metavar="S",
                      help="simulator tick interval seconds (default 2)")

    lst = sub.add_parser(
        "sandbox-list", help="list the demo sandboxes running on this box")
    backend(lst)
    lst.add_argument("--remote", action="store_true",
                     help="also call admin_list_demo_sessions (needs admin or "
                          "the service key)")
    lst.add_argument("--max-live", type=int, default=None, metavar="N",
                     help="ceiling to report (default: env/built-in)")

    reap = sub.add_parser(
        "sandbox-reap",
        help="purge every expired sandbox (rows + simulator). Idempotent — "
             "safe to run from cron every minute")
    backend(reap)
    reap.add_argument("--grace", type=int, default=0, metavar="MIN",
                      help="extra minutes past expiry before purging "
                           "(default 0)")

    from .sandbox_http import (DEFAULT_HOST, DEFAULT_MAX_INFLIGHT,
                               DEFAULT_PORT, DEFAULT_RATE,
                               DEFAULT_RATE_WINDOW_S, DEFAULT_SWEEP_S,
                               ROUTE_MINT)

    serve = sub.add_parser(
        "sandbox-serve",
        help="HTTP door for the marketing page's 'Try it with a live fleet' "
             f"button: POST {ROUTE_MINT} mints one sandbox and answers its "
             "console URL. Rate-limited per IP; never accepts a "
             "caller-supplied site or TTL")
    backend(serve)
    serve.add_argument("--host", default=DEFAULT_HOST,
                       help=f"bind address (default {DEFAULT_HOST} — put "
                            "nginx in front; 0.0.0.0 exposes it directly)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT,
                       help=f"bind port (default {DEFAULT_PORT})")
    serve.add_argument("--console", default=None, metavar="URL",
                       help="console base for the minted link (env "
                            f"YANTRA_CONSOLE_URL, default {DEFAULT_CONSOLE_URL})")
    serve.add_argument("--ttl", type=int, default=None, metavar="MIN",
                       help="lifetime of every sandbox this door mints "
                            "(clamped by demo_limits.ttl_minutes; default: "
                            "the server's). Operator config — a visitor "
                            "cannot ask for a different one")
    serve.add_argument("--robots", type=int, default=6, metavar="N",
                       help="robots to seed per sandbox (default 6)")
    serve.add_argument("--origin", default="web",
                       help="coarse marker recorded on every session "
                            "(default 'web')")
    serve.add_argument("--max-live", type=int, default=None, metavar="N",
                       help="ceiling on concurrent sandboxes ON THIS BOX "
                            f"(default {DEFAULT_MAX_LIVE}, env "
                            "YANTRAOPS_SANDBOX_MAX); answers HTTP 429")
    serve.add_argument("--rate", type=int, default=DEFAULT_RATE, metavar="N",
                       help=f"mints allowed per IP per window (default "
                            f"{DEFAULT_RATE}; 0 disables the limiter)")
    serve.add_argument("--rate-window", type=float,
                       default=DEFAULT_RATE_WINDOW_S, metavar="S",
                       help=f"rate-limit window seconds (default "
                            f"{int(DEFAULT_RATE_WINDOW_S)})")
    serve.add_argument("--max-inflight", type=int,
                       default=DEFAULT_MAX_INFLIGHT, metavar="N",
                       help="mints allowed to run at the same time "
                            f"(default {DEFAULT_MAX_INFLIGHT})")
    serve.add_argument("--trusted-proxy-hops", type=int, default=0,
                       metavar="N",
                       help="how many reverse proxies append to "
                            "X-Forwarded-For (default 0 = ignore the header "
                            "entirely and rate-limit the socket peer). Set 1 "
                            "behind a single nginx")
    serve.add_argument("--allow-origin", default="*", metavar="ORIGIN",
                       help="Access-Control-Allow-Origin value (default *; "
                            "pin it to https://yantrika.ai in production)")
    serve.add_argument("--sweep-interval", type=float, default=DEFAULT_SWEEP_S,
                       metavar="S",
                       help="how often to stop the simulators of expired "
                            f"sandboxes (default {int(DEFAULT_SWEEP_S)}; 0 "
                            "disables). Purging database rows is "
                            "sandbox-reap's job — it needs the service key")
    serve.add_argument("--sim-interval", type=float, default=2.0, metavar="S",
                       help="simulator tick interval seconds (default 2)")
    serve.add_argument("--no-sim", action="store_true",
                       help="mint and seed but start no simulator")
    serve.add_argument("--no-history", action="store_true",
                       help="skip the telemetry/incident/mission backfill")
    serve.add_argument("--duration", type=float, default=None, metavar="S",
                       help="exit cleanly after N seconds (default: run "
                            "until SIGTERM)")
    serve.add_argument("--quiet", action="store_true",
                       help="no banner and no access log")

    drive = sub.add_parser(
        "sandbox-drive",
        help="internal: move one sandbox's fleet (spawned by sandbox-mint; "
             "reads the demo token from $YANTRAOPS_DEMO_TOKEN)")
    drive.add_argument("--url", default=None)
    drive.add_argument("--key", default=None)
    drive.add_argument("--site", required=True, metavar="DEMO-XXXX")
    drive.add_argument("--robots", type=int, default=6)
    drive.add_argument("--interval", type=float, default=2.0)
    drive.add_argument("--until", default=None, metavar="ISO8601",
                       help="stop at this timestamp (the session's expiry)")
    drive.add_argument("--ticks", type=int, default=0,
                       help="stop after N ticks (0 = until --until/SIGTERM)")


MIGRATE_CMD = ('python -m yantraops migrate --db-url '
               '"postgresql://postgres:<password>@db.<project-ref>'
               '.supabase.co:5432/postgres"')


def cmd_up(args: argparse.Namespace) -> int:
    if args.broker and not args.mqtt:
        print("yantraops: --broker requires --mqtt", file=sys.stderr)
        return 2
    if args.no_sim and not args.mqtt:
        print("yantraops: --no-sim requires --mqtt (without the sim the only "
              "robot-data source is a VDA 5050 broker)", file=sys.stderr)
        return 2
    if args.mqtt:
        from .broker import mqtt_preflight, parse_broker
        err = mqtt_preflight(embedded=args.broker is None)
        if err:
            print(f"yantraops: {err}", file=sys.stderr)
            return 2
        if args.broker:
            try:
                parse_broker(args.broker)
            except ValueError as exc:
                print(f"yantraops: {exc}", file=sys.stderr)
                return 2

    if args.supabase:
        base_url, key = resolve_supabase(args.url, args.key)
        state, detail = preflight_supabase_schema(base_url, key)
        if state == "schema-missing":
            print(f"yantraops: schema missing — run: {MIGRATE_CMD}\n"
                  f"  (probe of {base_url}/rest/v1/robots said: {detail};\n"
                  "   see supabase/README.md for where to find the db-url)",
                  file=sys.stderr)
            return 2
        if state == "auth":
            print("yantraops: Supabase rejected the key "
                  f"({detail}) — check --key / SUPABASE_KEY (use the anon key "
                  "from Settings -> API in the dashboard)", file=sys.stderr)
            return 2
        if state != "ok":
            print(f"yantraops: WARNING supabase preflight inconclusive "
                  f"({state}: {detail}); starting anyway", file=sys.stderr)

    stack = FleetStack(
        loopback=not args.supabase,
        copilot=not args.no_copilot,
        url=args.url,
        key=args.key,
        sim_interval=args.sim_interval,
        detect_interval=args.detect_interval,
        notify_interval=args.notify_interval,
        state_file=args.state_file,
        quiet=args.quiet,
        verbose=args.verbose,
        open_browser=not args.no_open and not args.quiet,
        mqtt=args.mqtt,
        mqtt_broker=args.broker,
        sim=not args.no_sim,
    )
    stack.start()
    if not args.quiet:
        print(stack.banner(), flush=True)
    stack.wait_ready()
    return stack.run(duration=args.duration)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "up":
        return cmd_up(args)
    if args.command == "migrate":
        from .migrate import run_migrate  # lazy: --print-order needs zero deps
        return run_migrate(args.db_url, args.dir, dry_run=args.dry_run,
                           force=args.force, print_order=args.print_order,
                           include_opt_in=args.include_opt_in)
    if args.command == "doctor":
        from .doctor import run_doctor
        return run_doctor()
    if args.command == "audit-security":
        from .audit import run_audit
        return run_audit(args.url, args.key, json_output=args.json)
    if args.command == "grant-role":
        from .grant_role import run_grant_role
        return run_grant_role(args.db_url, args.email, args.role, args.site)
    if args.command.startswith("sandbox-"):
        return cmd_sandbox(args)
    return run_status(args.state_file)


def cmd_sandbox(args: argparse.Namespace) -> int:
    """Dispatch the four ``sandbox-*`` subcommands (see sandbox.py)."""
    from . import sandbox as sb
    try:
        base_url, key = resolve_supabase(args.url, args.key)
    except RuntimeError as exc:
        print(f"yantraops: {exc}", file=sys.stderr)
        return sb.EXIT_ERROR
    if args.command == "sandbox-mint":
        return sb.run_sandbox_mint(
            base_url, key, ttl=args.ttl, robots=args.robots,
            origin=args.origin, console=args.console, max_live=args.max_live,
            sim=not args.no_sim, interval=args.sim_interval,
            history=not args.no_history, state_file=args.state_file,
            json_output=args.json_output)
    if args.command == "sandbox-list":
        return sb.run_sandbox_list(
            base_url, key, state_file=args.state_file,
            json_output=args.json_output, remote=args.remote,
            max_live=args.max_live)
    if args.command == "sandbox-reap":
        return sb.run_sandbox_reap(
            base_url, key, grace=args.grace, state_file=args.state_file,
            json_output=args.json_output)
    if args.command == "sandbox-serve":
        from .sandbox_http import run_sandbox_serve
        return run_sandbox_serve(
            base_url, key, host=args.host, port=args.port,
            console=args.console, ttl=args.ttl, robots=args.robots,
            origin=args.origin, max_live=args.max_live,
            sim=not args.no_sim, history=not args.no_history,
            interval=args.sim_interval, state_file=args.state_file,
            rate=args.rate, rate_window=args.rate_window,
            max_inflight=args.max_inflight,
            trusted_proxy_hops=args.trusted_proxy_hops,
            allow_origin=args.allow_origin,
            sweep_interval=args.sweep_interval, duration=args.duration,
            quiet=args.quiet)
    return sb.run_sandbox_drive(
        base_url, key, args.site, robots=args.robots,
        interval=args.interval, until=args.until, ticks=args.ticks)


if __name__ == "__main__":
    sys.exit(main())
