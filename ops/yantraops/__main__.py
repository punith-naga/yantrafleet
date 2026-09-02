"""CLI: ``python -m yantraops up|status``.

    python -m yantraops up                       # zero-cloud loopback demo
    python -m yantraops up --supabase            # against real Supabase
    python -m yantraops up --no-copilot          # skip the sarathi API
    python -m yantraops up --duration 30         # exit cleanly after 30 s
    python -m yantraops status                   # ping the running stack
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .orchestrator import DEFAULT_STATE_FILE, FleetStack
from .status import run_status


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantraops",
        description="One-command orchestrator for the YantraFleet demo stack")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="start the whole stack")
    mode = up.add_mutually_exclusive_group()
    mode.add_argument("--loopback", action="store_true",
                      help="in-process fake PostgREST backend (default)")
    mode.add_argument("--supabase", action="store_true",
                      help="use real Supabase (flags/env/package defaults)")
    up.add_argument("--no-copilot", action="store_true",
                    help="skip the sarathi copilot API")
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
    return p


def cmd_up(args: argparse.Namespace) -> int:
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
    return run_status(args.state_file)


if __name__ == "__main__":
    sys.exit(main())
