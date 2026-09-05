"""Offline tests for MqttSource: a recording fake client, no real broker.

paho-mqtt is required to *construct* MqttSource (it imports paho.mqtt.client
in __init__), so these tests are skipped in an environment without it --
same policy as the rest of the optional-mqtt-dependency modules.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from yantrabridge.sources import (
    DEFAULT_CONNECTION_TOPIC,
    DEFAULT_STATE_TOPIC,
    MqttSource,
)

pytest.importorskip("paho.mqtt.client")


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeClient:
    """Stands in for paho.mqtt.client.Client: records subscribe() calls and
    lets tests drive on_connect/on_message directly (no real socket)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self.subscribed: list[tuple[str, int]] = []
        self.on_connect = None
        self.on_message = None
        self.username_pw_set_args: tuple[Any, Any] | None = None

    def username_pw_set(self, username: Any, password: Any) -> None:
        self.username_pw_set_args = (username, password)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        pass

    def loop_forever(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        pass


def make_source(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[MqttSource, FakeClient]:
    fake = FakeClient()
    monkeypatch.setattr(
        "paho.mqtt.client.Client", lambda *a, **kw: fake)
    on_state = kwargs.pop("on_state", lambda msg: None)
    source = MqttSource(on_state, **kwargs)
    return source, fake


class TestSubscriptions:
    def test_subscribes_state_topic_only_without_on_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source, fake = make_source(monkeypatch)
        source._handle_connect(fake, None, None, 0)
        assert fake.subscribed == [(DEFAULT_STATE_TOPIC, 0)]

    def test_subscribes_connection_topic_qos1_when_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source, fake = make_source(monkeypatch, on_connection=lambda msg: None)
        source._handle_connect(fake, None, None, 0)
        assert (DEFAULT_STATE_TOPIC, 0) in fake.subscribed
        assert (DEFAULT_CONNECTION_TOPIC, 1) in fake.subscribed


class TestMessageDispatch:
    def test_state_message_dispatched_to_on_state_with_topic_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = []
        source, fake = make_source(monkeypatch, on_state=seen.append)
        msg = FakeMessage(
            "uagv/v2/nexomotion/AMR_01/state",
            json.dumps({"orderId": "o1"}).encode())
        source._handle_message(fake, None, msg)
        assert len(seen) == 1
        assert seen[0]["manufacturer"] == "nexomotion"
        assert seen[0]["serialNumber"] == "AMR_01"

    def test_connection_message_dispatched_to_on_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_seen = []
        conn_seen = []
        source, fake = make_source(
            monkeypatch, on_state=state_seen.append, on_connection=conn_seen.append)
        msg = FakeMessage(
            "uagv/v2/nexomotion/AMR_01/connection",
            json.dumps({"connectionState": "OFFLINE"}).encode())
        source._handle_message(fake, None, msg)
        assert state_seen == []
        assert len(conn_seen) == 1
        assert conn_seen[0]["connectionState"] == "OFFLINE"
        assert conn_seen[0]["manufacturer"] == "nexomotion"
        assert conn_seen[0]["serialNumber"] == "AMR_01"

    def test_connection_message_dropped_when_no_callback_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_seen = []
        source, fake = make_source(monkeypatch, on_state=state_seen.append)
        msg = FakeMessage(
            "uagv/v2/nexomotion/AMR_01/connection",
            json.dumps({"connectionState": "OFFLINE"}).encode())
        source._handle_message(fake, None, msg)  # must not raise
        assert state_seen == []  # never misrouted to on_state either

    def test_malformed_payload_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = []
        source, fake = make_source(monkeypatch, on_state=seen.append)
        msg = FakeMessage("uagv/v2/nexomotion/AMR_01/state", b"not json")
        source._handle_message(fake, None, msg)
        assert seen == []

    def test_on_state_exception_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(msg: Any) -> None:
            raise RuntimeError("boom")

        source, fake = make_source(monkeypatch, on_state=boom)
        msg = FakeMessage(
            "uagv/v2/nexomotion/AMR_01/state", json.dumps({}).encode())
        source._handle_message(fake, None, msg)  # must not raise

    def test_on_connection_exception_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(msg: Any) -> None:
            raise RuntimeError("boom")

        source, fake = make_source(monkeypatch, on_connection=boom)
        msg = FakeMessage(
            "uagv/v2/nexomotion/AMR_01/connection",
            json.dumps({"connectionState": "OFFLINE"}).encode())
        source._handle_message(fake, None, msg)  # must not raise
