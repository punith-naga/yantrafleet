"""CLI entry point.

Examples
--------
Dry-run against the bundled sample (prints rows, no network)::

    python -m yantrabridge --file sample.jsonl --dry-run

Replay a JSONL file into Supabase::

    python -m yantrabridge --file sample.jsonl

Live MQTT bridge (requires paho-mqtt)::

    python -m yantrabridge --mqtt-host broker.local --mqtt-port 1883

Live MQTT bridge that also executes operator commands (polls approved
``commands`` rows, publishes VDA 5050 instantActions, closes rows from
the actionStates in the robots' state messages)::

    python -m yantrabridge --mqtt-host broker.local --commands

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
import os
import sys
from typing import Any

import httpx

from yantrabridge.sink import DEFAULT_SUPABASE_KEY, DEFAULT_SUPABASE_URL, SupabaseSink
from yantrabridge.sources import DEFAULT_STATE_TOPIC, MqttSource, read_jsonl
from yantrabridge.translate import (
    BATTERY_ALERT_THRESHOLD,
    Translator,
    translate_connection,
)


def _print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"-- {title} ({len(rows)}) --")
    for row in rows:
        print(json.dumps(row, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantrabridge",
        description="VDA 5050 v2.1 state -> Supabase connector (Yantrika).",
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
    out.add_argument("--site", help="stamp site_id on written rows and only "
                                    "poll commands for this site")

    cmds = p.add_argument_group("commands (MQTT mode)")
    cmds.add_argument("--commands", action="store_true",
                      help="poll approved operator commands and publish them "
                           "as VDA 5050 instantActions; command rows are "
                           "closed (executed/failed) from the actionStates "
                           "in the robots' state messages")
    cmds.add_argument("--commands-interval", type=float, default=2.0,
                      help="seconds between command polls (default: %(default)s)")
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


def _stamp_site(rows: list[dict[str, Any]], site: str | None) -> None:
    """Stamp ``site_id`` on outgoing rows when --site was given."""
    if site:
        for row in rows:
            row["site_id"] = site


def run_file(args: argparse.Namespace) -> int:
    translator = Translator(battery_threshold=args.battery_threshold)
    robots, alerts = translator.feed_many(read_jsonl(args.file))
    _stamp_site(robots, args.site)
    _stamp_site(alerts, args.site)

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


#: v0.18: non-secret runtime tunables this service reads live from
#: public.app_config (see supabase/0018_app_config.sql).
CONFIG_KEYS = (
    "CONNECTOR_BATTERY_THRESHOLD", "CONNECTOR_MQTT_HOST",
    "CONNECTOR_MQTT_PORT", "CONNECTOR_MQTT_TOPIC",
    "CONNECTOR_MQTT_USERNAME", "CONNECTOR_MQTT_PASSWORD",
)
CONFIG_POLL_INTERVAL_S = 30.0


def run_mqtt(args: argparse.Namespace,
            transport: httpx.BaseTransport | None = None,
            stop_event: Any | None = None) -> int:
    """Live MQTT bridge, optionally with live app_config wiring (v0.18).

    ``transport`` is an injectable ``httpx.BaseTransport``
    (``httpx.MockTransport``) used for both the Supabase writes (sink /
    CommandPublisher, same convention as the rest of this module) and the
    app_config poll below — tests exercise the whole thing offline.

    ``stop_event`` (a ``threading.Event``) is an injectable substitute
    for "wait for Ctrl-C": tests set it (immediately, or from another
    thread after letting a poll or two run) instead of sending a real
    SIGINT. Production leaves it ``None`` and gets a fresh Event that
    nothing but KeyboardInterrupt ever sets.

    Precedence for CONNECTOR_MQTT_HOST/PORT/TOPIC/USERNAME/PASSWORD and
    CONNECTOR_BATTERY_THRESHOLD is DELIBERATELY inverted from every other
    live-config wiring in this codebase: here, a live app_config value
    (once one exists) always wins over the CLI flag/hardcoded default,
    rather than the CLI flag pinning forever. That's because --mqtt-host
    is structurally REQUIRED just to select MQTT mode at all (see main()
    below) — treating "a CLI flag was given" as a permanent override
    would make it impossible to ever move a running bridge to a
    different broker from the admin console, which is the entire point
    of this wiring. The CLI flags/hardcoded defaults still matter: they
    are the value used at startup and whenever no app_config row exists
    (or the migration isn't applied) — see resolve_value() calls below,
    all with ``explicit=None``.
    """
    import threading

    from yantracore.runtime_config import TablePoller
    from yantrabridge.mqtt_runtime import LiveMqttConfig, MqttConnectionManager

    translator = Translator(battery_threshold=args.battery_threshold)
    sink: SupabaseSink | None = None
    if not args.dry_run:
        sink = SupabaseSink(args.supabase_url, args.supabase_key, transport=transport)
    publisher = None  # set below when --commands; on_state reads the closure

    def on_state(msg: dict[str, Any]) -> None:
        if publisher is not None:
            publisher.handle_state(msg)  # learn identity + close acked cmds
        robot, alerts = translator.feed(msg)
        _stamp_site([robot], args.site)
        _stamp_site(alerts, args.site)
        if args.dry_run:
            _print_rows("robot", [robot])
            if alerts:
                _print_rows("alerts", alerts)
        else:
            assert sink is not None
            counts = sink.push([robot], alerts)
            print(f"[{robot['id']}] status={robot['status']} "
                  f"alerts+={counts['alerts']}")

    def on_connection(msg: dict[str, Any]) -> None:
        row = translate_connection(msg)
        if row is None:  # ONLINE, or an unrecognized connectionState
            return
        _stamp_site([row], args.site)
        if args.dry_run:
            _print_rows("robot (connection)", [row])
        else:
            assert sink is not None
            sink.upsert_robots([row])
            print(f"[{row['id']}] connection={msg.get('connectionState')} "
                  f"-> status={row['status']}")

    def build_source(params: dict[str, Any]) -> MqttSource:
        return MqttSource(
            on_state,
            host=params["host"],
            port=params["port"],
            topic=params["topic"],
            on_connection=on_connection,
            username=params.get("username"),
            password=params.get("password"),
        )

    initial_params = {
        "host": args.mqtt_host, "port": args.mqtt_port,
        "topic": args.mqtt_topic, "username": args.mqtt_username,
        "password": args.mqtt_password,
    }
    manager = MqttConnectionManager(build_source, initial_params)

    def _publish(topic: str, payload: str, qos: int = 0) -> None:
        # Indirection so CommandPublisher survives a live reconnect —
        # manager.source is read fresh on every publish, never bound to
        # one specific (possibly torn-down) MqttSource instance.
        manager.source.publish(topic, payload, qos=qos)

    stop_polling = threading.Event()
    poll_thread: threading.Thread | None = None
    if args.commands:
        from yantrabridge.commands import CommandPublisher

        publisher = CommandPublisher(
            _publish, args.supabase_url, args.supabase_key,
            site=args.site, transport=transport)

        def _poll_loop() -> None:
            while not stop_polling.wait(args.commands_interval):
                publisher.poll()

        poll_thread = threading.Thread(
            target=_poll_loop, name="yantrabridge-commands", daemon=True)
        poll_thread.start()
        print(f"command gate: polling approved commands every "
              f"{args.commands_interval}s -> instantActions")

    resolved_url = (args.supabase_url or os.environ.get("SUPABASE_URL")
                    or DEFAULT_SUPABASE_URL).rstrip("/")
    resolved_key = (args.supabase_key or os.environ.get("SUPABASE_KEY")
                    or DEFAULT_SUPABASE_KEY)
    config_client = httpx.Client(transport=transport) if transport is not None else None
    config = TablePoller(resolved_url, resolved_key, table="app_config",
                         keys=CONFIG_KEYS, client=config_client)
    config.poll_once()

    live_config = LiveMqttConfig(
        config, manager, translator.deduper,
        defaults={
            "mqtt_host": args.mqtt_host, "mqtt_port": args.mqtt_port,
            "mqtt_topic": args.mqtt_topic, "mqtt_username": args.mqtt_username,
            "mqtt_password": args.mqtt_password,
            "battery_threshold": args.battery_threshold,
        },
    )

    stop_config_poll = threading.Event()

    def _config_poll_loop() -> None:
        while not stop_config_poll.wait(CONFIG_POLL_INTERVAL_S):
            live_config.poll_and_apply()

    config_thread = threading.Thread(
        target=_config_poll_loop, name="yantrabridge-config", daemon=True)
    config_thread.start()

    print(f"connecting to mqtt://{args.mqtt_host}:{args.mqtt_port} "
          f"topic '{args.mqtt_topic}' (ctrl-c to stop)")
    try:
        # manager already started the broker connection (non-blocking —
        # paho runs its own network thread via loop_start()); the main
        # thread just waits for Ctrl-C (or stop_event, in tests),
        # mirroring run_forever()'s old blocking behaviour from the
        # outside.
        (stop_event or threading.Event()).wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_config_poll.set()
        config_thread.join(timeout=5)
        config.close()
        stop_polling.set()
        if poll_thread is not None:
            poll_thread.join(timeout=5)
        if publisher is not None:
            publisher.close()
        manager.stop()
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
    if args.commands and (not args.mqtt_host or args.dry_run):
        print("error: --commands needs live MQTT mode "
              "(--mqtt-host, without --dry-run)", file=sys.stderr)
        return 2
    return run_file(args) if args.file else run_mqtt(args)


if __name__ == "__main__":
    raise SystemExit(main())
