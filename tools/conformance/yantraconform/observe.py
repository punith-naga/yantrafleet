"""Message collection: turn a stream of MQTT messages into per-robot evidence.

The engine never re-reads the wire. Everything the checks in
:mod:`yantraconform.checks` reason about lives in an :class:`Observation`,
which makes the check functions pure and unit-testable against synthetic
evidence with no broker anywhere in sight.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import spec
from .session import Message


@dataclass
class Received:
    """One message, parsed (or not) with its topic already split."""

    message: Message
    topic: spec.TopicParts | None
    payload: dict[str, Any] | None
    parse_error: str | None = None

    @property
    def subtopic(self) -> str:
        return self.topic.subtopic if self.topic else ""

    @property
    def retain(self) -> bool:
        return self.message.retain


@dataclass
class ProbeEvent:
    """A record of something the tester actively published.

    ``state_index`` is the number of state messages already observed for this
    robot at the moment the probe went out, so a check can say "everything
    from state #14 onward is the response window" without touching a clock.
    That keeps active-probe checks deterministic under any timing.
    """

    name: str
    state_index: int
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """Everything seen from (and sent to) one vehicle."""

    manufacturer: str
    serial: str
    interface: str = spec.DEFAULT_INTERFACE
    major: str = spec.DEFAULT_MAJOR_SEGMENT

    states: list[Received] = field(default_factory=list)
    connections: list[Received] = field(default_factory=list)
    factsheets: list[Received] = field(default_factory=list)
    visualizations: list[Received] = field(default_factory=list)
    other: list[Received] = field(default_factory=list)
    malformed: list[Received] = field(default_factory=list)

    probes: list[ProbeEvent] = field(default_factory=list)
    #: sub-topic -> was a retain=1 copy delivered when the tester re-subscribed
    #: at the end of the run. ``None``/absent means the retain probe never ran.
    retain_probe: dict[str, bool] = field(default_factory=dict)
    retain_probed: bool = False
    #: True when the transport can tell us about other clients' last-wills
    #: (only the in-process loopback broker can).
    wills_observable: bool = False
    wills: list[tuple[str, str]] = field(default_factory=list)
    #: filled by the runner when active probing was skipped
    active_probing: bool = True

    # -- ingestion ---------------------------------------------------------

    def add(self, rec: Received) -> None:
        if rec.payload is None:
            self.malformed.append(rec)
            return
        bucket = {
            "state": self.states,
            "connection": self.connections,
            "factsheet": self.factsheets,
            "visualization": self.visualizations,
        }.get(rec.subtopic)
        (bucket if bucket is not None else self.other).append(rec)

    def note_probe(self, name: str, **data: Any) -> ProbeEvent:
        ev = ProbeEvent(name=name, state_index=len(self.states), data=data)
        self.probes.append(ev)
        return ev

    def probe(self, name: str) -> ProbeEvent | None:
        for ev in reversed(self.probes):
            if ev.name == name:
                return ev
        return None

    def states_after(self, ev: ProbeEvent | None) -> list[Received]:
        if ev is None:
            return []
        return self.states[ev.state_index:]

    # -- convenience -------------------------------------------------------

    @property
    def topic_prefix(self) -> str:
        return "/".join((self.interface, self.major, self.manufacturer, self.serial))

    @property
    def all_received(self) -> list[Received]:
        return (self.states + self.connections + self.factsheets
                + self.visualizations + self.other + self.malformed)

    def topics_seen(self) -> list[str]:
        return sorted({r.message.topic for r in self.all_received})

    def message_counts(self) -> dict[str, int]:
        return {
            "state": len(self.states),
            "connection": len(self.connections),
            "factsheet": len(self.factsheets),
            "visualization": len(self.visualizations),
            "other": len(self.other),
            "malformed": len(self.malformed),
        }

    def version_reported(self) -> str | None:
        for rec in self.states + self.connections + self.factsheets:
            if rec.payload and isinstance(rec.payload.get("version"), str):
                return rec.payload["version"]
        return None

    def action_states(self) -> dict[str, list[tuple[int, dict[str, Any]]]]:
        """actionId -> [(state index, actionState object), ...] in order."""
        out: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for i, rec in enumerate(self.states):
            for a in (rec.payload or {}).get("actionStates") or []:
                if isinstance(a, dict) and isinstance(a.get("actionId"), str):
                    out.setdefault(a["actionId"], []).append((i, a))
        return out


def parse_message(msg: Message) -> Received:
    """Split the topic and parse the payload. Never raises."""
    topic = spec.parse_topic(msg.topic)
    text = msg.text()
    try:
        payload = json.loads(text) if text.strip() else None
    except ValueError as exc:
        return Received(message=msg, topic=topic, payload=None,
                        parse_error=f"invalid JSON: {exc}")
    if payload is None:
        return Received(message=msg, topic=topic, payload=None,
                        parse_error="empty payload")
    if not isinstance(payload, dict):
        return Received(message=msg, topic=topic, payload=None,
                        parse_error=f"top-level JSON is {type(payload).__name__}, "
                                    "expected an object")
    return Received(message=msg, topic=topic, payload=payload)


class Collector:
    """Routes a stream of messages into one :class:`Observation` per vehicle.

    Identity comes from the TOPIC, never the payload -- that is the spec's own
    rule (5.2), and it is also what lets the tester detect a payload whose
    manufacturer/serialNumber disagrees with the topic it arrived on.
    """

    def __init__(self, interface: str = spec.DEFAULT_INTERFACE,
                 major: str = spec.DEFAULT_MAJOR_SEGMENT) -> None:
        self.interface = interface
        self.major = major
        self.robots: dict[tuple[str, str], Observation] = {}
        #: messages whose topic could not be parsed at all
        self.unroutable: list[Received] = []

    def feed(self, msg: Message) -> Received:
        rec = parse_message(msg)
        if rec.topic is None:
            self.unroutable.append(rec)
            return rec
        key = (rec.topic.manufacturer, rec.topic.serial)
        obs = self.robots.get(key)
        if obs is None:
            obs = Observation(manufacturer=key[0], serial=key[1],
                              interface=rec.topic.interface,
                              major=rec.topic.major)
            self.robots[key] = obs
        obs.add(rec)
        return rec

    def feed_all(self, messages: list[Message]) -> None:
        for m in messages:
            self.feed(m)

    def get(self, manufacturer: str, serial: str) -> Observation:
        key = (manufacturer, serial)
        obs = self.robots.get(key)
        if obs is None:
            obs = Observation(manufacturer=manufacturer, serial=serial,
                              interface=self.interface, major=self.major)
            self.robots[key] = obs
        return obs
