"""SettingsSync: table override wins, env is the fallback forever.

Same coverage as copilot's test_settings_sync.py, minus os.environ
mirroring (not applicable to notifier — see settings_sync.py docstring),
plus maybe_poll() throttling with an injectable clock (mirrors
reliability.py's SystemClock/fake-clock pattern already used in
test_reliability.py).
"""
from __future__ import annotations

import httpx
import pytest

from yantranotify.settings_sync import SETTINGS_KEYS, SettingsSync

URL = "https://example.test"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _rows_handler(rows: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/app_settings"
        return httpx.Response(200, json=rows)

    return handler


# -- construction never touches the network ----------------------------------


def test_construction_does_not_touch_network() -> None:
    sync = SettingsSync(URL, "key")
    assert sync._overrides == {}
    sync.close()


# -- fallback to env var when no row exists (the load-bearing case) ----------


def test_get_falls_back_to_env_when_no_row_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core guarantee: an existing deployment that never touches the
    settings panel — including a project that hasn't even run
    0008_app_settings.sql — must behave exactly as it does today."""
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/x")
    sync = SettingsSync(URL, "key", client=_client(_rows_handler([])))
    sync.poll_once()
    assert sync.get("WEBHOOK_URL") == "https://hooks.example.test/x"
    assert sync.get("TWILIO_SID") is None


def test_get_returns_none_when_neither_row_nor_env_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    sync = SettingsSync(URL, "key", client=_client(_rows_handler([])))
    sync.poll_once()
    assert sync.get("WEBHOOK_URL") is None


# -- table override wins over env --------------------------------------------


def test_table_override_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/bootstrap")
    rows = [{"key": "WEBHOOK_URL", "value": "https://hooks.example.test/rotated"}]
    sync = SettingsSync(URL, "key", client=_client(_rows_handler(rows)))
    sync.poll_once()
    assert sync.get("WEBHOOK_URL") == "https://hooks.example.test/rotated"


def test_clearing_the_row_reverts_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_SID", "AC-bootstrap")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=[{"key": "TWILIO_SID", "value": "AC-rotated"}]
            )
        return httpx.Response(200, json=[])

    sync = SettingsSync(URL, "key", client=_client(handler))
    sync.poll_once()
    assert sync.get("TWILIO_SID") == "AC-rotated"
    sync.poll_once()
    assert sync.get("TWILIO_SID") == "AC-bootstrap"


# -- unreachable / 404 / malformed -> degrade quietly, never raise ----------


def test_unreachable_backend_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/bootstrap")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    sync = SettingsSync(URL, "key", client=_client(boom))
    sync.poll_once()  # must not raise
    assert sync.get("WEBHOOK_URL") == "https://hooks.example.test/bootstrap"


def test_404_before_0008_is_applied_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/bootstrap")

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "relation does not exist"})

    sync = SettingsSync(URL, "key", client=_client(not_found))
    sync.poll_once()
    assert sync.get("WEBHOOK_URL") == "https://hooks.example.test/bootstrap"


def test_malformed_payload_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/bootstrap")

    def weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    sync = SettingsSync(URL, "key", client=_client(weird))
    sync.poll_once()
    assert sync.get("WEBHOOK_URL") == "https://hooks.example.test/bootstrap"


def test_poll_failure_leaves_previous_overrides_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=[{"key": "TWILIO_SID", "value": "AC-rotated"}]
            )
        raise httpx.ConnectError("blip", request=request)

    sync = SettingsSync(URL, "key", client=_client(handler))
    sync.poll_once()
    assert sync.get("TWILIO_SID") == "AC-rotated"
    sync.poll_once()
    assert sync.get("TWILIO_SID") == "AC-rotated"


# -- maybe_poll() throttling with an injectable clock ------------------------


def test_maybe_poll_first_call_always_polls() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    clock = FakeClock()
    sync = SettingsSync(URL, "key", client=_client(handler), interval_s=30.0,
                        clock=clock)
    sync.maybe_poll()
    assert calls["n"] == 1


def test_maybe_poll_skips_before_interval_elapses() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    clock = FakeClock()
    sync = SettingsSync(URL, "key", client=_client(handler), interval_s=30.0,
                        clock=clock)
    sync.maybe_poll()
    assert calls["n"] == 1
    clock.advance(10.0)
    sync.maybe_poll()  # too soon
    assert calls["n"] == 1


def test_maybe_poll_polls_again_once_interval_elapses() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    clock = FakeClock()
    sync = SettingsSync(URL, "key", client=_client(handler), interval_s=30.0,
                        clock=clock)
    sync.maybe_poll()
    clock.advance(30.0)
    sync.maybe_poll()
    assert calls["n"] == 2


def test_default_keys_match_the_six_notifier_settings() -> None:
    assert SETTINGS_KEYS == (
        "WEBHOOK_URL", "YANTRA_WEBHOOK_SECRET",
        "TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_TO",
    )
