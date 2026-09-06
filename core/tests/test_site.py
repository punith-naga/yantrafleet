"""Site identity (yantracore.site) — env-driven, defaulted, exported."""
import httpx
import pytest
import yantracore
from yantracore import DEFAULT_SITE_ID, SITE_ID, site_id
from yantracore.site import start_site_sync, stop_site_sync

URL = "https://x.invalid"


@pytest.fixture(autouse=True)
def _clean_site_sync():
    """Every start_site_sync() test gets a fresh module-level poller, and
    no test leaks a background thread into the next one."""
    stop_site_sync()
    yield
    stop_site_sync()


def test_default_site_id(monkeypatch):
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    assert site_id() == "BLR-DC1" == DEFAULT_SITE_ID


def test_env_override(monkeypatch):
    monkeypatch.setenv("YANTRA_SITE_ID", "PNQ-DC2")
    assert site_id() == "PNQ-DC2"


def test_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("YANTRA_SITE_ID", "   ")
    assert site_id() == DEFAULT_SITE_ID


def test_module_snapshot_and_exports():
    assert isinstance(SITE_ID, str) and SITE_ID
    for name in ("DEFAULT_SITE_ID", "SITE_ID", "site_id"):
        assert name in yantracore.__all__


# -- v0.18: live app_config override via start_site_sync() ------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_start_site_sync_overrides_env(monkeypatch):
    monkeypatch.setenv("YANTRA_SITE_ID", "bootstrap-site")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/app_config"
        return httpx.Response(200, json=[{"key": "YANTRA_SITE_ID", "value": "PNQ-WH7"}])

    start_site_sync(URL, "key", client=_client(handler))
    assert site_id() == "PNQ-WH7"


def test_start_site_sync_is_idempotent(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    p1 = start_site_sync(URL, "key", client=_client(handler))
    p2 = start_site_sync(URL, "key", client=_client(handler))
    assert p1 is p2


def test_poll_failure_falls_back_to_env(monkeypatch):
    """No app_config table yet (e.g. loopback/demo mode, or migration
    0018 not applied) -> degrade quietly, keep the env/default behavior."""
    monkeypatch.setenv("YANTRA_SITE_ID", "bootstrap-site")

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "relation does not exist"})

    start_site_sync(URL, "key", client=_client(not_found))
    assert site_id() == "bootstrap-site"


def test_stop_site_sync_clears_override(monkeypatch):
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"key": "YANTRA_SITE_ID", "value": "PNQ-WH7"}])

    start_site_sync(URL, "key", client=_client(handler))
    assert site_id() == "PNQ-WH7"
    stop_site_sync()
    assert site_id() == DEFAULT_SITE_ID
