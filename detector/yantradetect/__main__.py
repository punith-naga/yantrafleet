"""CLI: poll the robots table and maintain the incidents table.

    python -m yantradetect --interval 5        # poll loop
    python -m yantradetect --once              # single poll, then exit
    python -m yantradetect --once --dry-run    # print actions, write nothing

The reader is always the PostgREST ``robots`` table; ``--dry-run`` only
swaps the *writer* for a printing sink.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import httpx

from .engine import IncidentEngine
from .sink import DryRunSink, PostgRESTSink

log = logging.getLogger("yantradetect")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantradetect",
        description="Detect fault/estop incidents from the robots table",
    )
    p.add_argument("--url", default=None, help="Supabase URL (env SUPABASE_URL)")
    p.add_argument("--key", default=None, help="Supabase key (env SUPABASE_KEY)")
    p.add_argument("--interval", type=float, default=5.0,
                   help="poll interval seconds (default 5)")
    p.add_argument("--once", action="store_true",
                   help="run a single poll and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="print actions instead of writing incidents")
    p.add_argument("--pending-polls", type=int, default=2,
                   help="consecutive abnormal polls before opening (default 2)")
    p.add_argument("--clear-polls", type=int, default=1,
                   help="consecutive clear polls before resolving (default 1)")
    p.add_argument("--reopen-window", type=float, default=300.0,
                   help="re-open same incident if bad again within N s (default 300)")
    p.add_argument("--stale-polls", type=int, default=6,
                   help="missing polls before auto-resolving (default 6)")
    return p


def run(argv: list[str] | None = None,
        client: httpx.Client | None = None,
        max_polls: int | None = None) -> int:
    """Entry point; ``client``/``max_polls`` are injectable for tests."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    source = PostgRESTSink(args.url, args.key, client=client)
    sink = DryRunSink() if args.dry_run else source
    engine = IncidentEngine(
        pending_polls=args.pending_polls,
        clear_polls=args.clear_polls,
        reopen_window_s=args.reopen_window,
        stale_polls=args.stale_polls,
    )
    if not args.dry_run:
        engine.seed(source.fetch_open_incidents())

    polls = 0
    try:
        while True:
            try:
                rows = source.fetch_robots()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("robots poll failed: %s", exc)
                rows = None
            if rows is not None:
                actions = engine.observe(rows)
                if actions:
                    sink.apply(actions)
                    log.info("poll: %d robots, %d actions", len(rows), len(actions))
            polls += 1
            if args.once or (max_polls is not None and polls >= max_polls):
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0
    finally:
        source.close()


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(run())


if __name__ == "__main__":
    main()
