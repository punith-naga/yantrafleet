"""Live low-battery threshold: source it from public.app_config (v0.18),
refreshed on every answer — same "live seam" shape as GEMINI_API_KEY
already has via settings_sync (see test_settings_sync_auth.py).

The fleet fixture (tests/conftest.py) has batteries at 78/21/55/8/91/14.
At the hardcoded default (20%) exactly R-004 (8%) and R-006 (14%) are
"low"; raising the threshold to 25 (via a live config_sync override)
also pulls in R-002 (21%) — the fact this changes WITHOUT recreating the
engine/service/app is the behaviour under test.
"""
from __future__ import annotations

from sarathi.app import create_app
from sarathi.config import Settings
from sarathi.offline import OfflineEngine
from sarathi.service import CopilotService
from sarathi.tools import Toolbox
from sarathi.transport import StaticTransport
from fastapi.testclient import TestClient

QUESTION = {"question": "Which robots are low on battery?"}


class FakeConfigSync:
    """Same tiny shape as FakeSettingsSync (test_settings_sync_auth.py):
    ``.get()`` return value can be flipped mid-test, standing in for a
    real TablePoller picking up an app_config row change."""

    def __init__(self, overrides: dict[str, str | None] | None = None) -> None:
        self.overrides: dict[str, str | None] = dict(overrides or {})

    def get(self, key: str) -> str | None:
        return self.overrides.get(key)


def _settings() -> Settings:
    return Settings(supabase_url="https://x.invalid", supabase_key="k", model=None)


# -- OfflineEngine: the low-level seam ---------------------------------------


def test_engine_uses_default_when_no_fn_given(transport: StaticTransport) -> None:
    engine = OfflineEngine(lambda: Toolbox(transport=transport))
    ans = engine.answer("battery situation?")
    assert "2 robot(s) below 20%" in ans.answer


def test_engine_reads_threshold_live_on_every_answer(transport: StaticTransport) -> None:
    box = {"value": 20.0}
    engine = OfflineEngine(
        lambda: Toolbox(transport=transport),
        low_battery_threshold_fn=lambda: box["value"],
    )
    assert "2 robot(s) below 20%" in engine.answer("battery situation?").answer
    box["value"] = 25.0  # no recreation — just flip the live source
    assert "3 robot(s) below 25%" in engine.answer("battery situation?").answer


def test_engine_falls_back_to_default_on_unparseable_live_value(
    transport: StaticTransport,
) -> None:
    engine = OfflineEngine(
        lambda: Toolbox(transport=transport),
        low_battery_threshold=20.0,
        low_battery_threshold_fn=lambda: "not-a-number",
    )
    assert "2 robot(s) below 20%" in engine.answer("battery situation?").answer


# -- CopilotService: config_sync wiring --------------------------------------


def test_service_reads_config_sync_live(transport: StaticTransport) -> None:
    config = FakeConfigSync()
    service = CopilotService(_settings(), transport, config_sync=config)
    assert "2 robot(s) below 20%" in service.ask("battery situation?").answer

    config.overrides["SARATHI_LOW_BATTERY_THRESHOLD"] = "25"
    assert "3 robot(s) below 25%" in service.ask("battery situation?").answer

    config.overrides["SARATHI_LOW_BATTERY_THRESHOLD"] = ""  # cleared -> revert
    assert "2 robot(s) below 20%" in service.ask("battery situation?").answer


def test_service_with_no_config_sync_uses_settings_default(
    transport: StaticTransport,
) -> None:
    service = CopilotService(_settings(), transport, config_sync=None)
    assert "2 robot(s) below 20%" in service.ask("battery situation?").answer


# -- full app: one instance, threshold changes mid-lifetime ------------------


def test_app_battery_threshold_flips_live_without_recreating_the_app(
    transport: StaticTransport,
) -> None:
    config = FakeConfigSync()
    client = TestClient(
        create_app(transport=transport, settings=_settings(), config_sync=config)
    )
    resp = client.post("/ask", json=QUESTION)
    assert resp.status_code == 200
    assert "2 robot(s) below 20%" in resp.json()["answer"]

    config.overrides["SARATHI_LOW_BATTERY_THRESHOLD"] = "25"
    resp = client.post("/ask", json=QUESTION)
    assert "3 robot(s) below 25%" in resp.json()["answer"]


def test_create_app_default_config_sync_is_inert(transport: StaticTransport) -> None:
    """create_app() with no config_sync builds a real-but-never-started
    TablePoller — mirrors settings_sync's existing contract exactly."""
    app = create_app(transport=transport, settings=_settings())
    assert app.state.config_sync.get("SARATHI_LOW_BATTERY_THRESHOLD") is None
    resp = TestClient(app).post("/ask", json=QUESTION)
    assert "2 robot(s) below 20%" in resp.json()["answer"]
