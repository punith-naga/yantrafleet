"""LiveMqttConfig: the poll_and_apply() tick that ties public.app_config
into a running MQTT bridge — battery threshold as a simple value swap,
broker params reconnected only on an actual diff. No real broker: the
manager under test uses fakes exactly like test_mqtt_runtime.py.
"""
from __future__ import annotations

import httpx

from yantrabridge.mqtt_runtime import LiveMqttConfig, MqttConnectionManager

URL = "https://x.invalid"

DEFAULTS = {
    "mqtt_host": "broker-a", "mqtt_port": 1883,
    "mqtt_topic": "uagv/v2/+/+/state", "mqtt_username": None,
    "mqtt_password": None, "battery_threshold": 20.0,
}


class FakeSource:
    def __init__(self, params: dict) -> None:
        self.params = dict(params)
        self.stopped = False

    def connect_start(self) -> None:
        pass

    def disconnect_stop(self) -> None:
        self.stopped = True


class FakeDeduper:
    def __init__(self, battery_threshold: float = 20.0) -> None:
        self.battery_threshold = battery_threshold


def _manager():
    built = []

    def build(params):
        s = FakeSource(params)
        built.append(s)
        return s

    initial = {"host": DEFAULTS["mqtt_host"], "port": DEFAULTS["mqtt_port"],
               "topic": DEFAULTS["mqtt_topic"], "username": None, "password": None}
    return MqttConnectionManager(build, initial), built


def _table_poller(rows: list[dict]) -> "TablePoller":
    from yantracore.runtime_config import TablePoller

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TablePoller(URL, "key", table="app_config",
                       keys=("CONNECTOR_BATTERY_THRESHOLD", "CONNECTOR_MQTT_HOST",
                             "CONNECTOR_MQTT_PORT", "CONNECTOR_MQTT_TOPIC",
                             "CONNECTOR_MQTT_USERNAME", "CONNECTOR_MQTT_PASSWORD"),
                       client=client)


# -- battery threshold: simple value swap, no reconnect ----------------------


def test_battery_threshold_applied_live_without_reconnect():
    manager, built = _manager()
    deduper = FakeDeduper(20.0)
    config = _table_poller([{"key": "CONNECTOR_BATTERY_THRESHOLD", "value": "15"}])
    live = LiveMqttConfig(config, manager, deduper, DEFAULTS)

    reconnected = live.poll_and_apply()
    assert deduper.battery_threshold == 15.0
    assert reconnected is False
    assert len(built) == 1  # no new source built


def test_no_config_row_keeps_hardcoded_battery_threshold():
    manager, built = _manager()
    deduper = FakeDeduper(20.0)
    config = _table_poller([])
    live = LiveMqttConfig(config, manager, deduper, DEFAULTS)

    live.poll_and_apply()
    assert deduper.battery_threshold == 20.0


# -- mqtt params: reconnect only on an actual diff ---------------------------


def test_no_mqtt_config_rows_never_reconnects():
    manager, built = _manager()
    deduper = FakeDeduper()
    config = _table_poller([])
    live = LiveMqttConfig(config, manager, deduper, DEFAULTS)

    for _ in range(5):
        assert live.poll_and_apply() is False
    assert len(built) == 1


def test_changed_host_triggers_reconnect():
    manager, built = _manager()
    deduper = FakeDeduper()
    config = _table_poller([{"key": "CONNECTOR_MQTT_HOST", "value": "broker-b"}])
    live = LiveMqttConfig(config, manager, deduper, DEFAULTS)

    assert live.poll_and_apply() is True
    assert len(built) == 2
    assert manager.source.params["host"] == "broker-b"
    assert built[0].stopped is True


def test_unchanged_config_value_does_not_reconnect_on_repeated_polls():
    manager, built = _manager()
    deduper = FakeDeduper()
    config = _table_poller([{"key": "CONNECTOR_MQTT_HOST", "value": "broker-b"}])
    live = LiveMqttConfig(config, manager, deduper, DEFAULTS)

    assert live.poll_and_apply() is True   # first application: reconnect
    assert live.poll_and_apply() is False  # same value again: no-op
    assert live.poll_and_apply() is False
    assert len(built) == 2


def test_all_five_mqtt_fields_flow_through():
    manager, built = _manager()
    deduper = FakeDeduper()
    config = _table_poller([
        {"key": "CONNECTOR_MQTT_HOST", "value": "broker-c"},
        {"key": "CONNECTOR_MQTT_PORT", "value": "8883"},
        {"key": "CONNECTOR_MQTT_TOPIC", "value": "uagv/v2/+/+/other"},
        {"key": "CONNECTOR_MQTT_USERNAME", "value": "svc"},
        {"key": "CONNECTOR_MQTT_PASSWORD", "value": "secret"},
    ])
    live = LiveMqttConfig(config, manager, deduper, DEFAULTS)

    assert live.poll_and_apply() is True
    params = manager.source.params
    assert params == {"host": "broker-c", "port": 8883,
                      "topic": "uagv/v2/+/+/other", "username": "svc",
                      "password": "secret"}
