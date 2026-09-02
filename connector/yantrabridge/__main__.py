"""CLI entry point.

Examples
--------
Dry-run against the bundled sample (prints rows, no network)::

    python -m yantrabridge --file sample.jsonl --dry-run

Replay a JSONL file into Supabase::

    python -m yantrabridge --file sample.jsonl

Live MQTT bridge (requires paho-mqtt)::

    python -m yantrabridge --mqtt-host broker.local --mqtt-port 1883

Import a recording (bring-your-own-recording; requires the ``import`` extra
for MCAP files)::

    python -m yantrabridge import --mcap run.mcap --dry-run
    python -m yantrabridge import --mcap run.mcap --topic-map map.json
    python -m yantrabridge import --mcap run.mcap --rate 4     # 4x replay
    python -m yantrabridge import --jsonl sample.jsonl
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


def build_import_parser() -> argparse.ArgumentParser:
    from yantrabridge.importer import TOPIC_MAP_DOC

    p = argparse.ArgumentParser(
        prog="yantrabridge import",
        description="Import a recording (MCAP or JSONL) into Supabase: "
                    "robots final state, robot_telemetry history with the "
                    "original timestamps, and alerts from VDA errors[].",
        epilog=TOPIC_MAP_DOC,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_argument_group("recording")
    src.add_argument("--mcap", help="MCAP recording (raw JSON channels; "
                                    "needs the 'import' extra: pip install "
                                    "'yantrabridge[import]')")
    src.add_argument("--jsonl", help="JSONL file of VDA 5050 state messages")
    src.add_argument("--topic-map",
                     help="JSON spec mapping non-VDA topics/fields to "
                          "{robot_id, battery, pos.x, pos.y, speed, status} "
                          "(MCAP only; see below)")

    mode = p.add_argument_group("mode")
    mode.add_argument("--rate", type=float, default=0.0,
                      help="0 = bulk import as fast as possible (default); "
                           "N = replay at Nx real time, updating robots live")
    mode.add_argument("--dry-run", action="store_true",
                      help="print a summary (topics, counts, time range, "
                           "rows that would be written); no network")

    out = p.add_argument_group("output")
    out.add_argument("--supabase-url", help="override SUPABASE_URL")
    out.add_argument("--supabase-key", help="override SUPABASE_KEY")
    out.add_argument("--battery-threshold", type=float,
                     default=BATTERY_ALERT_THRESHOLD,
                     help="low-battery alert threshold in %% "
                          "(default: %(default)s)")
    return p


def run_import(argv: list[str]) -> int:
    from yantrabridge import importer

    args = build_import_parser().parse_args(argv)
    if bool(args.mcap) == bool(args.jsonl):
        print("error: choose exactly one recording: --mcap <file> or "
              "--jsonl <file>", file=sys.stderr)
        return 2
    if args.rate < 0:
        print("error: --rate must be >= 0", file=sys.stderr)
        return 2

    topic_map = None
    if args.topic_map:
        if args.jsonl:
            print("warning: --topic-map only applies to --mcap (JSONL has "
                  "no topics); ignoring", file=sys.stderr)
        else:
            topic_map = importer.TopicMap.load(args.topic_map)

    def records() -> Any:
        if args.mcap:
            return importer.read_mcap(args.mcap)
        return importer.jsonl_records(read_jsonl(args.jsonl))

    try:
        # ----- replay mode: Nx real time, robots update live ---------------
        if args.rate > 0 and not args.dry_run:
            with SupabaseSink(args.supabase_url, args.supabase_key) as sink:
                def on_event(ev: "importer.ImportEvent") -> None:
                    if ev.robot_row is not None:
                        sink.upsert_robots([ev.robot_row])
                    if ev.telemetry_row is not None:
                        sink.insert_telemetry([ev.telemetry_row])
                    if ev.alert_rows:
                        sink.insert_alerts(ev.alert_rows)
                    if ev.robot_row is not None:
                        print(f"[{ev.robot_row['id']}] "
                              f"ts={ev.telemetry_row['ts']} "
                              f"alerts+={len(ev.alert_rows)}")

                summary = importer.replay(
                    records(), rate=args.rate, on_event=on_event,
                    topic_map=topic_map,
                    battery_threshold=args.battery_threshold)
                sink.heartbeat()
            print(importer.format_summary(summary, dry_run=False))
            return 0

        # ----- bulk / dry-run ----------------------------------------------
        robots, telemetry, alerts, summary = importer.collect(
            records(), topic_map=topic_map,
            battery_threshold=args.battery_threshold)

        if args.dry_run:
            print(importer.format_summary(summary, dry_run=True))
            _print_rows("robots (upsert)", robots)
            _print_rows("alerts (insert, deduped)", alerts)
            print(f"-- robot_telemetry: {len(telemetry)} rows "
                  "(omitted from dry-run output) --")
            return 0

        with SupabaseSink(args.supabase_url, args.supabase_key) as sink:
            sink.upsert_robots(robots)
            sink.insert_telemetry(telemetry)
            sink.insert_alerts(alerts)
            sink.heartbeat()
        print(importer.format_summary(summary, dry_run=False))
        return 0
    except (RuntimeError, importer.TopicMapError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


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
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "import":
        return run_import(argv[1:])
    args = build_parser().parse_args(argv)
    if bool(args.file) == bool(args.mqtt_host):
        print("error: choose exactly one source: --file <path> or --mqtt-host <host>",
              file=sys.stderr)
        return 2
    return run_file(args) if args.file else run_mqtt(args)


if __name__ == "__main__":
    raise SystemExit(main())
