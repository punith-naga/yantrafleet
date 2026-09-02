"""API-level offline tests: /ask degrades to tier 3, CORS, /health, and the
service ladder when the data backend is down. No network, no LLM keys."""
from __future__ import annotations

from fastapi.testclient import TestClient

from sarathi.app import create_app
from sarathi.config import Settings
from sarathi.llm import ground_check
from sarathi.tools import ToolResult
from sarathi.transport import FailingTransport, StaticTransport


def _settings(model: str | None = None) -> Settings:
    return Settings(supabase_url="https://x.invalid", supabase_key="k", model=model)


def test_ask_offline_tier(transport: StaticTransport) -> None:
    client = TestClient(create_app(transport=transport, settings=_settings()))
    resp = client.post("/ask", json={"question": "Which robots are low on battery?"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "answer", "evidence", "tier", "grounding", "meta", "latency_ms",
    }
    assert body["tier"] == "offline"
    assert body["grounding"] == "computed"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert body["evidence"] and all(set(e) == {"label", "ref"} for e in body["evidence"])
    assert "R-004" in body["answer"]


def test_cors_allows_null_origin(transport: StaticTransport) -> None:
    """file:// consoles send Origin: null — the API must still be readable."""
    client = TestClient(create_app(transport=transport, settings=_settings()))
    resp = client.post(
        "/ask", json={"question": "fleet status"}, headers={"Origin": "null"}
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
    pre = client.options(
        "/ask",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert pre.status_code == 200


def test_ask_backend_down_still_200() -> None:
    """Data backend down + no LLM: honest no-data answer, still HTTP 200."""
    client = TestClient(create_app(transport=FailingTransport(), settings=_settings()))
    resp = client.post("/ask", json={"question": "fleet status?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "offline"
    assert body["evidence"] == []
    assert "unavailable" in body["answer"].lower()


def test_health_reports_best_tier(transport: StaticTransport) -> None:
    client = TestClient(create_app(transport=transport, settings=_settings()))
    h = client.get("/health").json()
    assert h["ok"] is True
    assert h["llm_configured"] is False
    assert h["data_backend_ok"] is True
    assert h["best_tier"] == "offline"


def test_health_new_shape_offline_only(transport: StaticTransport) -> None:
    """v0.5 health contract: tiers_available / model / transport_ok."""
    client = TestClient(create_app(transport=transport, settings=_settings()))
    h = client.get("/health").json()
    assert h["tiers_available"] == ["offline"]
    assert h["model"] == "none"
    assert h["transport_ok"] is True


def test_health_all_tiers_when_llm_configured(transport: StaticTransport) -> None:
    client = TestClient(
        create_app(transport=transport, settings=_settings(model="fake/model"))
    )
    h = client.get("/health").json()
    assert h["tiers_available"] == ["grounded", "llm_only", "offline"]
    assert h["model"] == "fake/model"
    assert h["transport_ok"] is True
    assert h["best_tier"] == "grounded"


def test_health_transport_down_never_crashes() -> None:
    client = TestClient(
        create_app(transport=FailingTransport(), settings=_settings(model="fake/model"))
    )
    resp = client.get("/health")
    assert resp.status_code == 200
    h = resp.json()
    assert h["transport_ok"] is False
    assert h["tiers_available"] == ["llm_only", "offline"]
    assert h["best_tier"] == "llm_only"


def test_health_survives_non_transport_errors() -> None:
    """A misbehaving transport raising a foreign exception must not 500."""

    class ExplodingTransport(FailingTransport):
        def get(self, table, params):  # type: ignore[override]
            raise ValueError("totally unexpected")

    client = TestClient(
        create_app(transport=ExplodingTransport(), settings=_settings())
    )
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["transport_ok"] is False


def test_ground_check_flags_invented_numbers() -> None:
    """The tier-1 output rail: numbers absent from tool data are flagged."""
    tr = ToolResult(
        tool="query_robots", args={}, data={"rows": [{"id": "R-004", "battery": 8}],
                                            "count": 1},
        source_id="query_robots:aa:2026-08-26T10:00:00Z", ts="t",
    )
    assert ground_check("R-004 is at 8%", [tr]) == []          # grounded
    assert ground_check("R-004 is at 12%", [tr]) == [12.0]     # invented
