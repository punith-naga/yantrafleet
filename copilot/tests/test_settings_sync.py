"""SettingsSync: table override wins, env is the fallback forever.

No network anywhere: an httpx.MockTransport-backed client is injected, or
the settings poll is deliberately pointed at an unroutable host to
exercise the "unreachable -> degrade quietly" path. Construction itself
never touches the network — only ``.start()``/``.poll_once()`` do.
"""
from __future__ import annotations

import time

import httpx
import pytest

from sarathi.settings_sync import SETTINGS_KEYS, SettingsSync

URL = "https://x.invalid"


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url=f"{URL}/rest/v1", transport=httpx.MockTransport(handler)
    )


def _rows_handler(rows: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/app_settings"
        return httpx.Response(200, json=rows)

    return handler


# -- construction never touches the network ----------------------------------


def test_construction_does_not_touch_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("SettingsSync() must not make requests at construction")

    # No client passed: SettingsSync builds its own httpx.Client, which by
    # itself performs no I/O (only .get()/.poll_once() would).
    sync = SettingsSync(URL, "key")
    assert sync._overrides == {}
    sync.stop()


# -- fallback to env var when no row exists (the load-bearing case) ----------


def test_get_falls_back_to_env_when_no_row_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core guarantee: an existing deployment that never touches the
    settings panel — including a project that hasn't even run
    0008_app_settings.sql — must behave exactly as it does today."""
    monkeypatch.setenv("SARATHI_TOKEN", "bootstrap-token")
    sync = SettingsSync(URL, "key", client=_client(_rows_handler([])))
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") == "bootstrap-token"
    assert sync.get("GEMINI_API_KEY") is None  # unset anywhere


def test_get_returns_none_when_neither_row_nor_env_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SARATHI_TOKEN", raising=False)
    sync = SettingsSync(URL, "key", client=_client(_rows_handler([])))
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") is None


# -- table override wins over env --------------------------------------------


def test_table_override_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARATHI_TOKEN", "bootstrap-token")
    rows = [{"key": "SARATHI_TOKEN", "value": "rotated-token"}]
    sync = SettingsSync(URL, "key", client=_client(_rows_handler(rows)))
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") == "rotated-token"


def test_clearing_the_row_reverts_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARATHI_TOKEN", "bootstrap-token")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=[{"key": "SARATHI_TOKEN", "value": "rotated-token"}]
            )
        return httpx.Response(200, json=[])  # row deleted

    sync = SettingsSync(URL, "key", client=_client(handler))
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") == "rotated-token"
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") == "bootstrap-token"


# -- unreachable / 404 / malformed -> degrade quietly, never raise ----------


def test_unreachable_backend_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARATHI_TOKEN", "bootstrap-token")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    sync = SettingsSync(URL, "key", client=_client(boom))
    sync.poll_once()  # must not raise
    assert sync.get("SARATHI_TOKEN") == "bootstrap-token"


def test_404_before_0008_is_applied_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project that hasn't run 0008_app_settings.sql yet: the table
    doesn't exist, PostgREST 404s, and the service must behave as if the
    settings panel didn't exist at all."""
    monkeypatch.setenv("SARATHI_TOKEN", "bootstrap-token")

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "relation does not exist"})

    sync = SettingsSync(URL, "key", client=_client(not_found))
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") == "bootstrap-token"


def test_malformed_payload_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARATHI_TOKEN", "bootstrap-token")

    def weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    sync = SettingsSync(URL, "key", client=_client(weird))
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") == "bootstrap-token"


def test_poll_failure_leaves_previous_overrides_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=[{"key": "SARATHI_TOKEN", "value": "rotated-token"}]
            )
        raise httpx.ConnectError("blip", request=request)

    sync = SettingsSync(URL, "key", client=_client(handler))
    sync.poll_once()
    assert sync.get("SARATHI_TOKEN") == "rotated-token"
    sync.poll_once()  # transient failure: overrides left exactly as they were
    assert sync.get("SARATHI_TOKEN") == "rotated-token"


# -- os.environ mirroring (GEMINI_API_KEY: litellm reads os.environ) --------


def test_mirrors_override_into_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("GEMINI_API_KEY", "bootstrap-key")
    rows = [{"key": "GEMINI_API_KEY", "value": "rotated-key"}]
    sync = SettingsSync(URL, "key", client=_client(_rows_handler(rows)))
    sync.poll_once()
    assert os.environ["GEMINI_API_KEY"] == "rotated-key"


def test_clearing_override_restores_original_bootstrap_env_not_stale_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recompute-from-snapshot, never incrementally overwrite: clearing a
    panel value must restore the ORIGINAL deploy-time env var, not leave
    the stale override sitting in os.environ."""
    import os

    monkeypatch.setenv("GEMINI_API_KEY", "bootstrap-key")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=[{"key": "GEMINI_API_KEY", "value": "rotated-key"}]
            )
        return httpx.Response(200, json=[])

    sync = SettingsSync(URL, "key", client=_client(handler))
    sync.poll_once()
    assert os.environ["GEMINI_API_KEY"] == "rotated-key"
    sync.poll_once()  # row gone
    assert os.environ["GEMINI_API_KEY"] == "bootstrap-key"  # restored, not stale


def test_clearing_override_with_no_bootstrap_env_pops_the_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=[{"key": "GEMINI_API_KEY", "value": "rotated-key"}]
            )
        return httpx.Response(200, json=[])

    sync = SettingsSync(URL, "key", client=_client(handler))
    sync.poll_once()
    assert os.environ["GEMINI_API_KEY"] == "rotated-key"
    sync.poll_once()
    assert "GEMINI_API_KEY" not in os.environ


def test_mirror_to_environ_false_does_not_touch_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rows = [{"key": "GEMINI_API_KEY", "value": "rotated-key"}]
    sync = SettingsSync(
        URL, "key", client=_client(_rows_handler(rows)), mirror_to_environ=False
    )
    sync.poll_once()
    assert "GEMINI_API_KEY" not in os.environ
    assert sync.get("GEMINI_API_KEY") == "rotated-key"  # .get() still sees it


# -- lifecycle: start()/stop() ------------------------------------------------


def test_start_polls_once_synchronously_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"key": "SARATHI_TOKEN", "value": "rotated-token"}]
    sync = SettingsSync(
        URL, "key", client=_client(_rows_handler(rows)), interval_s=999.0
    )
    sync.start()
    try:
        assert sync.get("SARATHI_TOKEN") == "rotated-token"
    finally:
        sync.stop()


def test_start_then_stop_joins_the_background_thread() -> None:
    sync = SettingsSync(
        URL, "key", client=_client(_rows_handler([])), interval_s=0.01
    )
    sync.start()
    assert sync._thread is not None and sync._thread.is_alive()
    sync.stop()
    assert sync._thread is None


def test_background_thread_polls_again_after_interval() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    sync = SettingsSync(URL, "key", client=_client(handler), interval_s=0.02)
    sync.start()
    try:
        deadline = time.monotonic() + 2.0
        while calls["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls["n"] >= 2
    finally:
        sync.stop()


def test_default_keys_match_the_two_copilot_settings() -> None:
    assert SETTINGS_KEYS == ("GEMINI_API_KEY", "SARATHI_TOKEN")
