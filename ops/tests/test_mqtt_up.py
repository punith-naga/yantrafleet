"""Tests for the MQTT wire: embedded broker, CLI flags, and `up --mqtt`.

Everything runs on 127.0.0.1 ephemeral ports; the broker round-trip and the
stack smoke test skip cleanly when the optional ops[mqtt] extra (paho-mqtt +
amqtt) is not installed.
"""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from yantraops import broker as broker_mod
from yantraops.__main__ import main
from yantraops.broker import (AMQTT_AVAILABLE, PAHO_AVAILABLE,
                              mqtt_preflight, parse_broker)
from yantraops.orchestrator import FleetStack

needs_mqtt = pytest.mark.skipif(
    not (PAHO_AVAILABLE and AMQTT_AVAILABLE),
    reason="ops[mqtt] extra not installed (paho-mqtt + amqtt)")

POLL_BUDGET_S = 30.0


def _wait_for(predicate, deadline: float, interval: float = 0.4):
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


# --------------------------------------------------------------------------
# Pure helpers (no optional deps needed)
# --------------------------------------------------------------------------

def test_parse_broker_variants():
    assert parse_broker("mosquitto.local:1884") == ("mosquitto.local", 1884)
    assert parse_broker("10.0.0.5") == ("10.0.0.5", 1883)      # default port
    assert parse_broker("host:1883 ".strip()) == ("host", 1883)
    with pytest.raises(ValueError):
        parse_broker("")
    with pytest.raises(ValueError):
        parse_broker("host:notaport")
    with pytest.raises(ValueError):
        parse_broker("host:70000")


def test_mqtt_preflight_reports_missing_deps(monkeypatch):
    monkeypatch.setattr(broker_mod, "PAHO_AVAILABLE", False)
    monkeypatch.setattr(broker_mod, "AMQTT_AVAILABLE", False)
    err = mqtt_preflight(embedded=True)
    assert err and "paho-mqtt" in err and "amqtt" in err
    assert "pip install -e ops[mqtt]" in err
    # external broker: only the client library is needed
    err = mqtt_preflight(embedded=False)
    assert err and "paho-mqtt" in err and "amqtt" not in err

    monkeypatch.setattr(broker_mod, "PAHO_AVAILABLE", True)
    monkeypatch.setattr(broker_mod, "AMQTT_AVAILABLE", True)
    assert mqtt_preflight(embedded=True) is None


# --------------------------------------------------------------------------
# CLI flag validation (no stack is started; exit code 2 + stderr hint)
# --------------------------------------------------------------------------

def test_up_broker_requires_mqtt(capsys):
    assert main(["up", "--loopback", "--broker", "host:1883"]) == 2
    assert "--broker requires --mqtt" in capsys.readouterr().err


def test_up_no_sim_requires_mqtt(capsys):
    assert main(["up", "--loopback", "--no-sim"]) == 2
    assert "--no-sim requires --mqtt" in capsys.readouterr().err


def test_up_mqtt_graceful_error_when_paho_missing(monkeypatch, capsys):
    monkeypatch.setattr(broker_mod, "PAHO_AVAILABLE", False)
    assert main(["up", "--loopback", "--mqtt"]) == 2
    err = capsys.readouterr().err
    assert "paho-mqtt" in err and "pip install -e ops[mqtt]" in err


def test_up_mqtt_rejects_bad_broker_spec(monkeypatch, capsys):
    monkeypatch.setattr(broker_mod, "PAHO_AVAILABLE", True)
    assert main(["up", "--loopback", "--mqtt", "--broker", "host:nope"]) == 2
    assert "port" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Embedded broker round-trip: paho pub/sub over real localhost sockets
# --------------------------------------------------------------------------

@needs_mqtt
def test_embedded_broker_paho_roundtrip():
    import paho.mqtt.client as mqtt
    from yantraops.broker import EmbeddedBroker

    broker = EmbeddedBroker()
    host, port = broker.start()
    assert host == "127.0.0.1" and 0 < port < 65536
    assert broker.url == f"mqtt://{host}:{port}"
    try:
        got: list[tuple[str, bytes]] = []
        ev = threading.Event()

        sub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="ops-test-sub")
        sub.on_connect = (
            lambda c, u, f, rc, props=None: c.subscribe("uagv/v2/+/+/state"))
        def on_message(c, u, m):
            got.append((m.topic, m.payload))
            ev.set()
        sub.on_message = on_message
        sub.connect(host, port)
        sub.loop_start()
        time.sleep(0.5)  # let the SUBACK land

        pub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="ops-test-pub")
        pub.connect(host, port)
        pub.loop_start()
        payload = json.dumps({"serialNumber": "AMR_99"})
        deadline = time.time() + 10
        while not ev.is_set() and time.time() < deadline:
            pub.publish("uagv/v2/testco/AMR_99/state", payload, qos=0)
            ev.wait(0.5)
        pub.loop_stop(); pub.disconnect()
        sub.loop_stop(); sub.disconnect()

        assert got, "subscriber never received the published state"
        assert got[0][0] == "uagv/v2/testco/AMR_99/state"
        assert json.loads(got[0][1]) == {"serialNumber": "AMR_99"}
    finally:
        broker.stop()
    broker.stop()  # idempotent


# --------------------------------------------------------------------------
# Full stack smoke: broker + sim(mqtt) + yantrabridge feed the robots table
# --------------------------------------------------------------------------

@needs_mqtt
def test_mqtt_loopback_smoke(tmp_path):
    stack = FleetStack(
        loopback=True,
        copilot=False,
        mqtt=True,
        sim_interval=0.25,
        detect_interval=1.0,
        notify_interval=2.0,
        state_file=tmp_path / "state.json",
        quiet=True,
        open_browser=False,
    )
    try:
        info = stack.start()
        assert info.mqtt_url and info.mqtt_url.startswith("mqtt://127.0.0.1:")
        assert info.mqtt_embedded is True
        names = [s["name"] for s in info.services]
        assert "yantrasim" in names and "yantrabridge" in names

        banner = stack.banner()
        assert info.mqtt_url in banner
        assert "MQTT: VDA 5050 wire active" in banner

        # The readiness signal is unchanged: robots rows in the backend —
        # but now they arrive over the wire via the connector.
        deadline = time.time() + POLL_BUDGET_S
        with httpx.Client(timeout=5.0) as client:
            robots = _wait_for(
                lambda: client.get(f"{info.base_url}/rest/v1/robots").json(),
                deadline)
        assert robots, "no robots rows appeared through the MQTT wire"
        assert all(r["id"].startswith("AMR_") for r in robots), (
            "MQTT-path ids are VDA-legal serials (AMR_NN)")
    finally:
        stack.stop()
    for svc in stack.services:
        assert svc.proc.poll() is not None, f"{svc.name} still running"
    assert stack.broker is None and stack.fake is None
    assert not stack.state_file.exists()


def test_no_sim_stack_skips_simulator(tmp_path, monkeypatch):
    """--no-sim: no yantrasim child; the bridge still points at the broker."""
    if not (PAHO_AVAILABLE and AMQTT_AVAILABLE):
        pytest.skip("ops[mqtt] extra not installed")
    stack = FleetStack(
        loopback=True, copilot=False, mqtt=True, sim=False,
        state_file=tmp_path / "state.json", quiet=True, open_browser=False,
    )
    try:
        info = stack.start()
        names = [s["name"] for s in info.services]
        assert "yantrasim" not in names
        assert "yantrabridge" in names
    finally:
        stack.stop()
