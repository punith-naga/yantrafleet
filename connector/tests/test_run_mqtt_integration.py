"""End-to-end run_mqtt() smoke test: fully offline (a fake MqttSource
stands in for paho, httpx.MockTransport stands in for Supabase). Proves
the pieces wired in v0.18 (LiveMqttConfig, MqttConnectionManager) are
actually assembled and torn down cleanly by the real CLI entry point,
not just exercised in isolation.
"""
from __future__ import annotations

import threading

import httpx
import pytest

import yantrabridge.__main__ as main_mod
from yantrabridge.__main__ import build_parser, run_mqtt


class FakeMqttSource:
    """Stands in for yantrabridge.sources.MqttSource — no paho, no socket."""

    instances: list["FakeMqttSource"] = []

    def __init__(self, on_state, *, host, port, topic, on_connection=None,
                username=None, password=None):
        self.on_state = on_state
        self.host, self.port, self.topic = host, port, topic
        self.username, self.password = username, password
        self.connected = False
        self.stopped = False
        FakeMqttSource.instances.append(self)

    def connect_start(self) -> None:
        self.connected = True

    def disconnect_stop(self) -> None:
        self.stopped = True

    def publish(self, topic, payload, qos=0) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_fake_source():
    FakeMqttSource.instances.clear()
    yield
    FakeMqttSource.instances.clear()


def _args(argv):
    return build_parser().parse_args(argv)


def test_run_mqtt_connects_and_shuts_down_cleanly(monkeypatch):
    monkeypatch.setattr(main_mod, "MqttSource", FakeMqttSource)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    stop = threading.Event()
    stop.set()  # "Ctrl-C already pressed" — run_mqtt returns immediately

    args = _args(["--mqtt-host", "broker-a", "--mqtt-port", "1883",
                 "--dry-run"])
    rc = run_mqtt(args, transport=transport, stop_event=stop)
    assert rc == 0
    assert len(FakeMqttSource.instances) == 1
    src = FakeMqttSource.instances[0]
    assert src.host == "broker-a" and src.port == 1883
    assert src.connected is True
    assert src.stopped is True  # manager.stop() in the finally block


def test_run_mqtt_bootstraps_from_cli_when_no_app_config_rows(monkeypatch):
    monkeypatch.setattr(main_mod, "MqttSource", FakeMqttSource)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])  # no app_config rows anywhere

    transport = httpx.MockTransport(handler)
    stop = threading.Event()
    stop.set()

    args = _args(["--mqtt-host", "broker-a", "--mqtt-topic", "custom/topic",
                 "--dry-run"])
    run_mqtt(args, transport=transport, stop_event=stop)
    src = FakeMqttSource.instances[0]
    assert src.topic == "custom/topic"  # CLI flag used as bootstrap default


def test_run_mqtt_reconnects_when_config_poll_ticks_before_shutdown(monkeypatch):
    """A background config-poll tick that finds a changed
    CONNECTOR_MQTT_HOST reconnects the bridge to the new broker — proven
    through the real run_mqtt() entry point, not just LiveMqttConfig in
    isolation."""
    monkeypatch.setattr(main_mod, "MqttSource", FakeMqttSource)
    monkeypatch.setattr(main_mod, "CONFIG_POLL_INTERVAL_S", 0.02)

    rows = [{"key": "CONNECTOR_MQTT_HOST", "value": "broker-b"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app_config"):
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    stop = threading.Event()

    def _stop_after_a_reconnect():
        import time

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if len(FakeMqttSource.instances) >= 2:
                break
            time.sleep(0.01)
        stop.set()

    stopper = threading.Thread(target=_stop_after_a_reconnect, daemon=True)
    stopper.start()

    args = _args(["--mqtt-host", "broker-a", "--dry-run"])
    run_mqtt(args, transport=transport, stop_event=stop)
    stopper.join(timeout=2.0)

    assert len(FakeMqttSource.instances) >= 2
    assert FakeMqttSource.instances[0].host == "broker-a"
    assert FakeMqttSource.instances[0].stopped is True
    assert FakeMqttSource.instances[-1].host == "broker-b"
    assert FakeMqttSource.instances[-1].stopped is True  # torn down at shutdown too


def test_run_mqtt_unrelated_poll_never_reconnects(monkeypatch):
    """Same battery-threshold-only app_config row on every poll — must
    never build a second MqttSource."""
    monkeypatch.setattr(main_mod, "MqttSource", FakeMqttSource)
    monkeypatch.setattr(main_mod, "CONFIG_POLL_INTERVAL_S", 0.02)

    rows = [{"key": "CONNECTOR_BATTERY_THRESHOLD", "value": "15"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app_config"):
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    stop = threading.Event()

    def _stop_soon():
        import time

        time.sleep(0.2)
        stop.set()

    threading.Thread(target=_stop_soon, daemon=True).start()

    args = _args(["--mqtt-host", "broker-a", "--dry-run"])
    run_mqtt(args, transport=transport, stop_event=stop)
    assert len(FakeMqttSource.instances) == 1
