"""Tier-1/2 agent-loop tests with a scripted fake LLM (no litellm calls).

The seam is ``completion_fn`` on tier1_answer/tier2_answer, threaded through
CopilotService and create_app. The fakes below mimic litellm's response shape
(resp.choices[0].message with .content / .tool_calls) closely enough for the
loop, without importing litellm at all.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sarathi.app import create_app
from sarathi.config import Settings
from sarathi.llm import LLMError, tier1_answer, tier2_answer
from sarathi.service import CopilotService
from sarathi.tools import Toolbox
from sarathi.transport import StaticTransport

from fastapi.testclient import TestClient


# -- fake litellm plumbing ---------------------------------------------------

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
    """completion_fn returning canned responses in order, recording calls."""

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


def _settings(model: str | None = "fake/model") -> Settings:
    return Settings(supabase_url="https://x.invalid", supabase_key="k", model=model)


# Fixture-derived numbers (see conftest.FLEET_FIXTURE): 6 robots, batteries
# 78/21/55/8/91/14 -> avg 44.5; R-004 faulted at 8%; throughput 41.5.
SUMMARY_ANSWER = (
    "The fleet has 6 robots with an average battery of 44.5%. "
    "R-004 is faulted at 8% battery; throughput is 41.5 tasks/hr."
)


def _summary_script() -> ScriptedLLM:
    return ScriptedLLM([
        _resp(_msg(tool_calls=[_tool_call("call_1", "get_fleet_summary")])),
        _resp(_msg(content=SUMMARY_ANSWER)),
    ])


# -- (a) happy path: tool call, then grounded final answer -------------------

def test_tier1_agent_loop_grounded(transport: StaticTransport) -> None:
    fake = _summary_script()
    service = CopilotService(_settings(), transport, completion_fn=fake)
    result = service.ask("How is the fleet doing?")
    assert result.tier == "grounded"
    assert result.answer == SUMMARY_ANSWER
    assert result.evidence, "tier-1 answer must carry evidence"
    assert any("get_fleet_summary" in e["label"] for e in result.evidence)
    assert result.grounding == "verified"
    assert result.meta == {}
    # Exactly the scripted two calls: tool round + final answer.
    assert len(fake.calls) == 2
    # The seam passed model + tools through unchanged.
    assert fake.calls[0]["model"] == "fake/model"
    assert any(
        t["function"]["name"] == "get_fleet_summary" for t in fake.calls[0]["tools"]
    )


def test_tier1_via_http_ask(transport: StaticTransport) -> None:
    client = TestClient(
        create_app(
            transport=transport, settings=_settings(), completion_fn=_summary_script()
        )
    )
    body = client.post("/ask", json={"question": "fleet status?"}).json()
    assert body["tier"] == "grounded"
    assert body["grounding"] == "verified"
    assert body["evidence"]
    assert "44.5" in body["answer"]


# -- (b) fake raising: degrade to tier 3, never crash ------------------------

def test_tier1_llm_failure_degrades_to_offline(transport: StaticTransport) -> None:
    fake = ScriptedLLM([RuntimeError("provider exploded")])
    service = CopilotService(_settings(), transport, completion_fn=fake)
    result = service.ask("Which robots are low on battery?")
    assert result.tier == "offline"
    assert result.grounding == "computed"
    assert "R-004" in result.answer  # real tier-3 answer, not an error page
    assert result.evidence


def test_tier1_failure_via_http_never_500(transport: StaticTransport) -> None:
    fake = ScriptedLLM([RuntimeError("boom")])
    client = TestClient(
        create_app(transport=transport, settings=_settings(), completion_fn=fake)
    )
    resp = client.post("/ask", json={"question": "any critical alerts?"})
    assert resp.status_code == 200
    assert resp.json()["tier"] == "offline"


def test_tier1_failure_midloop_after_tool_round(transport: StaticTransport) -> None:
    """A failure after a successful tool round still degrades cleanly."""
    fake = ScriptedLLM([
        _resp(_msg(tool_calls=[_tool_call("c1", "get_fleet_summary")])),
        RuntimeError("second call died"),
    ])
    service = CopilotService(_settings(), transport, completion_fn=fake)
    result = service.ask("fleet status")
    assert result.tier == "offline"


# -- (c) tool-call loop is bounded -------------------------------------------

def test_tier1_loop_bounded(transport: StaticTransport) -> None:
    """A model that always requests tools hits max_turns, raising LLMError."""

    class GreedyLLM:
        calls = 0

        def __call__(self, **kwargs):
            GreedyLLM.calls += 1
            return _resp(
                _msg(tool_calls=[_tool_call(f"c{GreedyLLM.calls}", "get_fleet_summary")])
            )

    toolbox = Toolbox(transport=transport)
    with pytest.raises(LLMError, match="exceeded"):
        tier1_answer(
            "loop forever", toolbox, model="fake/model",
            max_turns=4, completion_fn=GreedyLLM(),
        )
    assert GreedyLLM.calls == 4  # exactly max_turns completions, then stop


def test_tier1_loop_bounded_service_degrades(transport: StaticTransport) -> None:
    fake = ScriptedLLM(
        [_resp(_msg(tool_calls=[_tool_call(f"c{i}", "get_fleet_summary")]))
         for i in range(20)]
    )
    service = CopilotService(_settings(), transport, completion_fn=fake)
    result = service.ask("fleet status")
    assert result.tier == "offline"  # bounded loop -> LLMError -> tier 3
    assert len(fake.calls) == Settings.max_agent_turns


# -- misc seam behaviour ------------------------------------------------------

def test_tier1_unknown_tool_name_is_survivable(transport: StaticTransport) -> None:
    """An hallucinated tool name gets an error payload, not a crash."""
    fake = ScriptedLLM([
        _resp(_msg(tool_calls=[_tool_call("c1", "launch_missiles")])),
        _resp(_msg(content="That tool is not available.")),
    ])
    toolbox = Toolbox(transport=transport)
    answer, tool_log = tier1_answer(
        "do something weird", toolbox, model="fake/model", completion_fn=fake
    )
    assert "not available" in answer
    assert tool_log == []  # unknown tool never executed
    # The error payload was fed back as the tool message.
    tool_msgs = [m for m in fake.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and "unknown tool" in tool_msgs[0]["content"]


def test_tier1_empty_answer_raises(transport: StaticTransport) -> None:
    fake = ScriptedLLM([_resp(_msg(content="   "))])
    with pytest.raises(LLMError, match="empty"):
        tier1_answer(
            "q", Toolbox(transport=transport), model="m", completion_fn=fake
        )


def test_tier2_seam_and_failure() -> None:
    ok = ScriptedLLM([_resp(_msg(content="Conceptually, check charger uptime."))])
    text = tier2_answer("what should I check?", model="m", completion_fn=ok)
    assert text.startswith("[lower confidence")
    assert "charger uptime" in text

    bad = ScriptedLLM([ConnectionError("no route")])
    with pytest.raises(LLMError):
        tier2_answer("q", model="m", completion_fn=bad)
