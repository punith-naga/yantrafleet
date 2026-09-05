"""The tester's MQTT surface: a tiny protocol plus a paho-backed session.

The whole conformance engine talks to exactly four methods -- ``subscribe``,
``publish``, ``drain`` and ``close`` (plus an optional ``wills``) -- so it can
be driven against a real broker (``PahoSession``) or against the in-process
:mod:`yantraconform.loopback` broker with no change in the code under test.
That is what makes the integration test against this repo's own simulator a
real end-to-end exercise of the tester rather than a mock of it.

``paho-mqtt`` is an OPTIONAL dependency: this module always imports; only
constructing a :class:`PahoSession` requires it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

try:  # optional dependency
    import paho.mqtt.client as _paho
except ImportError:  # pragma: no cover - covered by the PAHO_AVAILABLE branch
    _paho = None

PAHO_AVAILABLE = _paho is not None

DEFAULT_PORT = 1883
DEFAULT_TLS_PORT = 8883


@dataclass
class Message:
    """One received MQTT message, as the tester sees it."""

    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    #: monotonic receive time; only ever used for ordering/latency, never for
    #: the report's wall-clock timestamps.
    received_at: float = field(default_factory=time.monotonic)

    def text(self) -> str:
        try:
            return self.payload.decode("utf-8")
        except UnicodeDecodeError:
            return self.payload.decode("utf-8", "replace")


@runtime_checkable
class Session(Protocol):
    """What the conformance engine needs from a broker connection."""

    def subscribe(self, topic_filter: str, qos: int = 0) -> None: ...

    def publish(self, topic: str, payload: str, qos: int = 0,
                retain: bool = False) -> None: ...

    def drain(self) -> list[Message]:
        """Return every message received since the previous drain."""

    def close(self) -> None: ...


@dataclass
class BrokerTarget:
    """A parsed broker address."""

    host: str = "localhost"
    port: int = DEFAULT_PORT
    tls: bool = False
    username: str | None = None
    password: str | None = None

    def __str__(self) -> str:
        scheme = "mqtts" if self.tls else "mqtt"
        who = f"{self.username}@" if self.username else ""
        return f"{scheme}://{who}{self.host}:{self.port}"


def parse_broker(value: str, username: str | None = None,
                 password: str | None = None) -> BrokerTarget:
    """Parse ``mqtt://user:pass@host:1883``, ``host:1883`` or ``host``.

    Explicit ``username``/``password`` arguments win over anything embedded in
    the URL, so a CLI flag can override a value baked into a config string.
    """
    raw = (value or "").strip()
    if "://" not in raw:
        raw = "mqtt://" + raw
    u = urlparse(raw)
    tls = u.scheme in ("mqtts", "ssl", "mqtt+ssl", "wss")
    port = u.port or (DEFAULT_TLS_PORT if tls else DEFAULT_PORT)
    return BrokerTarget(
        host=u.hostname or "localhost",
        port=port,
        tls=tls,
        username=username or (u.username or None),
        password=password or (u.password or None),
    )


class PahoSession:
    """A real MQTT connection, buffering everything it receives.

    Deliberately dumb: no auto-resubscribe games, no filtering. The engine
    subscribes once and drains; the broker's own session handling is what is
    being measured, so the tester adds no compensating behaviour of its own.
    """

    def __init__(self, target: BrokerTarget, client_id: str = "yantra-conform",
                 keepalive: int = 30, connect_timeout: float = 10.0) -> None:
        if _paho is None:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "paho-mqtt is not installed; run "
                "'pip install yantraconform[mqtt]' (or 'pip install paho-mqtt') "
                "to test a real broker")
        self.target = target
        self._lock = threading.Lock()
        self._inbox: list[Message] = []
        self._connected = threading.Event()
        self._connect_error: str | None = None
        self.client = _paho.Client(
            callback_api_version=_paho.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=_paho.MQTTv311,
        )
        if target.username:
            self.client.username_pw_set(target.username, target.password or "")
        if target.tls:
            self.client.tls_set()
        self.client.on_message = self._on_message
        self.client.on_connect = self._on_connect
        self.client.connect(target.host, target.port, keepalive=keepalive)
        self.client.loop_start()
        if not self._connected.wait(connect_timeout):
            self.close()
            raise TimeoutError(
                f"no CONNACK from {target} within {connect_timeout:g}s")
        if self._connect_error:
            self.close()
            raise ConnectionError(f"{target} refused the connection: "
                                  f"{self._connect_error}")

    # -- Session protocol --------------------------------------------------

    def subscribe(self, topic_filter: str, qos: int = 0) -> None:
        self.client.subscribe(topic_filter, qos=qos)

    def publish(self, topic: str, payload: str, qos: int = 0,
                retain: bool = False) -> None:
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def drain(self) -> list[Message]:
        with self._lock:
            batch, self._inbox = self._inbox, []
        return batch

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # pragma: no cover - best effort teardown
            pass

    # -- paho callbacks ----------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        code = getattr(reason_code, "value", reason_code)
        if code not in (0, None):
            self._connect_error = str(reason_code)
        self._connected.set()

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        payload = message.payload
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        with self._lock:
            self._inbox.append(Message(
                topic=message.topic,
                payload=payload or b"",
                qos=int(getattr(message, "qos", 0) or 0),
                retain=bool(getattr(message, "retain", False)),
            ))
