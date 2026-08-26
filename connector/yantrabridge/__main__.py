"""CLI entry point.

Examples
--------
Dry-run against the bundled sample (prints rows, no network)::

    python -m yantrabridge --file sample.jsonl --dry-run

Replay a JSONL file into Supabase::

    python -m yantrabridge --file sample.jsonl

Live MQTT bridge (requires paho-mqtt)::

    python -m yantrabridge --mqtt-host broker.local --mqtt-port 1883
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from yantrabridge.sink import SupabaseSink
from yantrabridge.sources import DEFAULT_STATE_TOPIC, MqttSource, read_jsonl
from yantrabridge.translate import BATTERY_ALERT_THRESHOLD, Translator


def _print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"-- {title} ({len(rows)}) --")
    for row in rows:
        print(json.dumps(row, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantrabridge",
        description="VDA 5050 v2.1 state -> Supabase connector (YantraFleet).",
    )
    src = p.add_argument_group("source")
    src.add_argument("--file", help="JSONL file of VDA 5050 state messages")
    src.add_argument("--mqtt-host", help="MQTT broker host (live mode)")
    src.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port")
    src.add_argument("--mqtt-topic", default=DEFAULT_STATE_TOPIC,
                     help=f"state topic filter (default: {DEFAULT_STATE_TOPIC})")
    src.add_argument("--mqtt-username", help="MQTT username (optional)")
    src.add_argument("--mqtt-password", help="MQTT password (optional)")

    out = p.add_argument_group("output")
    out.add_argument("--dry-run", action="store_true",
                     help="print translated rows instead of writing to Supabase")
    out.add_argument("--supabase-url", help="override SUPABASE_URL")
    out.add_argument("--supabase-key", help="override SUPABASE_KEY")
    out.add_argument("--battery-threshold", type=float,
                     default=BATTERY_ALERT_THRESHOLD,
                     help="low-battery alert threshold in %% (default: %(default)s)")
    return p


def run_file(args: argparse.Namespace) -> int:
    translator = Translator(battery_threshold=args.battery_threshold)
    robots, alerts = translator.feed_many(read_jsonl(args.file))

    if args.dry_run:
        _print_rows("robots (upsert)", robots)
        _print_rows("alerts (insert, deduped)", alerts)
        print(f"-- fleet_meta heartbeat: id=1 writer_id=yantrabridge (skipped: dry-run) --")
        return 0

    with SupabaseSink(args.supabase_url, args.supabase_key) as sink:
        counts = sink.push(robots, alerts)
    print(f"pushed: {counts['robots']} robot rows, {counts['alerts']} alerts, "
          "1 heartbeat")
    return 0


def run_mqtt(args: argparse.Namespace) -> int:
    translator = Translator(battery_threshold=args.battery_threshold)
    sink: SupabaseSink | None = None
    if not args.dry_run:
        sink = SupabaseSink(args.supabase_url, args.supabase_key)

    def on_state(msg: dict[str, Any]) -> None:
        robot, alerts = translator.feed(msg)
        if args.dry_run:
            _print_rows("robot", [robot])
            if alerts:
                _print_rows("alerts", alerts)
        else:
            assert sink is not None
            counts = sink.push([robot], alerts)
            print(f"[{robot['id']}] status={robot['status']} "
                  f"alerts+={counts['alerts']}")

    source = MqttSource(
        on_state,
        host=args.mqtt_host,
        port=args.mqtt_port,
        topic=args.mqtt_topic,
        username=args.mqtt_username,
        password=args.mqtt_password,
    )
    print(f"connecting to mqtt://{args.mqtt_host}:{args.mqtt_port} "
          f"topic '{args.mqtt_topic}' (ctrl-c to stop)")
    try:
        source.run_forever()
    except KeyboardInterrupt:
        source.stop()
    finally:
        if sink is not None:
            sink.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.file) == bool(args.mqtt_host):
        print("error: choose exactly one source: --file <path> or --mqtt-host <host>",
              file=sys.stderr)
        return 2
    return run_file(args) if args.file else run_mqtt(args)


if __name__ == "__main__":
    raise SystemExit(main())
