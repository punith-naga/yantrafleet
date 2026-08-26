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
