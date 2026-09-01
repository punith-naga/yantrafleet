"""yantranotify — omnichannel notifier for YantraFleet.

Polls the fleet's PostgREST tables for *unacked* ``crit``/``serious``
alerts and newly ``Open`` incidents, then dispatches human-readable
messages through pluggable channels (console log lines, Slack-compatible
webhook, WhatsApp via the Twilio REST API).

Alarm-fatigue discipline: every alert/incident is announced at most once
(in-memory dedup, optionally persisted to a state file), and when more
than :data:`~yantranotify.notifier.DIGEST_THRESHOLD` new events land in
one poll they are collapsed into a single digest message.
"""
from .channels import Channel, ConsoleChannel, WebhookChannel, WhatsAppChannel
from .notifier import DIGEST_THRESHOLD, Event, Notifier
from .source import AlertSource

__all__ = [
    "AlertSource",
    "Channel",
    "ConsoleChannel",
    "DIGEST_THRESHOLD",
    "Event",
    "Notifier",
    "WebhookChannel",
    "WhatsAppChannel",
]

__version__ = "0.1.0"
