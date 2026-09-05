"""Unit tests for the pure VDA 5050 validators in :mod:`yantraconform.spec`."""
from __future__ import annotations

import math

import pytest

from yantraconform import spec


# ---------------------------------------------------------------------------
# topics
# ---------------------------------------------------------------------------

def test_parse_topic_splits_five_segments():
    parts = spec.parse_topic("uagv/v2/acme/AGV_01/state")
    assert parts is not None
    assert (parts.interface, parts.major, parts.manufacturer, parts.serial,
            parts.subtopic) == ("uagv", "v2", "acme", "AGV_01", "state")
    assert parts.prefix == "uagv/v2/acme/AGV_01"
    assert parts.major_number == 2


@pytest.mark.parametrize("topic", [
    "uagv/acme/AGV_01/state",              # missing the version level
    "uagv/v2/acme/AGV_01/state/extra",     # one segment too many
    "uagv/v2//AGV_01/state",               # empty manufacturer
    "",
])
def test_parse_topic_rejects_malformed(topic):
    assert spec.parse_topic(topic) is None


def test_build_topic_round_trips():
    topic = spec.build_topic("uagv", "v2", "acme", "AGV_01", "connection")
    assert topic == "uagv/v2/acme/AGV_01/connection"
    assert spec.parse_topic(topic).subtopic == "connection"


@pytest.mark.parametrize("serial,ok", [
    ("AGV_01", True), ("amr.7:b", True), ("AMR-07", True),
    ("AGV/01", False), ("AGV+01", False), ("AGV#01", False), ("", False),
])
def test_serial_charset(serial, ok):
    assert spec.is_legal_serial(serial) is ok


@pytest.mark.parametrize("segment,ok", [
    ("acme", True), ("acme robotics", True),
    ("acme/x", False), ("a+b", False), ("a#b", False), ("$sys", False),
    ("", False),
])
def test_segment_legality(segment, ok):
    assert spec.is_legal_segment(segment) is ok


@pytest.mark.parametrize("segment,ok", [
    ("v2", True), ("v10", True), ("2", False), ("v2.1", False), ("", False),
])
def test_major_segment(segment, ok):
    assert spec.is_major_segment(segment) is ok


# ---------------------------------------------------------------------------
# versions and timestamps
# ---------------------------------------------------------------------------

def test_parse_version():
    assert spec.parse_version("2.1.0") == (2, 1, 0)
    assert spec.parse_version("2.1") is None
    assert spec.parse_version("v2.1.0") is None
    assert spec.parse_version("") is None


def test_conformant_timestamp_has_no_problem():
    assert spec.timestamp_problem("2026-08-26T10:15:32.512Z") is None


@pytest.mark.parametrize("value,fragment", [
    (None, "missing"),
    ("", "missing"),
    (1756202132, "missing"),                       # epoch number, not a string
    ("not a date", "not parseable"),
    ("2026-08-26T10:15:32.512+00:00", "'Z' form"),  # right instant, wrong form
    ("2026-08-26T12:15:32.512+02:00", "'Z' form"),  # local time
])
def test_timestamp_problems(value, fragment):
    problem = spec.timestamp_problem(value)
    assert problem is not None and fragment in problem


def test_parse_timestamp_accepts_offset_form_for_ordering():
    """Non-conformant but parseable timestamps still order correctly."""
    a = spec.parse_timestamp("2026-08-26T10:15:32.512+00:00")
    b = spec.parse_timestamp("2026-08-26T10:15:33.512Z")
    assert a is not None and b is not None and a < b


# ---------------------------------------------------------------------------
# field validation
# ---------------------------------------------------------------------------

def test_field_problems_reports_missing_and_mistyped():
    problems = spec.field_problems(
        {"headerId": "7", "timestamp": "2026-08-26T10:15:32.512Z",
         "version": "2.1.0", "manufacturer": "acme"},
        spec.HEADER_FIELDS)
    assert any("headerId is str" in p for p in problems)
    assert any("serialNumber missing" in p for p in problems)


def test_field_problems_rejects_bool_where_integer_required():
    """bool subclasses int in Python; 'headerId': true is not an integer."""
    problems = spec.field_problems({"headerId": True}, {"headerId": (int,)})
    assert problems and "boolean" in problems[0]


def test_field_problems_accepts_bool_where_boolean_required():
    assert spec.field_problems({"driving": False}, {"driving": (bool,)}) == []


def test_field_problems_treats_null_as_missing():
    problems = spec.field_problems({"mapId": None}, {"mapId": (str,)})
    assert problems and "missing" in problems[0]


def test_field_problems_prefixes_the_object_name():
    problems = spec.field_problems({}, {"eStop": (str,)}, where="safetyState")
    assert problems == ["safetyState.eStop missing (expected string)"]


# ---------------------------------------------------------------------------
# geometry / sequencing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("theta,ok", [
    (0.0, True), (math.pi, True), (-math.pi, True), (3.1416, True),
    (7.5, False), (-4.0, False), (True, False), ("0.4", False),
])
def test_theta_in_range(theta, ok):
    assert spec.theta_in_range(theta) is ok


def test_sequence_ids_conformant():
    nodes = [{"nodeId": "N1", "sequenceId": 2}, {"nodeId": "N2", "sequenceId": 4}]
    edges = [{"edgeId": "E1", "sequenceId": 1}, {"edgeId": "E2", "sequenceId": 3}]
    assert spec.sequence_ids_wellformed(nodes, edges) == []


def test_sequence_ids_flags_wrong_parity():
    problems = spec.sequence_ids_wellformed([{"sequenceId": 3}], [])
    assert any("parity" in p for p in problems)


def test_sequence_ids_flags_non_increasing():
    problems = spec.sequence_ids_wellformed(
        [{"sequenceId": 4}, {"sequenceId": 2}], [])
    assert any("does not increase" in p for p in problems)


def test_sequence_ids_flags_missing_id():
    problems = spec.sequence_ids_wellformed([{"nodeId": "N1"}], [])
    assert any("missing or not an integer" in p for p in problems)


# ---------------------------------------------------------------------------
# the constants themselves
# ---------------------------------------------------------------------------

def test_vocabularies_are_uppercase_and_unique():
    for name in ("CONNECTION_STATES", "ACTION_STATUSES", "OPERATING_MODES",
                 "ERROR_LEVELS", "ESTOP_VALUES"):
        values = getattr(spec, name)
        assert len(set(values)) == len(values), name
        assert all(v == v.upper() for v in values), name


def test_terminal_statuses_are_a_subset_of_action_statuses():
    assert set(spec.TERMINAL_ACTION_STATUSES) <= set(spec.ACTION_STATUSES)


def test_agv_to_master_and_master_to_agv_topics_are_disjoint():
    assert not set(spec.SUBTOPICS_FROM_AGV) & set(spec.SUBTOPICS_TO_AGV)


def test_spec_agrees_with_the_simulator_on_the_topic_scheme():
    """Cross-check the copied constants against ``sim/yantrasim/vda.py``.

    ``spec`` deliberately re-states the protocol rather than importing the
    implementation under test (see that module's docstring). This test is the
    seam: if the two ever drift, one of them is wrong and somebody has to say
    which.
    """
    vda = pytest.importorskip("yantrasim.vda")
    assert vda.INTERFACE_NAME == spec.DEFAULT_INTERFACE
    assert vda.VDA_MAJOR == spec.DEFAULT_MAJOR_SEGMENT
    assert vda.VDA_VERSION == spec.TARGET_VERSION
    built = vda.topic("acme", "AGV_01", "state")
    parts = spec.parse_topic(built)
    assert parts is not None and parts.subtopic == "state"
