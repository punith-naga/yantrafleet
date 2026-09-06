"""CLI: poll alerts/incidents and notify through the configured channels.

    python -m yantranotify --interval 10             # poll loop
    python -m yantranotify --once                    # single poll, then exit
    python -m yantranotify --interval 10 --dry-run   # print instead of send
    python -m yantranotify test --format slack       # verify your webhook

Channels are auto-configured from the environment:

* console  — always on
* webhook  — active when ``WEBHOOK_URL`` is set (else prints the payload);
  payload format via ``--format`` / ``WEBHOOK_FORMAT`` (json|slack|discord)
* whatsapp — active when ``TWILIO_SID``/``TWILIO_TOKEN``/``TWILIO_FROM``/
  ``TWILIO_TO`` are all set (else prints the payload; creds are never
  required)

Outbound channels are wrapped for reliability: 3 delivery attempts with
1s/4s backoff, failed items kept queued for the next poll, and a
per-channel circuit breaker (5 consecutive failures -> 5 min pause).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import httpx
from yantracore import start_site_sync, stop_site_sync
from yantracore.runtime_config import TablePoller, coerce_float, resolve_value

from .channels import ConsoleChannel, WebhookChannel, WhatsAppChannel
from .formats import FORMATS, payload_for, sample_alert, sample_incident
from .notifier import Notifier
from .reliability import ReliableChannel
from .settings_sync import SettingsSync
from .source import AlertSource, resolve_config

log = logging.getLogger("yantranotify")

#: v0.18: non-secret runtime tunables this service reads live from
#: public.app_config (see supabase/0018_app_config.sql).
CONFIG_KEYS = ("NOTIFIER_INTERVAL",)
DEFAULT_INTERVAL_S = 10.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantranotify",
        description="Notify on unacked crit/serious alerts and open incidents",
    )
    p.add_argument("--url", default=None, help="Supabase URL (env SUPABASE_URL)")
    p.add_argument("--key", default=None, help="Supabase key (env SUPABASE_KEY)")
    p.add_argument("--interval", type=float, default=None,
                   help="poll interval seconds (default: live from "
                        "public.app_config's NOTIFIER_INTERVAL, else 10)")
    p.add_argument("--once", action="store_true",
                   help="run a single poll and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="print every outbound message instead of sending")
    p.add_argument("--state-file", default=None,
                   help="JSON file remembering already-notified ids "
                        "(default: in-memory only)")
    p.add_argument("--all-sites", action="store_true",
                   help="notify for every site (default: only rows whose "
                        "site_id matches this process's site, env "
                        "YANTRA_SITE_ID / 'BLR-DC1')")
    p.add_argument("--format", dest="format", default=None, choices=FORMATS,
                   help="webhook payload format (env WEBHOOK_FORMAT; "
                        "default json)")
    return p


def build_channels(args: argparse.Namespace,
                   client: httpx.Client | None = None,
                   clock=None,
                   settings: SettingsSync | None = None) -> list:
    fmt = getattr(args, "format", None)
    return [
        ConsoleChannel(),
        ReliableChannel(
            WebhookChannel(client=client, dry_run=args.dry_run, fmt=fmt,
                          settings=settings),
            clock=clock),
        ReliableChannel(
            WhatsAppChannel(client=client, dry_run=args.dry_run,
                            settings=settings),
            clock=clock),
    ]


def run(args: argparse.Namespace,
        client: httpx.Client | None = None,
        max_polls: int | None = None,
        clock=None) -> int:
    """Poll loop; ``client``/``max_polls``/``clock`` are test injection
    points."""
    url, key = resolve_config(args.url, args.key)
    settings = SettingsSync(url, key, client=client)
    settings.poll_once()  # populate before the first send, mirrors sarathi's start()
    # v0.18: NOTIFIER_INTERVAL from public.app_config when --interval was
    # not explicitly passed; live-refreshed every config poll thereafter.
    # v0.18.1: YANTRA_SITE_ID gets the same live wiring (used by source.py
    # when scoping which site's alerts/incidents to notify for) — same
    # degrade-quietly behavior, so an admin-console site change reaches
    # yantracore.site_id() without a restart.
    config = TablePoller(url, key, table="app_config", keys=CONFIG_KEYS,
                         client=client)
    config.poll_once()
    start_site_sync(url, key, client=client)
    source = AlertSource(args.url, args.key, client=client,
                         all_sites=getattr(args, "all_sites", False))
    notifier = Notifier(
        build_channels(args, client=client, clock=clock, settings=settings),
        state_path=args.state_file)

    polls = 0
    try:
        while True:
            try:
                events = source.fetch_events()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("poll failed: %s", exc)
                events = None
            if events is not None:
                new = notifier.dispatch(events)
                log.info("poll: %d notifiable, %d new", len(events), len(new))
            polls += 1
            if args.once or (max_polls is not None and polls >= max_polls):
                return 0
            settings.maybe_poll()
            config.maybe_poll()
            interval = resolve_value(args.interval, config, "NOTIFIER_INTERVAL",
                                     DEFAULT_INTERVAL_S, coerce_float)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if client is None:
            source.close()
            settings.close()
            config.close()
        stop_site_sync()


# -- `test` subcommand: verify a webhook in ten seconds ---------------------

def build_test_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantranotify test",
        description="Send one sample alert + one sample incident through "
                    "the real delivery path (or just print the payloads).")
    p.add_argument("--channel", default="webhook", choices=["webhook"],
                   help="channel to exercise (default: webhook)")
    p.add_argument("--url", default=None,
                   help="webhook URL to POST to (env WEBHOOK_URL; omit to "
                        "only print the payloads)")
    p.add_argument("--format", dest="format", default=None, choices=FORMATS,
                   help="payload format (env WEBHOOK_FORMAT; default json)")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="per-request timeout seconds (default 5)")
    return p


def run_test_command(argv: list[str],
                     client: httpx.Client | None = None,
                     clock=None) -> int:
    """``python -m yantranotify test [--url ... --format slack]``.

    Renders one sample alert and one sample incident, prints both
    payloads, and — when a URL is given (arg or ``WEBHOOK_URL``) — sends
    them through the real delivery path (retry/backoff included).
    """
    args = build_test_parser().parse_args(argv)
    fmt = (args.format or os.environ.get("WEBHOOK_FORMAT") or "json").lower()
    url = args.url or os.environ.get("WEBHOOK_URL") or None
    samples = [("alert", sample_alert()), ("incident", sample_incident())]

    for kind, ev in samples:
        print(f"--- sample {kind} ({fmt}) " + "-" * (24 - len(kind)))
        print(json.dumps(payload_for(ev, fmt), indent=2, ensure_ascii=False))

    if not url:
        print("\nno webhook URL given (flag --url / env WEBHOOK_URL) — "
              "payloads printed, nothing sent")
        return 0

    channel = ReliableChannel(
        WebhookChannel(url=url, fmt=fmt, client=client,
                       timeout_s=args.timeout),
        clock=clock)
    ok = True
    for kind, ev in samples:
        sent = channel.send_event(ev)
        print(f"send sample {kind} -> {'ok' if sent else 'FAILED'}")
        ok = ok and sent
    if ok:
        print(f"webhook delivery OK: 2 messages posted to {url}")
        return 0
    print("webhook delivery FAILED — see log above")
    return 1


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=getattr(logging,
        os.environ.get("YANTRA_LOG_LEVEL", "INFO").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["test"]:
        return run_test_command(argv[1:])
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
