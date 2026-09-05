"""Does the tester actually FAIL non-compliant robots?

This is the file that decides whether the whole tool is worth anything. A
conformance tester that passes everything is trivially easy to ship by
accident: every check returns ``pass`` when the evidence looks fine, and
``skip`` when the evidence is missing, so a subtly broken engine that never
collects any evidence still produces a clean, confident, worthless report.

So the battery below asserts BOTH halves of the property, for every single
check in the catalogue:

* **Sensitivity.** Switch on exactly one non-compliant behaviour in
  :class:`fake_robots.FakeRobot` and the corresponding check must flip to
  ``fail`` (or, for the handful of checks whose worst outcome is by design a
  warning, to ``warn``).
* **Specificity.** Nothing else may flip. The expected fallout of each defect
  is written out in full in :data:`DEFECT_CASES`, so a change that makes some
  unrelated check start firing breaks this suite instead of quietly turning
  the report into noise. Where one defect genuinely violates two clauses --
  a header without ``version`` breaks both the header rule and the version
  rule -- both are named, with a comment saying why.

:func:`test_every_check_has_a_failing_case` then closes the loop mechanically:
it walks ``checks.CATALOGUE`` and fails if any check id is not exercised
negatively somewhere in the table. A new check cannot be added without a test
that proves it can fail.
"""
from __future__ import annotations

import dataclasses

import pytest

from fake_robots import Defects, FakeRobot
from harness import run_against, statuses
from yantraconform import checks
from yantraconform.loopback import LoopbackBroker

_DEFECT_NAMES = {f.name for f in dataclasses.fields(Defects)}


def run_robot(**kwargs):
    """Run the full battery against one FakeRobot and return (doc, statuses).

    Keyword arguments are split automatically: anything named on
    :class:`Defects` configures the defect, everything else is passed to the
    :class:`FakeRobot` constructor (``serial``, ``major``, ...).
    """
    defect_kwargs = {k: v for k, v in kwargs.items() if k in _DEFECT_NAMES}
    robot_kwargs = {k: v for k, v in kwargs.items() if k not in _DEFECT_NAMES}
    robot_kwargs.setdefault("publish_visualization", True)
    broker = LoopbackBroker()
    robot = FakeRobot(broker, defects=Defects(**defect_kwargs), **robot_kwargs)
    document = run_against(lambda ticks: robot.tick(ticks), broker)
    return document, statuses(document)


def _of_status(status_map: dict[str, str], want: str) -> set[str]:
    return {cid for cid, status in status_map.items() if status == want}


# ---------------------------------------------------------------------------
# the reference vehicle
# ---------------------------------------------------------------------------

def test_a_compliant_robot_has_no_findings_at_all():
    """The floor of the whole exercise: no false positives on a good vehicle.

    If this fails, every ``fail`` asserted below is worthless -- a tester that
    fails a conformant robot is not detecting anything, it is just noisy.
    """
    document, status_map = run_robot()
    assert _of_status(status_map, "fail") == set()
    assert _of_status(status_map, "warn") == set()
    assert document["summary"]["grade"] == "A"
    assert document["summary"]["score"] == 100.0
    assert document["summary"]["grade_capped_by"] == []


def test_a_compliant_robot_leaves_nothing_unobserved():
    """Every check must actually reach a verdict on a well-behaved vehicle.

    A check that is permanently ``skip`` scores nothing and proves nothing, so
    a green report full of skips would be a fake pass. The two exceptions are
    named explicitly rather than tolerated as a blanket allowance.
    """
    _, status_map = run_robot()
    skipped = _of_status(status_map, "skip")
    assert skipped == set(), f"never evaluated on a conformant vehicle: {skipped}"


# ---------------------------------------------------------------------------
# one defect at a time
# ---------------------------------------------------------------------------

#: (label, robot/defect kwargs, expected failures, expected warnings).
#: The expected sets are EXACT: extra findings fail the test just as missing
#: ones do.
DEFECT_CASES: list[tuple[str, dict, set[str], set[str]]] = [
    # -- connection --------------------------------------------------------
    ("never publishes connection", {"no_connection": True},
     {"connection.published"}, set()),
    ("connectionState 'CONNECTED'", {"bad_connection_state": True},
     # Also loses connection.online: the enum value it invented is not ONLINE,
     # so liveness is never announced in a form anyone can read.
     {"connection.state_enum", "connection.online"}, set()),
    ("connection not retained", {"connection_not_retained": True},
     {"connection.retained"}, set()),
    ("no last-will registered", {"no_lastwill": True},
     {"connection.lastwill"}, set()),
    ("payload serial disagrees with the topic", {"identity_mismatch": True},
     # The header is shared by every topic, so all three identity-bearing
     # topics are wrong. factsheet.header folds identity into itself.
     {"state.identity", "connection.identity", "factsheet.header"}, set()),

    # -- state -------------------------------------------------------------
    ("never publishes state", {"no_state": True},
     # A silent vehicle cannot acknowledge an instantAction either; those
     # failures are real, not collateral, because master control genuinely
     # never learns the action's outcome.
     {"state.published", "instant.stateRequest", "instant.cancelOrder",
      "instant.initPosition", "instant.unknown_rejected"},
     {"instant.factsheetRequest"}),
    ("drops required state fields", {"missing_state_fields": True},
     # errors[] is one of the dropped fields, so the orderUpdateError raised
     # for the stale replay becomes unobservable too.
     {"state.required_fields", "order.reject_stale_reports_error"}, set()),
    ("batteryCharge sent as a string", {"string_battery_charge": True},
     {"state.field_types", "state.battery_range"}, set()),
    ("omits the optional-but-needed blocks", {"drop_recommended_fields": True},
     set(), {"state.recommended_fields"}),
    ("state header has no version", {"state_header_drop_version": True},
     # Two clauses, genuinely: the header is incomplete AND there is no
     # protocol version to match against the 'v2' topic segment.
     {"state.header", "state.version_field"}, set()),
    ("headerId never advances", {"header_id_frozen": True},
     {"state.header_id_monotonic"}, set()),
    ("local-time timestamps", {"local_timestamps": True},
     {"state.timestamp_format"}, set()),
    ("timestamps jump backwards", {"timestamps_go_backwards": True},
     {"state.timestamp_monotonic"}, set()),
    ("serialNumber outside the charset", {"serial": "AGV@01"},
     {"state.serial_charset"}, set()),
    ("reports a 3.x protocol version", {"future_major_version": True},
     {"state.version_field"}, {"protocol.supported_version"}),
    ("retains the state topic", {"retained_state": True},
     {"state.not_retained"}, set()),
    ("theta outside [-pi, pi]", {"theta_out_of_range": True},
     {"state.agv_position"}, set()),
    ("batteryCharge of 150%", {"battery_out_of_range": True},
     {"state.battery_range"}, set()),
    ("batteryCharge as a 0..1 fraction", {"battery_as_fraction": True},
     set(), {"state.battery_range"}),
    ("errorLevel outside the enum", {"malformed_error_entry": True},
     {"state.errors_wellformed"}, set()),
    ("operatingMode 'AUTO'", {"bad_operating_mode": True},
     {"state.operating_mode"}, set()),
    ("eStop 'ESTOPPED'", {"bad_estop": True},
     {"state.safety_state"}, set()),
    ("node/edge sequenceId parity swapped", {"wrong_sequence_parity": True},
     {"state.sequence_ids"}, set()),

    # -- orders ------------------------------------------------------------
    ("ignores the order topic entirely", {"ignore_orders": True},
     # No order is ever adopted, so there is also no order to update.
     {"order.accept_new", "order.accept_update"}, set()),
    ("drops updates to the running order", {"ignore_order_updates": True},
     {"order.accept_update"}, set()),
    ("accepts a stale orderUpdateId", {"accept_stale_order": True},
     # It rewound AND said nothing about it: two distinct defects.
     {"order.reject_stale", "order.reject_stale_reports_error"}, set()),
    ("rejects a stale update silently", {"no_order_update_error": True},
     {"order.reject_stale_reports_error"}, set()),
    ("adopts an unexecutable order", {"adopt_invalid_order": True},
     {"order.reject_invalid"}, set()),
    ("orderUpdateId echoed as a string", {"string_order_update_id": True},
     # A string where the spec says integer breaks the required-field type
     # check, the echo check, and the tester's ability to see the update land.
     {"order.echo", "state.required_fields", "order.accept_update"}, set()),

    # -- actionStates ------------------------------------------------------
    ("invents an actionStatus 'DONE'", {"custom_action_status": True},
     # A status outside the vocabulary is invisible to a compliant consumer,
     # so every action that ends in it also never reaches a terminal status.
     {"actions.status_enum", "actions.terminal_reported",
      "instant.stateRequest", "instant.cancelOrder", "instant.initPosition"},
     {"instant.factsheetRequest"}),
    ("actionStates entry without actionType", {"action_without_type": True},
     {"actions.required_fields"}, set()),
    ("actions vanish without a terminal status", {"vanishing_actions": True},
     {"actions.terminal_reported"}, set()),
    ("same actionId twice in one message", {"duplicate_action_ids": True},
     # The stale duplicate is an older, non-terminal copy sitting after the
     # terminal one, which is exactly what a regression looks like on the wire.
     {"actions.unique_ids", "actions.no_regression"}, set()),
    ("a finished action goes back to RUNNING", {"action_regresses": True},
     {"actions.no_regression"}, set()),

    # -- instantActions ----------------------------------------------------
    ("ignores stateRequest", {"ignore_instant_types": ("stateRequest",)},
     {"instant.stateRequest"}, set()),
    ("ignores factsheetRequest",
     {"ignore_instant_types": ("factsheetRequest",)},
     {"instant.factsheetRequest"}, set()),
    ("ignores cancelOrder", {"ignore_instant_types": ("cancelOrder",)},
     {"instant.cancelOrder"}, set()),
    ("ignores initPosition", {"ignore_instant_types": ("initPosition",)},
     {"instant.initPosition"}, set()),
    ("silently drops an unsupported actionType",
     {"silent_unknown_action": True}, {"instant.unknown_rejected"}, set()),
    ("claims FINISHED for an action it cannot do",
     {"finish_unknown_action": True}, {"instant.unknown_rejected"}, set()),

    # -- factsheet ---------------------------------------------------------
    ("never publishes a factsheet", {"no_factsheet": True},
     # It acknowledges factsheetRequest but publishes nothing, which the
     # instant check downgrades to a warning rather than double-failing.
     {"factsheet.published"}, {"instant.factsheetRequest"}),
    ("factsheet missing required blocks", {"factsheet_missing_blocks": True},
     {"factsheet.required_blocks"}, set()),
    ("agvKinematic outside the enum", {"factsheet_bad_enums": True},
     {"factsheet.type_specification"}, set()),
    ("physicalParameters incomplete",
     {"factsheet_incomplete_physical": True},
     {"factsheet.physical_parameters"}, set()),
    ("factsheet published without a header", {"factsheet_no_header": True},
     {"factsheet.header"}, set()),
    ("factsheet not retained", {"factsheet_not_retained": True},
     {"factsheet.retained"}, set()),

    # -- visualization -----------------------------------------------------
    ("visualization with no pose or velocity",
     {"broken_visualization": True}, {"visualization.wellformed"}, set()),

    # -- protocol hygiene --------------------------------------------------
    ("publishes an extra, non-spec sub-topic", {"extra_subtopic": True},
     {"protocol.topic_scheme"}, set()),
    ("publishes with the version level missing", {"bad_topic_scheme": True},
     # The topic is unroutable, so no state is ever attributed to the vehicle
     # -- which is precisely the production symptom of getting this wrong.
     {"protocol.topic_scheme", "state.published", "instant.stateRequest",
      "instant.cancelOrder", "instant.initPosition",
      "instant.unknown_rejected"},
     {"instant.factsheetRequest"}),
    ("majorVersion segment is '2' not 'v2'", {"major": "2"},
     {"protocol.major_version_segment"}, set()),
    ("topics disagree about the protocol version", {"version_skew": True},
     {"protocol.version_consistency"}, set()),
    ("publishes a non-JSON payload", {"malformed_json": True},
     {"protocol.json_valid"}, set()),
]


@pytest.mark.parametrize(
    "kwargs,expected_fail,expected_warn",
    [pytest.param(kw, f, w, id=label) for label, kw, f, w in DEFECT_CASES])
def test_one_defect_fails_exactly_the_right_checks(kwargs, expected_fail,
                                                   expected_warn):
    _, status_map = run_robot(**kwargs)
    assert _of_status(status_map, "fail") == expected_fail
    assert _of_status(status_map, "warn") == expected_warn


def test_every_check_has_a_failing_case():
    """No check may exist without a case that proves it can fire.

    Mechanical, so it cannot rot: adding a check to the catalogue without
    adding a negative case to :data:`DEFECT_CASES` fails right here.
    """
    exercised = {cid for _, _, fails, warns in DEFECT_CASES
                 for cid in fails | warns}
    catalogue = {cs.id for cs in checks.CATALOGUE}
    missing = catalogue - exercised
    assert missing == set(), (
        "these checks are never observed failing, so nothing proves they "
        f"can: {sorted(missing)}")


def test_no_case_names_a_check_that_does_not_exist():
    """Guards the other direction: a renamed check must not leave a dead case."""
    exercised = {cid for _, _, fails, warns in DEFECT_CASES
                 for cid in fails | warns}
    catalogue = {cs.id for cs in checks.CATALOGUE}
    assert exercised - catalogue == set()


# ---------------------------------------------------------------------------
# the headline defects, asserted on their own terms
# ---------------------------------------------------------------------------

def test_a_broken_vehicle_is_graded_f_and_the_cap_is_explained():
    """Several critical defects at once: F, and the report says which capped it."""
    document, _ = run_robot(no_connection=True, missing_state_fields=True,
                            accept_stale_order=True, ignore_orders=True)
    summary = document["summary"]
    assert summary["grade"] == "F"
    # The cap has to name the critical failures, not just assert a letter.
    assert "connection.published" in summary["grade_capped_by"]
    assert "order.accept_new" in summary["grade_capped_by"]
    assert summary["score"] < 100.0


def test_a_single_major_defect_caps_the_grade_at_b():
    """One major failure must not still read as an A on a weighted average."""
    document, _ = run_robot(header_id_frozen=True)
    summary = document["summary"]
    assert summary["grade"] == "B"
    assert summary["grade_capped_by"] == ["state.header_id_monotonic"]
    # ... and the score itself is still high, which is exactly why the cap
    # has to exist: 51 of 52 checks passed.
    assert summary["score"] > 90.0


def test_findings_carry_everything_an_integrator_needs_to_act():
    """Every non-passing finding is actionable, not just a red label."""
    document, _ = run_robot(header_id_frozen=True, bad_estop=True,
                            no_lastwill=True)
    findings = [c for r in document["robots"] for c in r["checks"]
                if c["status"] in ("fail", "warn")]
    assert findings
    for finding in findings:
        assert finding["spec_ref"].startswith("VDA 5050"), finding["id"]
        assert finding["severity"] in ("critical", "major", "minor", "info")
        assert finding["expected"].strip(), finding["id"]
        assert finding["observed"].strip(), finding["id"]
        # The remediation is the difference between a report and a complaint.
        assert len(finding["remediation"].strip()) > 40, finding["id"]


def test_passing_checks_carry_no_remediation_noise():
    """A green check must not tell anyone to fix anything."""
    document, _ = run_robot()
    for robot in document["robots"]:
        for check in robot["checks"]:
            if check["status"] == "pass":
                assert check["remediation"] == "", check["id"]


# ---------------------------------------------------------------------------
# passive mode and multi-vehicle runs
# ---------------------------------------------------------------------------

def test_passive_mode_publishes_nothing_to_the_vehicle():
    """--passive must be safe to point at a live fleet."""
    from harness import fast_options

    broker = LoopbackBroker()
    robot = FakeRobot(broker)
    before = len(broker.log)
    document = run_against(lambda ticks: robot.tick(ticks), broker,
                           options=fast_options(active=False))
    published_topics = {topic for topic, _, _, _ in broker.log[before:]}
    assert not any(t.endswith(("/order", "/instantActions"))
                   for t in published_topics), published_topics
    status_map = statuses(document)
    # The command-surface checks must report themselves as not evaluated
    # rather than passing on no evidence.
    for cid in ("order.accept_new", "instant.cancelOrder",
                "instant.unknown_rejected"):
        assert status_map[cid] == "skip", cid
    assert document["robots"][0]["notes"], "passive mode must say so in the report"


def test_passive_mode_never_claims_a_probe_it_did_not_send():
    """The report is shown to a vendor; it must not misstate what was tried."""
    from harness import checks_by_id, fast_options

    broker = LoopbackBroker()
    # A vehicle that only answers factsheetRequest and never retains one --
    # so the factsheet check fails, and the reason it gives has to be honest.
    robot = FakeRobot(broker, defects=Defects(no_factsheet=True))
    document = run_against(lambda ticks: robot.tick(ticks), broker,
                           options=fast_options(active=False))
    finding = checks_by_id(document)["factsheet.published"]
    assert finding["status"] == "fail"
    assert "after an explicit factsheetRequest" not in finding["observed"]
    assert "passive" in finding["observed"]
    assert "--passive" in finding["remediation"]


def test_a_bad_vehicle_does_not_drag_down_a_good_one():
    """Per-vehicle grading: findings stay attributed to the robot that earned them."""
    from harness import checks_by_id

    broker = LoopbackBroker()
    good = FakeRobot(broker, serial="AGV_GOOD")
    bad = FakeRobot(broker, serial="AGV_BAD",
                    defects=Defects(header_id_frozen=True, bad_estop=True))

    def tick(n):
        good.tick(n)
        bad.tick(n)

    document = run_against(tick, broker)
    by_serial = {r["serial"]: r for r in document["robots"]}
    assert set(by_serial) == {"AGV_GOOD", "AGV_BAD"}
    assert by_serial["AGV_GOOD"]["counts"]["fail"] == 0
    assert by_serial["AGV_GOOD"]["score"] == 100.0
    assert by_serial["AGV_BAD"]["counts"]["fail"] == 2
    index = {r["serial"]: i for i, r in enumerate(document["robots"])}
    bad_checks = checks_by_id(document, index["AGV_BAD"])
    assert bad_checks["state.header_id_monotonic"]["status"] == "fail"
    good_checks = checks_by_id(document, index["AGV_GOOD"])
    assert good_checks["state.header_id_monotonic"]["status"] == "pass"


def test_a_dead_vehicle_publishes_its_last_will():
    """The last-will path is exercised, not just its registration."""
    broker = LoopbackBroker()
    robot = FakeRobot(broker)
    robot.tick(2)
    robot.kill()
    payloads = [payload for topic, payload, _, _ in broker.log
                if topic.endswith("/connection")]
    assert any("CONNECTIONBROKEN" in p for p in payloads)


def test_scoping_to_one_serial_ignores_the_rest_of_the_fleet():
    """--serial must not widen just because the tester scans the namespace."""
    from harness import fast_options

    broker = LoopbackBroker()
    wanted = FakeRobot(broker, serial="AGV_01")
    other = FakeRobot(broker, serial="AGV_02",
                      defects=Defects(header_id_frozen=True))

    def tick(n):
        wanted.tick(n)
        other.tick(n)

    document = run_against(tick, broker,
                           options=fast_options(serial="AGV_01"))
    assert [r["serial"] for r in document["robots"]] == ["AGV_01"]
    assert document["summary"]["counts"]["fail"] == 0
    assert any("ignored because the run is pinned" in w
               for w in document["warnings"]), document["warnings"]
