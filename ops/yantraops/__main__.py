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
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
        description="One-command orchestrator for the YantraFleet demo stack")
    p.add_argument("--version", action="version",
                   version=f"YantraFleet {YF_VERSION}")
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
    up.add_argument("--sim-interval", type=float, default=2.0,
                    help="yantrasim tick interval seconds (default 2)")
    up.add_argument("--detect-interval", type=float, default=5.0,
                    help="yantradetect poll interval seconds (default 5)")
    up.add_argument("--notify-interval", type=float, default=10.0,
                    help="yantranotify poll interval seconds (default 10)")
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
    return p


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
    return run_status(args.state_file)


if __name__ == "__main__":
    sys.exit(main())
