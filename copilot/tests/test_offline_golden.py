"""Golden eval: run tier 3 (offline, no LLM) against the mocked fleet data.

Three deterministic evaluator layers (Palantir-AIP style):
  a) intent + trajectory — the right tools were called;
  b) content — expected facts appear in the answer;
  c) grounding — every numeric token in the answer exists in the evidence
     (tool result data), and every evidence ref is a real source_id.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sarathi.offline import OfflineEngine, classify_intent
from sarathi.tools import Toolbox
from sarathi.transport import StaticTransport

GOLDEN = json.loads(
    (Path(__file__).resolve().parents[1] / "eval" / "golden.json").read_text()
)["cases"]

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _engine(transport: StaticTransport) -> OfflineEngine:
    return OfflineEngine(lambda: Toolbox(transport=transport))


@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_golden_case(case: dict, transport: StaticTransport) -> None:
    result = _engine(transport).answer(case["question"])

    # (a) intent + tool trajectory
    assert result.intent == case["expected_intent"]
    called = [t.tool for t in result.tool_log]
    for tool in case["expected_tools"]:
        assert tool in called, f"expected tool {tool}, got {called}"

    # (b) expected facts present (case-insensitive)
    low = result.answer.lower()
    for frag in case["expected_contains"]:
        assert frag.lower() in low, f"missing {frag!r} in: {result.answer}"

    # (c1) evidence present and refs match real source_ids from this run
    assert result.evidence, "tier-3 answer must carry evidence"
    real_refs = {t.source_id for t in result.tool_log}
    for ev in result.evidence:
        assert set(ev) == {"label", "ref"}
        assert ev["ref"] in real_refs, "fabricated citation"

    # (c2) grounding: every number in the answer appears in tool data
    corpus = json.dumps([t.data for t in result.tool_log], default=str)
    available = {float(m) for m in _NUM_RE.findall(corpus)}
    for tok in _NUM_RE.findall(result.answer):
        assert float(tok) in available, (
            f"ungrounded number {tok!r} in answer: {result.answer}"
        )


def test_intent_classifier_edges() -> None:
    assert classify_intent("is AGV-001 ok?")[0] == "robot_detail"
    assert classify_intent("any open incidents?")[0] == "incidents"
    assert classify_intent("which robots are charging")[0] == "charging"
    assert classify_intent("battery situation?")[0] == "low_battery"
    assert classify_intent("hello there")[0] == "fleet_summary"


def test_intent_classifier_history() -> None:
    # history keywords beat the plain robot-id shortcut
    intent, slots = classify_intent("show the battery trend for R-004")
    assert intent == "history" and slots["robot_id"] == "R-004"
    intent, slots = classify_intent("R-004 telemetry over the last 15 minutes")
    assert intent == "history"
    assert slots == {"robot_id": "R-004", "minutes": 15}
    # history keywords without a robot id still classify as history
    intent, slots = classify_intent("how has the fleet behaved over time?")
    assert intent == "history" and slots["robot_id"] is None
    # a plain id question stays robot_detail
    assert classify_intent("what's up with R-004?")[0] == "robot_detail"


def test_offline_history_intent_grounded(transport):
    ans = _engine(transport).answer(
        "show me the telemetry history for R-004 over the last 30 minutes"
    )
    assert ans.intent == "history"
    # grounded numbers from the fixture decline: 30% -> 8%, max temp 88C
    for frag in ("R-004", "12 samples", "30% -> 8%", "88C",
                 "2 status change(s)", "fault"):
        assert frag in ans.answer, f"missing {frag!r} in: {ans.answer}"
    # evidence cites query_telemetry with real refs
    assert any("query_telemetry" in e["label"] for e in ans.evidence)
    real_refs = {t.source_id for t in ans.tool_log}
    assert all(e["ref"] in real_refs for e in ans.evidence)
    # every number in the answer exists in the tool data (grounding)
    corpus = json.dumps([t.data for t in ans.tool_log], default=str)
    available = {float(m) for m in _NUM_RE.findall(corpus)}
    for tok in _NUM_RE.findall(ans.answer):
        assert float(tok) in available, f"ungrounded {tok!r}: {ans.answer}"


def test_offline_history_no_robot_falls_back_to_summary(transport):
    ans = _engine(transport).answer("what's the trend across the fleet?")
    assert ans.intent == "history"
    assert "Fleet summary: 6 robots" in ans.answer
    assert any("get_fleet_summary" in e["label"] for e in ans.evidence)


def test_offline_history_unknown_robot(transport):
    ans = _engine(transport).answer("telemetry history for R-999")
    assert ans.intent == "history"
    assert "No telemetry samples for R-999" in ans.answer
    assert any("query_telemetry" in e["label"] for e in ans.evidence)


def test_offline_approvals_intent(transport):
    ans = _engine(transport).answer("what is waiting for approval?")
    assert "awaiting approval" in ans.answer
    assert "estop R-004" in ans.answer
    assert any("query_commands" in e["label"] for e in ans.evidence)
