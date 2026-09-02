"""Optional bearer auth on POST /ask, driven by env SARATHI_TOKEN.

Unset (the default, and what every other test runs under): the API is
fully open — unchanged demo behaviour. Set: /ask requires the exact
``Authorization: Bearer <token>`` header and answers 401 JSON otherwise;
/health stays open but reports ``auth_required``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sarathi.app import create_app
from sarathi.config import Settings
from sarathi.transport import StaticTransport

TOKEN = "s3cret-fleet-token"
QUESTION = {"question": "Which robots are low on battery?"}


def _settings() -> Settings:
    return Settings(supabase_url="https://x.invalid", supabase_key="k", model=None)


def _client(transport: StaticTransport) -> TestClient:
    return TestClient(create_app(transport=transport, settings=_settings()))


# -- SARATHI_TOKEN unset: everything stays open ---------------------------


def test_ask_open_when_token_unset(transport: StaticTransport) -> None:
    resp = _client(transport).post("/ask", json=QUESTION)
    assert resp.status_code == 200
    assert resp.json()["tier"] == "offline"


def test_health_reports_auth_not_required_when_unset(
    transport: StaticTransport,
) -> None:
    h = _client(transport).get("/health").json()
    assert h["auth_required"] is False


# -- SARATHI_TOKEN set ----------------------------------------------------


@pytest.fixture()
def auth_client(
    transport: StaticTransport, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv("SARATHI_TOKEN", TOKEN)
    return _client(transport)


def test_ask_correct_token_is_200(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/ask", json=QUESTION, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code == 200
    assert "R-004" in resp.json()["answer"]


def test_ask_wrong_token_is_401_json(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/ask", json=QUESTION, headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"] == "unauthorized"


def test_ask_absent_header_is_401_json(auth_client: TestClient) -> None:
    resp = auth_client.post("/ask", json=QUESTION)
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_ask_wrong_scheme_is_401(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/ask", json=QUESTION, headers={"Authorization": f"Basic {TOKEN}"}
    )
    assert resp.status_code == 401


def test_health_stays_open_but_flags_auth(auth_client: TestClient) -> None:
    resp = auth_client.get("/health")  # no Authorization header at all
    assert resp.status_code == 200
    h = resp.json()
    assert h["auth_required"] is True
    assert h["ok"] is True


def test_empty_token_env_means_open(
    transport: StaticTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SARATHI_TOKEN='' counts as unset — no accidental lock-out."""
    monkeypatch.setenv("SARATHI_TOKEN", "")
    resp = _client(transport).post("/ask", json=QUESTION)
    assert resp.status_code == 200
