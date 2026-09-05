"""Unit tests for the scoring model and report shape."""
from __future__ import annotations

import pytest

from yantraconform.model import (
    SCHEMA_VERSION, SEVERITY_WEIGHT, CheckResult, Report, RobotReport,
    grade_for, score_checks, tally,
)


def make(status: str, severity: str = "major", id: str = "x.y") -> CheckResult:
    return CheckResult(id=id, title="t", category="state", severity=severity,
                       spec_ref="VDA 5050 2.1 §6.4", status=status,
                       expected="e", observed="o", remediation="fix it")


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------

def test_bad_severity_is_rejected():
    with pytest.raises(ValueError):
        make("pass", severity="catastrophic")


def test_bad_status_is_rejected():
    with pytest.raises(ValueError):
        make("exploded")


def test_info_checks_are_never_scored():
    chk = make("fail", severity="info")
    assert chk.weight == 0
    assert chk.scored is False


def test_skipped_checks_are_never_scored():
    assert make("skip").scored is False


def test_to_dict_carries_every_field_a_web_layer_needs():
    doc = make("fail").to_dict()
    assert set(doc) == {"id", "title", "category", "severity", "spec_ref",
                        "status", "expected", "observed", "remediation",
                        "detail"}


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def test_all_pass_is_a_hundred():
    score, grade, capped = score_checks(
        [make("pass", "critical"), make("pass", "minor")])
    assert (score, grade, capped) == (100.0, "A", [])


def test_warn_scores_half_credit():
    score, _, _ = score_checks([make("warn", "major")])
    assert score == 50.0


def test_skip_is_excluded_from_both_sides_of_the_ratio():
    """A skipped check must not drag the score down OR prop it up."""
    only_pass = score_checks([make("pass", "major")])[0]
    with_skip = score_checks([make("pass", "major"),
                              make("skip", "critical", id="a.b")])[0]
    assert only_pass == with_skip == 100.0


def test_severity_weights_are_respected():
    # one failed critical (10) against three passed minors (2 each)
    score, _, _ = score_checks([
        make("fail", "critical", "c.1"),
        make("pass", "minor", "m.1"), make("pass", "minor", "m.2"),
        make("pass", "minor", "m.3")])
    assert score == pytest.approx(100.0 * 6 / 16, abs=0.05)


def test_no_scored_checks_scores_zero_without_dividing_by_zero():
    assert score_checks([make("skip")]) == (0.0, "F", [])


@pytest.mark.parametrize("score,grade", [
    (100.0, "A"), (95.0, "A"), (94.9, "B"), (85.0, "B"), (84.9, "C"),
    (70.0, "C"), (69.9, "D"), (50.0, "D"), (49.9, "F"), (0.0, "F"),
])
def test_grade_bands(score, grade):
    assert grade_for(score) == grade


# ---------------------------------------------------------------------------
# honesty caps
# ---------------------------------------------------------------------------

def test_a_failed_critical_caps_the_grade_at_f():
    """19 passing minors + 1 failed critical is still a failing vehicle."""
    checks = [make("pass", "minor", f"m.{i}") for i in range(19)]
    checks.append(make("fail", "critical", "c.1"))
    score, grade, capped = score_checks(checks)
    assert score > 70.0          # the weighted average alone looks survivable
    assert grade == "F"
    assert capped == ["c.1"]


def test_a_failed_major_caps_the_grade_at_b():
    checks = [make("pass", "critical", f"c.{i}") for i in range(20)]
    checks.append(make("fail", "major", "j.1"))
    score, grade, capped = score_checks(checks)
    assert score >= 95.0         # would otherwise be an A
    assert grade == "B"
    assert capped == ["j.1"]


def test_a_cap_never_improves_a_worse_grade():
    """A major failure caps AT B; it must not lift an F up to a B."""
    checks = [make("fail", "critical", "c.1"), make("fail", "major", "j.1")]
    score, grade, capped = score_checks(checks)
    assert (score, grade) == (0.0, "F")
    # Nothing is listed as a cap: the weighted score reached F on its own, so
    # no check actually constrained the grade. "capped_by" answers "why is this
    # worse than the score suggests?", and here the score suggests F already.
    assert capped == []


def test_a_failed_minor_does_not_cap():
    checks = [make("pass", "critical", f"c.{i}") for i in range(30)]
    checks.append(make("fail", "minor", "n.1"))
    _, grade, capped = score_checks(checks)
    assert grade == "A" and capped == []


def test_warnings_never_cap_the_grade():
    checks = [make("pass", "major", f"m.{i}") for i in range(40)]
    checks.append(make("warn", "critical", "c.1"))
    _, grade, capped = score_checks(checks)
    assert capped == [] and grade == "A"


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def test_tally_counts_every_status():
    counts = tally([make("pass"), make("pass"), make("fail"), make("skip")])
    assert counts == {"pass": 2, "fail": 1, "warn": 0, "skip": 1}


def test_robot_report_topic_prefix():
    rr = RobotReport(manufacturer="acme", serial="AGV_01", interface="uagv",
                     major="v2")
    assert rr.topic_prefix == "uagv/v2/acme/AGV_01"


def test_report_document_shape():
    rr = RobotReport(manufacturer="acme", serial="AGV_01", interface="uagv",
                     major="v2", checks=[make("pass"), make("fail", id="z.z")])
    doc = Report(broker="mqtt://x:1883", generated_at="2026-08-26T10:00:00.000Z",
                 robots=[rr]).to_dict()
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["tool"] == "yantra-conform"
    assert doc["summary"]["robots"] == 1
    assert doc["summary"]["failed_checks"] == ["z.z"]
    assert doc["summary"]["by_category"]["state"]["fail"] == 1
    assert doc["robots"][0]["topic_prefix"] == "uagv/v2/acme/AGV_01"


def test_fleet_score_pools_every_vehicle():
    """One bad vehicle in a fleet of two drags the fleet score down."""
    good = RobotReport("acme", "A1", "uagv", "v2",
                       checks=[make("pass", "major", "m.1")])
    bad = RobotReport("acme", "A2", "uagv", "v2",
                      checks=[make("fail", "major", "m.1")])
    doc = Report(robots=[good, bad]).to_dict()
    assert doc["summary"]["score"] == 50.0
    assert doc["robots"][0]["score"] == 100.0
    assert doc["robots"][1]["score"] == 0.0


def test_severity_weights_are_ordered():
    assert (SEVERITY_WEIGHT["critical"] > SEVERITY_WEIGHT["major"]
            > SEVERITY_WEIGHT["minor"] > SEVERITY_WEIGHT["info"] == 0)
