"""MqttConnectionManager: reconnect on an actual diff, never on an
unrelated poll tick. No real broker, no paho dependency — build_source
returns a fake with the two lifecycle methods the manager calls.
"""
from __future__ import annotations

import pytest

from yantrabridge.mqtt_runtime import MqttConnectionManager

PARAMS = {"host": "broker-a", "port": 1883, "topic": "uagv/v2/+/+/state",
          "username": None, "password": None}


class FakeSource:
    def __init__(self, params: dict, fail_connect: bool = False) -> None:
        self.params = dict(params)
        self._fail_connect = fail_connect
        self.connected = False
        self.stopped = False

    def connect_start(self) -> None:
        if self._fail_connect:
            raise ConnectionRefusedError(f"cannot reach {self.params['host']}")
        self.connected = True

    def disconnect_stop(self) -> None:
        self.stopped = True


def make_builder(fail_hosts: set[str] | None = None):
    fail_hosts = fail_hosts or set()
    built: list[FakeSource] = []

    def build(params: dict) -> FakeSource:
        src = FakeSource(params, fail_connect=params["host"] in fail_hosts)
        built.append(src)
        return src

    return build, built


# -- construction connects the initial source --------------------------------


def test_construction_connects_the_initial_source():
    build, built = make_builder()
    mgr = MqttConnectionManager(build, PARAMS)
    assert mgr.source is built[0]
    assert built[0].connected is True
    assert mgr.params == PARAMS


# -- no diff -> no reconnect --------------------------------------------------


def test_apply_same_params_does_not_reconnect():
    build, built = make_builder()
    mgr = MqttConnectionManager(build, PARAMS)
    original_source = mgr.source
    changed = mgr.apply(dict(PARAMS))
    assert changed is False
    assert mgr.source is original_source
    assert len(built) == 1  # no second source ever built
    assert original_source.stopped is False


def test_apply_is_called_repeatedly_with_no_diff_stays_quiet():
    """Simulates many poll ticks where nothing changed — must never
    reconnect just because a poll happened."""
    build, built = make_builder()
    mgr = MqttConnectionManager(build, PARAMS)
    for _ in range(10):
        assert mgr.apply(dict(PARAMS)) is False
    assert len(built) == 1


# -- an actual diff -> reconnect ----------------------------------------------


def test_apply_different_host_reconnects():
    build, built = make_builder()
    mgr = MqttConnectionManager(build, PARAMS)
    old_source = mgr.source
    new_params = dict(PARAMS, host="broker-b")
    changed = mgr.apply(new_params)
    assert changed is True
    assert len(built) == 2
    assert mgr.source is built[1]
    assert mgr.source.connected is True
    assert old_source.stopped is True  # old connection torn down
    assert mgr.params == new_params


@pytest.mark.parametrize("key,value", [
    ("port", 1884), ("topic", "uagv/v2/+/+/other"),
    ("username", "u"), ("password", "p"),
])
def test_apply_any_single_field_change_reconnects(key, value):
    build, built = make_builder()
    mgr = MqttConnectionManager(build, PARAMS)
    new_params = dict(PARAMS, **{key: value})
    assert mgr.apply(new_params) is True
    assert len(built) == 2


# -- unreachable new broker: keep the old connection, don't crash -----------


def test_apply_unreachable_new_broker_keeps_old_connection():
    build, built = make_builder(fail_hosts={"broker-b"})
    mgr = MqttConnectionManager(build, PARAMS)
    old_source = mgr.source
    changed = mgr.apply(dict(PARAMS, host="broker-b"))
    assert changed is False
    assert mgr.source is old_source          # unchanged
    assert old_source.stopped is False        # never torn down
    assert mgr.params == PARAMS                # still the old params
    assert len(built) == 2                     # the failed attempt was built...
    assert built[1].connected is False         # ...but never connected


def test_apply_retries_on_next_poll_after_a_failed_reconnect():
    fail = {"broker-b"}
    build, built = make_builder(fail_hosts=fail)
    mgr = MqttConnectionManager(build, PARAMS)
    assert mgr.apply(dict(PARAMS, host="broker-b")) is False
    fail.clear()  # broker-b becomes reachable
    assert mgr.apply(dict(PARAMS, host="broker-b")) is True
    assert mgr.source.params["host"] == "broker-b"


# -- stop() ------------------------------------------------------------------


def test_stop_disconnects_the_current_source():
    build, built = make_builder()
    mgr = MqttConnectionManager(build, PARAMS)
    mgr.stop()
    assert built[0].stopped is True


def test_stop_never_raises_even_if_disconnect_fails():
    def build(params):
        class Boom:
            def connect_start(self):
                pass

            def disconnect_stop(self):
                raise RuntimeError("socket already closed")

        return Boom()

    mgr = MqttConnectionManager(build, PARAMS)
    mgr.stop()  # must not raise
