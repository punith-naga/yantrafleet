"""CLI entry point: ``python -m yantrasim [--supabase|--mqtt] --interval 2``."""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .commands import apply_command
from .sim import FleetSim
from .transports.base import StdoutTransport, Transport
from .transports.supabase import SupabaseTransport


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantrasim",
        description="Simulate a 10-AMR warehouse fleet emitting VDA 5050 v2.1 state.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--supabase", action="store_true",
                      help="publish to Supabase via PostgREST bulk upsert (default)")
    mode.add_argument("--mqtt", action="store_true",
                      help="publish VDA 5050 topics to an MQTT broker (needs paho-mqtt)")
    mode.add_argument("--stdout", action="store_true",
                      help="print tick summaries instead of publishing")
    p.add_argument("--interval", type=float, default=2.0,
                   help="wall-clock seconds between ticks (default: 2)")
    p.add_argument("--time-scale", type=float, default=5.0,
                   help="simulated seconds per wall second (default: 5)")
    p.add_argument("--ticks", type=int, default=0,
                   help="stop after N ticks (0 = run until Ctrl-C)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42)")
    p.add_argument("--url", default=None, help="Supabase URL (or env SUPABASE_URL)")
    p.add_argument("--key", default=None, help="Supabase anon key (or env SUPABASE_KEY)")
    p.add_argument("--broker", default="localhost", help="MQTT broker host")
    p.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    p.add_argument("--verbose", action="store_true", help="debug logging / full JSON")
    return p


def make_transport(args: argparse.Namespace, sim: FleetSim) -> Transport:
    if args.mqtt:
        from .transports.mqtt import MqttTransport  # optional paho import lives here
        return MqttTransport(sim.robots, broker=args.broker, port=args.port)
    if args.stdout:
        return StdoutTransport(verbose=args.verbose)
    return SupabaseTransport(url=args.url, key=args.key, writer_id=sim.writer_id)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import os as _os
    _lvl = (logging.DEBUG if args.verbose else
            getattr(logging, _os.environ.get("YANTRA_LOG_LEVEL", "INFO").upper(),
                    logging.INFO))
    logging.basicConfig(
        level=_lvl,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:  # httpx request spam is DEBUG-grade for a demo
        logging.getLogger("httpx").setLevel(logging.WARNING)
    sim = FleetSim(seed=args.seed)
    transport = make_transport(args, sim)
    # Simulated seconds per tick; a zero --interval (fast-forward runs)
    # still advances sim time by one time-scale unit per tick.
    dt = args.interval * args.time_scale if args.interval > 0 else args.time_scale
    mode = "mqtt" if args.mqtt else ("stdout" if args.stdout else "supabase")
    print(f"yantrasim: {len(sim.robots)} AMRs, mode={mode}, "
          f"interval={args.interval}s, dt={dt}s/tick (Ctrl-C to stop)")
    try:
        while args.ticks <= 0 or sim.tick_count < args.ticks:
            out = sim.tick(dt_s=dt)
            transport.publish(out)
            # v0.2: execute human-approved operator commands (Supabase only).
            if hasattr(transport, "poll_commands"):
                transport.poll_commands(
                    lambda rid, cmd: apply_command(sim, rid, cmd))
            for e in out.events:
                logging.getLogger("yantrasim").info(
                    "event %s (%s): %s", e.kind, e.sev, e.msg)
            if args.ticks <= 0 or sim.tick_count < args.ticks:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
