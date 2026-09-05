"""Live behaviour the settings panel actually buys: one app/service
instance whose SARATHI_TOKEN / GEMINI_API_KEY can change mid-lifetime,
with no ``create_app``/``CopilotService`` recreation.

``test_auth.py`` covers the token being fixed for the app's lifetime
(env set once, at app creation) — this file covers the new thing: a
*single* app/service picking up a change between two requests/calls, via
an injected fake ``SettingsSync``.
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from sarathi.app import create_app
from sarathi.config import Settings
from sarathi.service import CopilotService
from sarathi.transport import StaticTransport

QUESTION = {"question": "Which robots are low on battery?"}


class FakeSettingsSync:
    """``.get()`` return value can be flipped mid-test by mutating
    ``overrides`` — stands in for a real poll picking up a row change."""

    def __init__(self, overrides: dict[str, str | None] | None = None) -> None:
        self.overrides: dict[str, str | None] = dict(overrides or {})

    def get(self, key: str) -> str | None:
        return self.overrides.get(key)


def _settings(model: str | None = None) -> Settings:
    return Settings(supabase_url="https://x.invalid", supabase_key="k", model=model)


# -- SARATHI_TOKEN: one app, auth requirement flips between requests --------


def test_auth_requirement_flips_live_without_recreating_the_app() -> None:
    fake = FakeSettingsSync()
    transport = StaticTransport({"fleet_meta": [{"id": 1}]})
    client = TestClient(
        create_app(transport=transport, settings=_settings(), settings_sync=fake)
    )

    # No token configured yet: open.
    resp = client.post("/ask", json=QUESTION)
    assert resp.status_code == 200

    # Admin rotates SARATHI_TOKEN via the panel (simulated: the same fake
    # SettingsSync instance the app is already holding starts returning a
    # token) — the very next request enforces it, no recreation needed.
    fake.overrides["SARATHI_TOKEN"] = "s3cret"
    resp = client.post("/ask", json=QUESTION)
    assert resp.status_code == 401

    resp = client.post(
        "/ask", json=QUESTION, headers={"Authorization": "Bearer s3cret"}
    )
    assert resp.status_code == 200

    # Admin clears the panel value: back to open.
    fake.overrides.pop("SARATHI_TOKEN")
    resp = client.post("/ask", json=QUESTION)
    assert resp.status_code == 200


def test_health_auth_required_flips_live_too() -> None:
    fake = FakeSettingsSync()
    transport = StaticTransport({"fleet_meta": [{"id": 1}]})
    client = TestClient(
        create_app(transport=transport, settings=_settings(), settings_sync=fake)
    )
    assert client.get("/health").json()["auth_required"] is False
    fake.overrides["SARATHI_TOKEN"] = "s3cret"
    assert client.get("/health").json()["auth_required"] is True


# -- GEMINI_API_KEY: one CopilotService, tier selection flips between calls -


def _msg(content: str | None = None, tool_calls: list | None = None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _resp(message) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(call_id: str, name: str, arguments: str = "{}"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class ScriptedLLM:
    """completion_fn returning canned responses in order, recording calls
    (mirrors test_llm_fake.py's fake, kept local to avoid cross-test-file
    coupling)."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake LLM ran out of scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_llm_tier_turns_on_when_gemini_key_becomes_present(
    transport: StaticTransport,
) -> None:
    """No key at construction -> tier3 (offline). Admin sets GEMINI_API_KEY
    via the panel -> the very next .ask() on the SAME CopilotService
    attempts tier1, with no service/app recreation."""
    fake = FakeSettingsSync()  # nothing configured yet
    fake_llm = ScriptedLLM(
        [
            _resp(_msg(tool_calls=[_tool_call("c1", "get_fleet_summary")])),
            _resp(_msg(content="Fleet summary: all nominal.")),
        ]
    )
    service = CopilotService(
        _settings(model=None), transport, completion_fn=fake_llm, settings_sync=fake
    )

    assert service.llm_available is False
    first = service.ask("How is the fleet doing?")
    assert first.tier == "offline"
    assert fake_llm.calls == []  # tier1 never attempted — no key yet

    fake.overrides["GEMINI_API_KEY"] = "rotated-in-from-the-panel"
    assert service.llm_available is True
    second = service.ask("How is the fleet doing?")
    assert second.tier == "grounded"
    assert len(fake_llm.calls) == 2  # tier1 attempted exactly once a key exists
