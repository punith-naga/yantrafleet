"""TablePoller (yantracore.runtime_config) — the shared mechanism behind
every settings/config sync in the codebase.

No network anywhere: an httpx.MockTransport-backed client is injected.
"""
from __future__ import annotations

import time

import httpx
import pytest

from yantracore.runtime_config import (
    TablePoller,
    coerce_float,
    coerce_int,
    resolve_value,
)

URL = "https://x.invalid"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _rows_handler(rows: list[dict], expect_table: str = "app_config"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/rest/v1/{expect_table}"
        return httpx.Response(200, json=rows)

    return handler


# -- construction never touches the network ----------------------------------


def test_construction_does_not_touch_network() -> None:
    poller = TablePoller(URL, "key", table="app_config", keys=("A",))
    assert poller.snapshot() == {}
    poller.close()


# -- basic fetch --------------------------------------------------------------


def test_poll_once_returns_snapshot() -> None:
    rows = [{"key": "A", "value": "1"}, {"key": "B", "value": "2"}]
    poller = TablePoller(URL, "key", table="app_config", keys=("A", "B"),
                         client=_client(_rows_handler(rows)))
    result = poller.poll_once()
    assert result == {"A": "1", "B": "2"}
    assert poller.get("A") == "1"
    assert poller.get("C") is None


def test_only_tracked_keys_are_kept() -> None:
    rows = [{"key": "A", "value": "1"}, {"key": "UNTRACKED", "value": "x"}]
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(_rows_handler(rows)))
    poller.poll_once()
    assert poller.snapshot() == {"A": "1"}


def test_empty_value_rows_are_dropped() -> None:
    rows = [{"key": "A", "value": ""}, {"key": "B", "value": "2"}]
    poller = TablePoller(URL, "key", table="app_config", keys=("A", "B"),
                         client=_client(_rows_handler(rows)))
    poller.poll_once()
    assert poller.snapshot() == {"B": "2"}


# -- failure modes degrade quietly, never raise ------------------------------


def test_unreachable_backend_keeps_previous_snapshot() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(boom))
    poller.poll_once()  # must not raise
    assert poller.snapshot() == {}


def test_404_before_migration_is_applied_keeps_previous_snapshot() -> None:
    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "relation does not exist"})

    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(not_found))
    poller.poll_once()
    assert poller.snapshot() == {}


def test_malformed_payload_keeps_previous_snapshot() -> None:
    def weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(weird))
    poller.poll_once()
    assert poller.snapshot() == {}


def test_poll_failure_leaves_previous_values_untouched() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[{"key": "A", "value": "1"}])
        raise httpx.ConnectError("blip", request=request)

    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(handler))
    poller.poll_once()
    assert poller.get("A") == "1"
    poller.poll_once()
    assert poller.get("A") == "1"


# -- maybe_poll() throttling with an injectable clock ------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_maybe_poll_first_call_always_polls() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    clock = FakeClock()
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         interval_s=30.0, client=_client(handler), clock=clock)
    poller.maybe_poll()
    assert calls["n"] == 1


def test_maybe_poll_skips_before_interval_elapses() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    clock = FakeClock()
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         interval_s=30.0, client=_client(handler), clock=clock)
    poller.maybe_poll()
    clock.advance(10.0)
    poller.maybe_poll()
    assert calls["n"] == 1


def test_maybe_poll_polls_again_once_interval_elapses() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    clock = FakeClock()
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         interval_s=30.0, client=_client(handler), clock=clock)
    poller.maybe_poll()
    clock.advance(30.0)
    poller.maybe_poll()
    assert calls["n"] == 2


# -- lifecycle: start()/stop() -------------------------------------------


def test_start_polls_once_synchronously_before_returning() -> None:
    rows = [{"key": "A", "value": "1"}]
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(_rows_handler(rows)), interval_s=999.0)
    poller.start()
    try:
        assert poller.get("A") == "1"
    finally:
        poller.stop()


def test_start_then_stop_joins_the_background_thread() -> None:
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(_rows_handler([])), interval_s=0.01)
    poller.start()
    assert poller._thread is not None and poller._thread.is_alive()
    poller.stop()
    assert poller._thread is None


def test_background_thread_polls_again_after_interval() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(handler), interval_s=0.02)
    poller.start()
    try:
        deadline = time.monotonic() + 2.0
        while calls["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls["n"] >= 2
    finally:
        poller.stop()


# -- coercion + resolve_value helpers ----------------------------------------


def test_coerce_float():
    assert coerce_float("2.5", 1.0) == 2.5
    assert coerce_float(None, 1.0) == 1.0
    assert coerce_float("not-a-number", 1.0) == 1.0


def test_coerce_int():
    assert coerce_int("5", 1) == 5
    assert coerce_int("5.0", 1) == 5  # UI-typed float still resolves
    assert coerce_int(None, 1) == 1
    assert coerce_int("nope", 1) == 1


def test_resolve_value_explicit_wins():
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(_rows_handler([{"key": "A", "value": "9"}])))
    poller.poll_once()
    assert resolve_value(5.0, poller, "A", 1.0, coerce_float) == 5.0


def test_resolve_value_falls_back_to_poller_then_default():
    poller = TablePoller(URL, "key", table="app_config", keys=("A",),
                         client=_client(_rows_handler([{"key": "A", "value": "9"}])))
    poller.poll_once()
    assert resolve_value(None, poller, "A", 1.0, coerce_float) == 9.0
    assert resolve_value(None, poller, "MISSING", 1.0, coerce_float) == 1.0
    assert resolve_value(None, None, "A", 1.0, coerce_float) == 1.0


def test_resolve_value_no_coerce_returns_raw_string():
    poller = TablePoller(URL, "key", table="app_config", keys=("HOST",),
                         client=_client(_rows_handler([{"key": "HOST", "value": "broker.local"}])))
    poller.poll_once()
    assert resolve_value(None, poller, "HOST", "localhost") == "broker.local"
