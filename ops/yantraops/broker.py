"""Embedded MQTT broker for ``yantraops up --mqtt`` (demo/tests, no mosquitto).

Wraps the pure-Python `amqtt <https://pypi.org/project/amqtt/>`_ broker in a
daemon thread with its own asyncio loop, bound to ``127.0.0.1`` on an
ephemeral port, so the whole VDA 5050 wire (sim -> broker -> yantrabridge)
runs offline with zero external services.

Both ``amqtt`` (the broker) and ``paho-mqtt`` (the clients in yantrasim and
yantrabridge) are OPTIONAL: install them with ``pip install -e ops[mqtt]``.
This module always imports; only :class:`EmbeddedBroker` and the running
stack need them, and :func:`mqtt_preflight` produces the friendly error.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import warnings

try:  # optional dependency (broker side)
    from amqtt.broker import Broker as _AmqttBroker
except ImportError:  # pragma: no cover - env-dependent
    _AmqttBroker = None

try:  # optional dependency (client side, used by sim + connector children)
    import paho.mqtt.client as _paho  # noqa: F401
except ImportError:  # pragma: no cover - env-dependent
    _paho = None

AMQTT_AVAILABLE = _AmqttBroker is not None
PAHO_AVAILABLE = _paho is not None

INSTALL_HINT = "pip install -e ops[mqtt]"


def mqtt_preflight(embedded: bool) -> str | None:
    """Return a human-readable error when --mqtt cannot run, else ``None``.

    ``embedded`` is True when no external ``--broker`` was given, i.e. the
    amqtt in-process broker is needed too (paho alone suffices otherwise).
    """
    missing = []
    if not PAHO_AVAILABLE:
        missing.append("paho-mqtt")
    if embedded and not AMQTT_AVAILABLE:
        missing.append("amqtt (embedded broker)")
    if missing:
        return (f"--mqtt needs {' and '.join(missing)} — run: {INSTALL_HINT}")
    return None


def parse_broker(spec: str) -> tuple[str, int]:
    """Parse a ``host:port`` broker spec (port optional, default 1883)."""
    spec = spec.strip()
    if not spec:
        raise ValueError("empty --broker value; expected HOST[:PORT]")
    host, sep, port_s = spec.rpartition(":")
    if not sep:
        return spec, 1883
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"invalid --broker port {port_s!r} in {spec!r}") from None
    if not (0 < port < 65536):
        raise ValueError(f"--broker port out of range: {port}")
    return (host or "localhost"), port


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EmbeddedBroker:
    """In-process MQTT 3.1.1 broker (amqtt) on a 127.0.0.1 ephemeral port.

    Usage::

        broker = EmbeddedBroker()
        host, port = broker.start()   # blocks until the socket accepts
        ...                           # paho clients pub/sub via host:port
        broker.stop()
    """

    START_TIMEOUT_S = 15.0

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        if _AmqttBroker is None:
            raise RuntimeError(f"amqtt is not installed — run: {INSTALL_HINT}")
        self.host = host
        self.port = port if port is not None else _free_port()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broker: object | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"mqtt://{self.host}:{self.port}"

    def start(self) -> tuple[str, int]:
        """Start the broker thread; return ``(host, port)`` once listening."""
        # amqtt is chatty at INFO/WARNING even on a happy path; keep the
        # yantraops banner readable. (Children log via their own processes.)
        logging.getLogger("amqtt").setLevel(logging.ERROR)
        logging.getLogger("transitions").setLevel(logging.ERROR)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="yantraops-mqtt-broker", daemon=True)
        self._thread.start()
        if not self._started.wait(self.START_TIMEOUT_S):
            self.stop()
            raise RuntimeError("embedded MQTT broker did not start in time")
        if self._error is not None:
            err, self._error = self._error, None
            self.stop()
            raise RuntimeError(f"embedded MQTT broker failed to start: {err}")
        return self.host, self.port

    def stop(self) -> None:
        """Shut the broker down and join the thread (idempotent)."""
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        if thread.is_alive():
            if self._broker is not None:
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        self._broker.shutdown(), loop)  # type: ignore[attr-defined]
                    fut.result(timeout=5)
                except Exception:  # pragma: no cover - best-effort teardown
                    pass
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._broker = None

    # -- internals ---------------------------------------------------------

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        config = {
            "listeners": {
                "default": {"type": "tcp",
                            "bind": f"{self.host}:{self.port}"},
            },
            "sys_interval": 0,                    # no $SYS spam
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
        async def _boot() -> None:
            # amqtt requires a running loop at construction time.
            with warnings.catch_warnings():
                # amqtt 0.12 warns about its own entry-point plugin loading.
                warnings.simplefilter("ignore", DeprecationWarning)
                self._broker = _AmqttBroker(config)  # type: ignore[misc]
            await self._broker.start()  # type: ignore[attr-defined]

        try:
            self._loop.run_until_complete(_boot())
        except BaseException as exc:  # surface to start() in the main thread
            self._error = exc
            self._started.set()
            return
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:  # pragma: no cover
                pass
