"""Message sources: JSONL files (testing/replay) and MQTT (production).

The MQTT source needs the optional ``paho-mqtt`` package (>=2.0); importing
this module without it is fine — only constructing :class:`MqttSource` fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterator

#: Default VDA 5050 state-topic subscription (interfaceName/majorVersion/+/+/state).
DEFAULT_STATE_TOPIC = "uagv/v2/+/+/state"
#: Default VDA 5050 connection-topic subscription (retained ONLINE/OFFLINE/
#: CONNECTIONBROKEN -- see translate.translate_connection).
DEFAULT_CONNECTION_TOPIC = "uagv/v2/+/+/connection"

_TOPIC_RE = re.compile(
    r"^[^/]+/v\d+/(?P<manufacturer>[^/]+)/(?P<serial>[^/]+)/(?:state|connection)$")


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield parsed state messages from a JSONL file, skipping blank lines.

    Malformed lines raise ``ValueError`` with the line number so bad fixture
    data is caught immediately rather than silently dropped.
    """
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(msg, dict):
                raise ValueError(f"{path}:{lineno}: expected a JSON object")
            yield msg


class MqttSource:
    """Subscribe to VDA 5050 state (and, optionally, connection) topics and
    invoke a callback per message.

    The topic path is authoritative for identity: if the payload's
    manufacturer/serialNumber disagree with (or lack) the topic segments,
    the topic values are filled in, per VDA 5050 header rules.
    """

    def __init__(
        self,
        on_state: Callable[[dict[str, Any]], None],
        *,
        host: str = "localhost",
        port: int = 1883,
        topic: str = DEFAULT_STATE_TOPIC,
        on_connection: Callable[[dict[str, Any]], None] | None = None,
        connection_topic: str = DEFAULT_CONNECTION_TOPIC,
        client_id: str = "yantrabridge",
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        try:
            import paho.mqtt.client as mqtt  # noqa: WPS433 (optional dep)
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "paho-mqtt is required for MQTT mode: "
                "pip install 'paho-mqtt>=2.0'"
            ) from exc

        self._on_state = on_state
        self._on_connection = on_connection
        self._host, self._port, self._topic = host, port, topic
        self._connection_topic = connection_topic
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = self._handle_connect
        self._client.on_message = self._handle_message

    # paho callbacks --------------------------------------------------------

    def _handle_connect(self, client: Any, userdata: Any, flags: Any,
                        reason_code: Any, properties: Any = None) -> None:
        client.subscribe(self._topic, qos=0)
        if self._on_connection is not None:
            # connection: QoS 1 per spec (retained ONLINE/OFFLINE/
            # CONNECTIONBROKEN -- a subscriber must see the last-known
            # state immediately, including one set by a broker last-will
            # that fired before this subscription existed).
            client.subscribe(self._connection_topic, qos=1)

    def _handle_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            msg = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return  # drop malformed payloads, keep the loop alive
        if not isinstance(msg, dict):
            return
        m = _TOPIC_RE.match(message.topic)
        if m:  # topic is authoritative for identity
            msg.setdefault("manufacturer", m.group("manufacturer"))
            msg.setdefault("serialNumber", m.group("serial"))
        if message.topic.endswith("/connection"):
            if self._on_connection is None:
                return
            try:
                self._on_connection(msg)
            except Exception as exc:  # never let one bad message kill the loop
                print(f"[yantrabridge] error handling connection message: {exc}")
            return
        try:
            self._on_state(msg)
        except Exception as exc:  # never let one bad message kill the loop
            print(f"[yantrabridge] error handling state message: {exc}")

    # publishing ------------------------------------------------------------

    def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        """Publish one message on this source's client (thread-safe in paho).

        Used by :class:`yantrabridge.commands.CommandPublisher` to send
        VDA instantActions over the same broker connection the bridge is
        already consuming state from.
        """
        self._client.publish(topic, payload, qos=qos)

    # lifecycle -------------------------------------------------------------

    def run_forever(self) -> None:
        """Connect and block, dispatching messages until interrupted."""
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_forever()

    def stop(self) -> None:
        self._client.disconnect()
