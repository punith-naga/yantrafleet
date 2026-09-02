"""CLI: poll alerts/incidents and notify through the configured channels.

    python -m yantranotify --interval 10             # poll loop
    python -m yantranotify --once                    # single poll, then exit
    python -m yantranotify --interval 10 --dry-run   # print instead of send

Channels are auto-configured from the environment:

* console  — always on
* webhook  — active when ``WEBHOOK_URL`` is set (else prints the payload)
* whatsapp — active when ``TWILIO_SID``/``TWILIO_TOKEN``/``TWILIO_FROM``/
  ``TWILIO_TO`` are all set (else prints the payload; creds are never
  required)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import httpx

from .channels import ConsoleChannel, WebhookChannel, WhatsAppChannel
from .notifier import Notifier
from .source import AlertSource

log = logging.getLogger("yantranotify")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantranotify",
        description="Notify on unacked crit/serious alerts and open incidents",
    )
    p.add_argument("--url", default=None, help="Supabase URL (env SUPABASE_URL)")
    p.add_argument("--key", default=None, help="Supabase key (env SUPABASE_KEY)")
    p.add_argument("--interval", type=float, default=10.0,
                   help="poll interval seconds (default 10)")
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
    return p


def build_channels(args: argparse.Namespace,
                   client: httpx.Client | None = None) -> list:
    return [
        ConsoleChannel(),
        WebhookChannel(client=client, dry_run=args.dry_run),
        WhatsAppChannel(client=client, dry_run=args.dry_run),
    ]


def run(args: argparse.Namespace,
        client: httpx.Client | None = None,
        max_polls: int | None = None) -> int:
    """Poll loop; ``client``/``max_polls`` are injection points for tests."""
    source = AlertSource(args.url, args.key, client=client,
                         all_sites=getattr(args, "all_sites", False))
    notifier = Notifier(build_channels(args, client=client),
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
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if client is None:
            source.close()


def main(argv: list[str] | None = None) -> int:
    import os as _os
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=getattr(logging,
        _os.environ.get("YANTRA_LOG_LEVEL", "INFO").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
