"""CLI: poll the robots table and maintain the incidents table.

    python -m yantradetect --interval 5        # poll loop
    python -m yantradetect --once              # single poll, then exit
    python -m yantradetect --once --dry-run    # print actions, write nothing
    python -m yantradetect --maintenance --once  # telemetry -> maintenance_findings

The reader is always PostgREST (``robots``, or ``robot_telemetry`` under
``--maintenance``); ``--dry-run`` only swaps the *writer* for a printing
sink.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import httpx
from yantracore import start_site_sync, stop_site_sync
from yantracore.runtime_config import (
    TablePoller,
    coerce_float,
    coerce_int,
    resolve_value,
)

from .engine import IncidentEngine
from .maintenance import MaintenanceEngine, MaintenanceSink
from .sink import DryRunSink, PostgRESTSink, resolve_config

log = logging.getLogger("yantradetect")

#: v0.18: non-secret runtime tunables this service reads live from
#: public.app_config (see supabase/0018_app_config.sql). Hardcoded
#: fallbacks match the argparse defaults this module used before v0.18.
CONFIG_KEYS = (
    "DETECTOR_PENDING_POLLS", "DETECTOR_CLEAR_POLLS",
    "DETECTOR_REOPEN_WINDOW", "DETECTOR_STALE_POLLS",
    "DETECTOR_WINDOW_HOURS", "DETECTOR_INTERVAL",
)
DEFAULT_INTERVAL_S = 5.0
DEFAULT_PENDING_POLLS = 2
DEFAULT_CLEAR_POLLS = 1
DEFAULT_REOPEN_WINDOW_S = 300.0
DEFAULT_STALE_POLLS = 6
DEFAULT_WINDOW_HOURS = 6.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantradetect",
        description="Detect fault/estop incidents from the robots table",
    )
    p.add_argument("--url", default=None, help="Supabase URL (env SUPABASE_URL)")
    p.add_argument("--key", default=None, help="Supabase key (env SUPABASE_KEY)")
    p.add_argument("--interval", type=float, default=None,
                   help="poll interval seconds (default: live from "
                        "public.app_config's DETECTOR_INTERVAL, else 5)")
    p.add_argument("--once", action="store_true",
                   help="run a single poll and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="print actions instead of writing incidents")
    p.add_argument("--pending-polls", type=int, default=None,
                   help="consecutive abnormal polls before opening "
                        "(default: live from app_config, else 2)")
    p.add_argument("--clear-polls", type=int, default=None,
                   help="consecutive clear polls before resolving "
                        "(default: live from app_config, else 1)")
    p.add_argument("--reopen-window", type=float, default=None,
                   help="re-open same incident if bad again within N s "
                        "(default: live from app_config, else 300)")
    p.add_argument("--stale-polls", type=int, default=None,
                   help="missing polls before auto-resolving "
                        "(default: live from app_config, else 6)")
    p.add_argument("--maintenance", action="store_true",
                   help="predictive maintenance: read robot_telemetry, "
                        "write maintenance_findings")
    p.add_argument("--window-hours", type=float, default=None,
                   help="telemetry window for --maintenance "
                        "(default: live from app_config, else 6)")
    return p


def run_maintenance(args: argparse.Namespace,
                    client: httpx.Client | None = None,
                    max_polls: int | None = None,
                    config: TablePoller | None = None) -> int:
    """--maintenance mode: telemetry window -> maintenance_findings."""
    source = MaintenanceSink(args.url, args.key, client=client)
    sink = DryRunSink() if args.dry_run else source
    engine = MaintenanceEngine()
    if not args.dry_run:
        engine.seed(source.fetch_open_findings())

    polls = 0
    try:
        while True:
            window_hours = resolve_value(args.window_hours, config,
                                         "DETECTOR_WINDOW_HOURS",
                                         DEFAULT_WINDOW_HOURS, coerce_float)
            try:
                samples = source.fetch_telemetry(window_hours)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telemetry poll failed: %s", exc)
                samples = None
            if samples is not None:
                actions = engine.observe(samples)
                if actions:
                    sink.apply(actions)
                log.info("maintenance poll: %d samples, %d actions",
                         len(samples), len(actions))
            polls += 1
            if args.once or (max_polls is not None and polls >= max_polls):
                return 0
            if config is not None:
                config.maybe_poll()
            interval = resolve_value(args.interval, config, "DETECTOR_INTERVAL",
                                     DEFAULT_INTERVAL_S, coerce_float)
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0
    finally:
        source.close()


def _apply_live_engine_tuning(engine: IncidentEngine, args: argparse.Namespace,
                              config: TablePoller) -> None:
    """Push any live app_config change straight into the running engine's
    plain instance attributes — a "simple value swap", same as every other
    live-reconfigurable tunable in this codebase; per-robot counters keep
    counting against whatever threshold is current at each observe() call.
    Explicit CLI flags (checked first) are never overridden.
    """
    pending = resolve_value(args.pending_polls, config, "DETECTOR_PENDING_POLLS",
                            DEFAULT_PENDING_POLLS, coerce_int)
    clear = resolve_value(args.clear_polls, config, "DETECTOR_CLEAR_POLLS",
                          DEFAULT_CLEAR_POLLS, coerce_int)
    reopen = resolve_value(args.reopen_window, config, "DETECTOR_REOPEN_WINDOW",
                           DEFAULT_REOPEN_WINDOW_S, coerce_float)
    stale = resolve_value(args.stale_polls, config, "DETECTOR_STALE_POLLS",
                          DEFAULT_STALE_POLLS, coerce_int)
    if pending >= 1:
        engine.pending_polls = pending
    if clear >= 1:
        engine.clear_polls = clear
    engine.reopen_window_s = reopen
    engine.stale_polls = stale


def run(argv: list[str] | None = None,
        client: httpx.Client | None = None,
        max_polls: int | None = None) -> int:
    """Entry point; ``client``/``max_polls`` are injectable for tests."""
    args = build_parser().parse_args(argv)
    import os as _os
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=getattr(logging,
        _os.environ.get("YANTRA_LOG_LEVEL", "INFO").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    # v0.18: non-secret tuning read live from public.app_config when a
    # flag wasn't explicitly passed; degrades quietly (see TablePoller)
    # when the migration isn't applied or there's no real backend at all.
    # v0.18.1: YANTRA_SITE_ID gets the same live wiring (used by engine.py/
    # maintenance.py when stamping incidents/findings) — same degrade-
    # quietly behavior, so an admin-console site change reaches
    # yantracore.site_id() without a restart.
    url, key = resolve_config(args.url, args.key)
    config = TablePoller(url, key, table="app_config", keys=CONFIG_KEYS,
                         client=client)
    config.poll_once()
    start_site_sync(url, key, client=client)

    try:
        if args.maintenance:
            return run_maintenance(args, client=client, max_polls=max_polls,
                                   config=config)

        source = PostgRESTSink(args.url, args.key, client=client)
        sink = DryRunSink() if args.dry_run else source
        engine = IncidentEngine(
            pending_polls=resolve_value(args.pending_polls, config,
                                        "DETECTOR_PENDING_POLLS",
                                        DEFAULT_PENDING_POLLS, coerce_int),
            clear_polls=resolve_value(args.clear_polls, config,
                                      "DETECTOR_CLEAR_POLLS",
                                      DEFAULT_CLEAR_POLLS, coerce_int),
            reopen_window_s=resolve_value(args.reopen_window, config,
                                          "DETECTOR_REOPEN_WINDOW",
                                          DEFAULT_REOPEN_WINDOW_S, coerce_float),
            stale_polls=resolve_value(args.stale_polls, config,
                                      "DETECTOR_STALE_POLLS",
                                      DEFAULT_STALE_POLLS, coerce_int),
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
                config.maybe_poll()
                _apply_live_engine_tuning(engine, args, config)
                interval = resolve_value(args.interval, config, "DETECTOR_INTERVAL",
                                         DEFAULT_INTERVAL_S, coerce_float)
                time.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            return 0
        finally:
            source.close()
            config.close()
    finally:
        stop_site_sync()


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(run())


if __name__ == "__main__":
    main()
