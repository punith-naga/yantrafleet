"""Unit + API tests for the grounding guard (sarathi.grounding)."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from sarathi.app import create_app
from sarathi.config import Settings
from sarathi.grounding import (
    GROUNDING_UNVERIFIED,
    GROUNDING_VERIFIED,
    extract_numbers,
    verify_grounding,
)
from sarathi.tools import ToolResult
from sarathi.transport import StaticTransport


def _tr(data: dict) -> ToolResult:
    return ToolResult(
        tool="query_telemetry", args={}, data=data,
        source_id="query_telemetry:0123abcd:2026-08-26T10:00:00Z",
        ts="2026-08-26T10:00:00Z",
    )


# -- number extraction: skips timestamps / ids / refs ------------------------

def test_extract_skips_timestamps_ids_and_refs() -> None:
    text = (
        "As of 2026-08-26T10:14:00Z, R-004 (alert AL-1, incident INC-2) is "
        "at 8% battery; see query_robots:0123abcd:2026-08-26T10:00:00Z. "
        "Updated at 10:31."
    )
    assert extract_numbers(text) == ["8"]


def test_extract_plain_figures() -> None:
    assert extract_numbers("6 robots, avg 44.5%, throughput 41.5") == [
        "6", "44.5", "41.5",
    ]


# -- verification ------------------------------------------------------------

def test_verify_clean_pass() -> None:
    tr = _tr({"rows": [{"battery": 8}], "count": 1, "stats": {"battery_avg": 44.5}})
    status, offending = verify_grounding("R-004 is at 8%; fleet avg 44.5%.", [tr])
    assert status == GROUNDING_VERIFIED
    assert offending == []


def test_verify_dirty_catch() -> None:
    tr = _tr({"rows": [{"battery": 8}], "count": 1})
    status, offending = verify_grounding(
        "R-004 is at 12% and completed 99 tasks.", [tr]
    )
    assert status == GROUNDING_UNVERIFIED
    assert offending == ["12", "99"]


def test_verify_rounding_tolerance() -> None:
    """avg battery 54.16 in the payload supports '54.2' in the answer."""
    tr = _tr({"stats": {"battery_avg": 54.16}})
    status, offending = verify_grounding("Average battery is 54.2%.", [tr])
    assert status == GROUNDING_VERIFIED
    assert offending == []


def test_verify_int_float_formatting() -> None:
    tr = _tr({"stats": {"speed_avg": 1.0, "samples": 13}})
    status, _ = verify_grounding("13 samples at 1 m/s average.", [tr])
    assert status == GROUNDING_VERIFIED
    status, _ = verify_grounding("13.0 samples at 1.0 m/s.", [tr])
    assert status == GROUNDING_VERIFIED


def test_verify_timestamp_in_answer_not_flagged() -> None:
    tr = _tr({"count": 1})
    status, offending = verify_grounding(
        "1 sample recorded at 2026-08-26T10:13:30Z.", [tr]
    )
    assert status == GROUNDING_VERIFIED
    assert offending == []


def test_verify_empty_tool_log_flags_all_numbers() -> None:
    status, offending = verify_grounding("There are 42 robots.", [])
    assert status == GROUNDING_UNVERIFIED
    assert offending == ["42"]


# -- wired into /ask for tier-1 responses ------------------------------------

def _fake_llm(answer: str):
    """One tool round (get_fleet_summary) then a fixed final answer."""
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                id="c1", type="function",
                function=SimpleNamespace(name="get_fleet_summary", arguments="{}"),
            )],
        ))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=answer, tool_calls=[],
        ))]),
    ]

    def fn(**kwargs):
        return responses.pop(0)

    return fn


def _client(transport: StaticTransport, answer: str) -> TestClient:
    settings = Settings(
        supabase_url="https://x.invalid", supabase_key="k", model="fake/model"
    )
    return TestClient(create_app(
        transport=transport, settings=settings, completion_fn=_fake_llm(answer)
    ))


def test_ask_tier1_verified(transport: StaticTransport) -> None:
    body = _client(transport, "6 robots, average battery 44.5%.").post(
        "/ask", json={"question": "fleet status"}
    ).json()
    assert body["tier"] == "grounded"
    assert body["grounding"] == "verified"
    assert body["meta"] == {}


def test_ask_tier1_unverified_lists_offenders(transport: StaticTransport) -> None:
    body = _client(transport, "There are 17 robots at 93.7% average battery.").post(
        "/ask", json={"question": "fleet status"}
    ).json()
    assert body["tier"] == "grounded"
    assert body["grounding"] == "unverified"
    assert body["meta"]["unsupported_numbers"] == ["17", "93.7"]
