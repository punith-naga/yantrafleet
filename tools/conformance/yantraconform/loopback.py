"""An in-process MQTT broker good enough to conformance-test against.

Why this exists: the tester's most important test is "does it correctly grade
a real AGV implementation?", and the only AGV implementation available offline
is this repo's own simulator (``yantrasim``), which speaks paho. Standing up
mosquitto in a unit test is not an option, so this module provides:

* :class:`LoopbackBroker` -- topic-filter matching (``+`` and ``#``), retained
  messages, and last-will delivery on ungraceful disconnect. That is the whole
  feature set VDA 5050 leans on.
* :class:`LoopbackSession` -- a :class:`~yantraconform.session.Session` over
  that broker, so the conformance engine runs unmodified.
* :class:`PahoShimClient` -- an object shaped like ``paho.mqtt.client.Client``
  (``publish``/``subscribe``/``on_message``/``on_connect``/``will_set``/
  ``loop_stop``/``disconnect``) backed by the same broker, so any code written
  against paho -- ``yantrasim.transports.mqtt.MqttTransport`` included -- can
  be wired straight in.

It is NOT a real broker: no QoS 1/2 retry state machine, no persistent
sessions, no wildcards in publish topics, no ordering guarantees beyond
"delivered in publish order". Everything is synchronous on the calling thread,
which is exactly what makes the tests deterministic.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .session import Message


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT topic-filter match with ``+`` (one level) and ``#`` (rest)."""
    f = filter_.split("/")
    t = topic.split("/")
    for i, seg in enumerate(f):
        if seg == "#":
            # '#' must be last and matches the remainder (>= 0 levels), but
            # never a leading '$' topic -- irrelevant here, VDA topics never
            # start with '$'.
            return i == len(f) - 1
        if i >= len(t):
            return False
        if seg != "+" and seg != t[i]:
            return False
    return len(f) == len(t)


@dataclass
class _Will:
    topic: str
    payload: str
    qos: int = 0
    retain: bool = False


@dataclass
class _Subscriber:
    """One connected client's view: its filters and its delivery hook."""

    name: str
    deliver: Callable[[Message], None]
    filters: list[tuple[str, int]] = field(default_factory=list)
    will: _Will | None = None
    connected: bool = True


class LoopbackBroker:
    """A minimal, synchronous, in-process MQTT broker."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: list[_Subscriber] = []
        #: topic -> (payload, qos) for retained messages
        self.retained: dict[str, tuple[str, int]] = {}
        #: every publish that ever crossed the broker (topic, payload, qos, retain)
        self.log: list[tuple[str, str, int, bool]] = []

    # -- client lifecycle ---------------------------------------------------

    def register(self, name: str, deliver: Callable[[Message], None],
                 will: _Will | None = None) -> _Subscriber:
        sub = _Subscriber(name=name, deliver=deliver, will=will)
        with self._lock:
            self._subs.append(sub)
        return sub

    def disconnect(self, sub: _Subscriber, graceful: bool = True) -> None:
        """Drop a client. ``graceful=False`` publishes its last-will."""
        with self._lock:
            sub.connected = False
            if sub in self._subs:
                self._subs.remove(sub)
            will = sub.will
        if not graceful and will is not None:
            self.publish(will.topic, will.payload, qos=will.qos,
                         retain=will.retain)

    # -- pub/sub ------------------------------------------------------------

    def subscribe(self, sub: _Subscriber, topic_filter: str, qos: int = 0) -> None:
        with self._lock:
            sub.filters.append((topic_filter, qos))
            retained = [(t, p, q) for t, (p, q) in self.retained.items()
                        if topic_matches(topic_filter, t)]
        for topic, payload, q in retained:
            sub.deliver(Message(topic=topic, payload=payload.encode("utf-8"),
                                qos=min(q, qos), retain=True))

    def publish(self, topic: str, payload: str, qos: int = 0,
                retain: bool = False) -> None:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        with self._lock:
            self.log.append((topic, payload, qos, retain))
            if retain:
                if payload == "":
                    self.retained.pop(topic, None)  # zero-byte clears retention
                else:
                    self.retained[topic] = (payload, qos)
            targets = [(s, min(qos, fq)) for s in self._subs
                       for f, fq in s.filters if topic_matches(f, topic)]
        seen: set[int] = set()
        for sub, eff_qos in targets:
            if id(sub) in seen:
                continue  # one copy per client even on overlapping filters
            seen.add(id(sub))
            sub.deliver(Message(topic=topic, payload=payload.encode("utf-8"),
                                qos=eff_qos, retain=False))

    # -- introspection used by the last-will conformance check --------------

    def wills(self) -> list[tuple[str, str]]:
        """(topic, payload) of every registered last-will, for inspection."""
        with self._lock:
            return [(s.will.topic, s.will.payload)
                    for s in self._subs if s.will is not None]


class LoopbackSession:
    """:class:`~yantraconform.session.Session` over a :class:`LoopbackBroker`."""

    def __init__(self, broker: LoopbackBroker, name: str = "yantra-conform") -> None:
        self.broker = broker
        self._inbox: list[Message] = []
        # NB: the delivery hook must go through a method, not a bound
        # ``self._inbox.append``. ``drain`` empties the list IN PLACE for the
        # same reason -- rebinding ``self._inbox`` to a fresh list would leave
        # the broker appending into the detached original, and the session
        # would silently receive nothing after its first drain.
        self._sub = broker.register(name, self._deliver)

    def _deliver(self, msg: Message) -> None:
        self._inbox.append(msg)

    def subscribe(self, topic_filter: str, qos: int = 0) -> None:
        self.broker.subscribe(self._sub, topic_filter, qos)

    def publish(self, topic: str, payload: str, qos: int = 0,
                retain: bool = False) -> None:
        self.broker.publish(topic, payload, qos=qos, retain=retain)

    def drain(self) -> list[Message]:
        batch = list(self._inbox)
        del self._inbox[:]
        return batch

    def close(self) -> None:
        self.broker.disconnect(self._sub, graceful=True)

    # Optional capability the engine feature-detects: on a real broker a
    # client cannot see another client's last-will, so the corresponding
    # check is skipped there and evaluated here.
    def wills(self) -> list[tuple[str, str]]:
        return self.broker.wills()


class _ShimMessage:
    """Duck-type of a paho MQTTMessage."""

    __slots__ = ("topic", "payload", "qos", "retain")

    def __init__(self, msg: Message) -> None:
        self.topic = msg.topic
        self.payload = msg.payload
        self.qos = msg.qos
        self.retain = msg.retain


class PahoShimClient:
    """``paho.mqtt.client.Client``-shaped adapter over a LoopbackBroker.

    Enough of the surface for a device implementation written against paho to
    run headless in a test: ``publish``, ``subscribe``, ``will_set``, the
    ``on_message``/``on_connect`` callbacks, and ``loop_start``/``loop_stop``/
    ``disconnect`` no-ops. Delivery is synchronous, on the publishing thread.
    """

    def __init__(self, broker: LoopbackBroker, name: str = "device") -> None:
        self.broker = broker
        self.name = name
        self.on_message: Callable[..., None] | None = None
        self.on_connect: Callable[..., None] | None = None
        self.published: list[tuple[str, str, int, bool]] = []
        self._will: _Will | None = None
        self._sub = broker.register(name, self._deliver)

    # -- paho surface ------------------------------------------------------

    def will_set(self, topic: str, payload: str = "", qos: int = 0,
                 retain: bool = False) -> None:
        self._will = _Will(topic=topic, payload=payload, qos=qos, retain=retain)
        self._sub.will = self._will

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.broker.subscribe(self._sub, topic, qos)

    def publish(self, topic: str, payload: Any, qos: int = 0,
                retain: bool = False) -> None:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        self.published.append((topic, payload, qos, retain))
        self.broker.publish(topic, payload, qos=qos, retain=retain)

    def loop_start(self) -> None:  # pragma: no cover - trivial no-op
        pass

    def loop_stop(self) -> None:  # pragma: no cover - trivial no-op
        pass

    def disconnect(self) -> None:
        self.broker.disconnect(self._sub, graceful=True)

    # -- test affordance ---------------------------------------------------

    def kill(self) -> None:
        """Simulate a network drop: the broker publishes this client's will."""
        self.broker.disconnect(self._sub, graceful=False)

    def fire_on_connect(self) -> None:
        """Invoke the device's on_connect callback (reconnect simulation)."""
        if self.on_connect is not None:
            self.on_connect(self, None, {}, 0, None)

    def _deliver(self, msg: Message) -> None:
        if self.on_message is not None:
            self.on_message(self, None, _ShimMessage(msg))
