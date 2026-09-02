"""Payload templates: pure functions turning events into webhook payloads.

Three wire formats:

* ``json``    — the original generic contract, ``{"text": <message>}``
  (default; Slack, Discord's ``/slack`` endpoint and Teams adapters all
  accept it).
* ``slack``   — Slack Block Kit: header with severity emoji, fields for
  robot/site/time, a context line; incidents get a distinct layout with
  an Impact field. No interactive elements (action-less).
* ``discord`` — Discord embeds, one embed per message, colour-coded by
  severity.

Everything here is a pure function of its inputs — no I/O, no clock —
so payload shapes are trivially unit-testable:

    payload_for(event, "slack")      -> dict
    digest_payload_for(events, fmt)  -> dict
"""
from __future__ import annotations

from typing import Sequence

from .source import Event

FORMATS = ("json", "slack", "discord")
DEFAULT_FORMAT = "json"

SEVERITY_EMOJI = {
    "crit": ":red_circle:",
    "serious": ":large_orange_circle:",
    "warn": ":large_yellow_circle:",
}
_DEFAULT_EMOJI = ":white_circle:"

SEVERITY_COLOR = {         # Discord embed colours (decimal RGB)
    "crit": 0xE01E5A,      # red
    "serious": 0xF2A100,   # orange
    "warn": 0xF8E71C,      # yellow
}
_DEFAULT_COLOR = 0x9AA0A6  # grey

_SEV_ORDER = ("crit", "serious", "warn")

DIGEST_TOP_N = 3  # items listed per severity group in a digest


def _emoji(sev: str) -> str:
    return SEVERITY_EMOJI.get(sev, _DEFAULT_EMOJI)


def _color(sev: str) -> int:
    return SEVERITY_COLOR.get(sev, _DEFAULT_COLOR)


def _check_format(fmt: str) -> str:
    fmt = (fmt or DEFAULT_FORMAT).lower()
    if fmt not in FORMATS:
        raise ValueError(f"unknown payload format {fmt!r} "
                         f"(expected one of {', '.join(FORMATS)})")
    return fmt


def group_by_severity(events: Sequence[Event]) -> list[tuple[str, list[Event]]]:
    """Stable severity grouping, worst first (crit, serious, …)."""
    buckets: dict[str, list[Event]] = {}
    for e in events:
        buckets.setdefault(e.sev, []).append(e)
    order = [s for s in _SEV_ORDER if s in buckets]
    order += sorted(s for s in buckets if s not in _SEV_ORDER)
    return [(sev, buckets[sev]) for sev in order]


# --------------------------------------------------------------------------
# Single-event payloads
# --------------------------------------------------------------------------

def payload_for(event: Event, fmt: str = DEFAULT_FORMAT) -> dict:
    """Webhook body for one alert/incident in the given format."""
    fmt = _check_format(fmt)
    if fmt == "slack":
        return _slack_event(event)
    if fmt == "discord":
        return _discord_event(event)
    return {"text": event.render()}


def _slack_fields(event: Event) -> list[dict]:
    fields = []
    if event.kind == "incident" and event.impact:
        fields.append({"type": "mrkdwn", "text": f"*Impact*\n{event.impact}"})
    if event.src:
        fields.append({"type": "mrkdwn", "text": f"*Robot*\n{event.src}"})
    if event.site:
        fields.append({"type": "mrkdwn", "text": f"*Site*\n{event.site}"})
    if event.tlabel:
        fields.append({"type": "mrkdwn", "text": f"*Time*\n{event.tlabel}"})
    return fields


def _slack_event(event: Event) -> dict:
    noun = "Incident" if event.kind == "incident" else "Alert"
    header = f"{_emoji(event.sev)} {event.sev.upper()} {noun} — {event.id}"
    body = event.msg or event.text
    if event.kind == "incident":
        context = f"YantraFleet · incident `{event.id}` · state Open"
    else:
        context = f"YantraFleet · alert `{event.id}` · unacknowledged"
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{body}*"}},
    ]
    fields = _slack_fields(event)
    if fields:
        blocks.append({"type": "section", "fields": fields})
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": context}]})
    return {"text": event.render(), "blocks": blocks}


def _discord_event(event: Event) -> dict:
    noun = "Incident" if event.kind == "incident" else "Alert"
    fields = []
    if event.kind == "incident" and event.impact:
        fields.append({"name": "Impact", "value": event.impact, "inline": True})
    if event.src:
        fields.append({"name": "Robot", "value": event.src, "inline": True})
    if event.site:
        fields.append({"name": "Site", "value": event.site, "inline": True})
    if event.tlabel:
        fields.append({"name": "Time", "value": event.tlabel, "inline": True})
    embed = {
        "title": f"{event.sev.upper()} {noun} — {event.id}",
        "description": event.msg or event.text,
        "color": _color(event.sev),
        "fields": fields,
        "footer": {"text": "YantraFleet"},
    }
    return {"embeds": [embed]}


# --------------------------------------------------------------------------
# Digest payloads (storm collapsed to one message)
# --------------------------------------------------------------------------

def _digest_head(events: Sequence[Event]) -> str:
    alerts = sum(1 for e in events if e.kind == "alert")
    incidents = sum(1 for e in events if e.kind == "incident")
    return (f"YantraFleet digest: {len(events)} new notifications "
            f"({alerts} alerts, {incidents} incidents)")


def render_digest_grouped(events: Sequence[Event]) -> str:
    """Plain-text digest grouped by severity, top-N items per group."""
    lines = [_digest_head(events)]
    for sev, group in group_by_severity(events):
        lines.append(f"{sev.upper()} ({len(group)}):")
        for e in group[:DIGEST_TOP_N]:
            lines.append(f"- {e.kind} {e.id}: {e.text}")
        more = len(group) - DIGEST_TOP_N
        if more > 0:
            lines.append(f"  … and {more} more {sev}")
    return "\n".join(lines)


def digest_payload_for(events: Sequence[Event],
                       fmt: str = DEFAULT_FORMAT) -> dict:
    """Webhook body for a digest of many new events, per format."""
    fmt = _check_format(fmt)
    if fmt == "slack":
        return _slack_digest(events)
    if fmt == "discord":
        return _discord_digest(events)
    return {"text": render_digest_grouped(events)}


def _slack_digest(events: Sequence[Event]) -> dict:
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f":inbox_tray: YantraFleet digest — "
                          f"{len(events)} new",
                  "emoji": True}},
    ]
    for sev, group in group_by_severity(events):
        lines = [f"{_emoji(sev)} *{sev.upper()} — {len(group)}*"]
        for e in group[:DIGEST_TOP_N]:
            lines.append(f"• {e.kind} `{e.id}` — {e.text}")
        more = len(group) - DIGEST_TOP_N
        if more > 0:
            lines.append(f"_… and {more} more {sev}_")
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn",
                                 "text": _digest_head(events)}]})
    return {"text": _digest_head(events), "blocks": blocks}


def _discord_digest(events: Sequence[Event]) -> dict:
    groups = group_by_severity(events)
    worst = groups[0][0] if groups else "crit"
    fields = []
    for sev, group in groups:
        lines = [f"{e.kind} {e.id} — {e.text}" for e in group[:DIGEST_TOP_N]]
        more = len(group) - DIGEST_TOP_N
        if more > 0:
            lines.append(f"… and {more} more {sev}")
        fields.append({"name": f"{sev.upper()} ({len(group)})",
                       "value": "\n".join(lines), "inline": False})
    embed = {
        "title": _digest_head(events),
        "color": _color(worst),
        "fields": fields,
        "footer": {"text": "YantraFleet"},
    }
    return {"embeds": [embed]}


# --------------------------------------------------------------------------
# Samples for `python -m yantranotify test`
# --------------------------------------------------------------------------

def sample_alert() -> Event:
    return Event("alert", "A-SAMPLE", "crit",
                 "battery critical (7%) · src=AMR-03 · at 14:07",
                 msg="battery critical (7%)", src="AMR-03",
                 site="BLR-DC1", tlabel="14:07")


def sample_incident() -> Event:
    return Event("incident", "INC-SAMPLE", "serious",
                 "AMR-05 fault — overtemp · robot out of service",
                 msg="AMR-05 fault — overtemp", src="AMR-05",
                 site="BLR-DC1", tlabel="14:10",
                 impact="robot out of service")
