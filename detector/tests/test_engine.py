"""IncidentEngine: edges, flap guard, dedup, re-open, stale sweep. Offline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from yantradetect.engine import IncidentEngine, incident_id

T0 = datetime(2026, 9, 1, 14, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def robot(rid: str = "AMR-07", status: str = "active", fault_msg: str | None = None):
    return {"id": rid, "status": status, "fault_msg": fault_msg}


def open_incident(engine: IncidentEngine, rid: str = "AMR-07",
                  status: str = "fault", start: float = 0.0):
    """Drive the default 2-poll pending window; return the open action."""
    assert engine.observe([robot(rid, status)], at(start)) == []
    acts = engine.observe([robot(rid, status)], at(start + 5))
    assert [a.op for a in acts] == ["open"]
    return acts[0]


# -- flap guard (pending window) -------------------------------------------

def test_single_bad_poll_does_not_open():
    eng = IncidentEngine()
    assert eng.observe([robot(status="fault")], at(0)) == []
    # back to normal: blip suppressed for good
    assert eng.observe([robot(status="active")], at(5)) == []
    assert eng.open_incidents == {}


def test_alternating_blips_never_open():
    eng = IncidentEngine(pending_polls=2)
    for i in range(6):
        status = "fault" if i % 2 == 0 else "active"
        assert eng.observe([robot(status=status)], at(5 * i)) == []


def test_pending_polls_configurable():
    eng = IncidentEngine(pending_polls=3)
    assert eng.observe([robot(status="estop")], at(0)) == []
    assert eng.observe([robot(status="estop")], at(5)) == []
    acts = eng.observe([robot(status="estop")], at(10))
    assert [a.op for a in acts] == ["open"]


def test_condition_change_restarts_pending_window():
    eng = IncidentEngine(pending_polls=2)
    assert eng.observe([robot(status="fault")], at(0)) == []
    assert eng.observe([robot(status="estop")], at(5)) == []  # window restarted
    acts = eng.observe([robot(status="estop")], at(10))
    assert len(acts) == 1 and acts[0].row["sev"] == "serious"


# -- open action shape ------------------------------------------------------

def test_open_row_fault():
    eng = IncidentEngine()
    eng.observe([robot(status="fault", fault_msg="localization loss")], at(0))
    acts = eng.observe([robot(status="fault", fault_msg="localization loss")], at(5))
    row = acts[0].row
    assert row["id"].startswith("INC-") and len(row["id"]) == 8
    assert row["sev"] == "crit"
    assert row["src"] == "AMR-07"
    assert row["state"] == "Open"
    assert row["tlabel"] == "14:00"
    assert row["impact"] == "0 min so far · robot out of service"
    assert row["dur"] == 0
    assert "localization loss" in row["title"]
    assert row["created_at"] == at(5).isoformat()  # console orders by it; seed() restores from it
    assert row["site_id"] == "BLR-DC1"  # yantracore.site_id() default (v0.5.x)
    assert acts[0].incident_id == row["id"]


def test_open_row_estop_severity_and_title():
    eng = IncidentEngine()
    act = open_incident(eng, rid="AMR-03", status="estop")
    assert act.row["sev"] == "serious"
    assert "Emergency stop on AMR-03" in act.row["title"]


def test_incident_id_deterministic():
    a = incident_id("AMR-07", "fault", at(5))
    b = incident_id("AMR-07", "fault", at(5))
    assert a == b
    assert a != incident_id("AMR-07", "fault", at(10))
    assert a != incident_id("AMR-08", "fault", at(5))
    # two identical engines fed identical snapshots agree on the id
    e1, e2 = IncidentEngine(), IncidentEngine()
    assert open_incident(e1).row["id"] == open_incident(e2).row["id"]


def test_legacy_status_spellings_normalized():
    eng = IncidentEngine()
    eng.observe([robot(status="safety_stop")], at(0))
    acts = eng.observe([robot(status="safety_stop")], at(5))
    assert acts[0].row["sev"] == "serious"  # -> estop


# -- dedup ------------------------------------------------------------------

def test_one_open_incident_per_robot():
    eng = IncidentEngine()
    open_incident(eng)
    for i in range(2, 8):
        acts = eng.observe([robot(status="fault")], at(5 * i))
        assert all(a.op != "open" for a in acts)
    assert len(eng.open_incidents) == 1


def test_condition_switch_does_not_open_second_incident():
    eng = IncidentEngine()
    act = open_incident(eng, status="fault")
    acts = eng.observe([robot(status="estop")], at(10))
    acts += eng.observe([robot(status="estop")], at(15))
    assert all(a.op != "open" for a in acts)
    assert eng.open_incidents["AMR-07"].id == act.row["id"]


def test_independent_robots_get_independent_incidents():
    eng = IncidentEngine()
    snap = [robot("AMR-01", "fault"), robot("AMR-02", "estop"),
            robot("AMR-03", "active")]
    eng.observe(snap, at(0))
    acts = eng.observe(snap, at(5))
    assert sorted(a.row["src"] for a in acts) == ["AMR-01", "AMR-02"]
    assert {a.row["sev"] for a in acts} == {"crit", "serious"}


def test_duration_patch_once_per_minute():
    eng = IncidentEngine()
    act = open_incident(eng)
    # 30s later: same minute, no patch
    assert eng.observe([robot(status="fault")], at(35)) == []
    # 65s after open: dur ticks to 1
    acts = eng.observe([robot(status="fault")], at(70))
    assert acts == [acts[0]] and acts[0].op == "patch"
    assert acts[0].incident_id == act.row["id"]
    assert acts[0].row == {"dur": 1, "impact": "1 min so far · robot out of service"}


# -- resolve ----------------------------------------------------------------

def test_resolve_on_clear():
    eng = IncidentEngine()
    act = open_incident(eng)
    acts = eng.observe([robot(status="active")], at(5 + 180))
    assert len(acts) == 1 and acts[0].op == "patch"
    assert acts[0].incident_id == act.row["id"]
    assert acts[0].row["state"] == "Resolved"
    assert acts[0].row["dur"] == 3
    assert acts[0].row["impact"] == "3 min robot out of service · recovered"
    assert eng.open_incidents == {}


def test_clear_hold_hysteresis():
    eng = IncidentEngine(clear_polls=2)
    open_incident(eng)
    assert eng.observe([robot(status="idle")], at(10)) == []  # holding
    # a bad blip resets the clear streak
    eng.observe([robot(status="fault")], at(15))
    assert eng.observe([robot(status="idle")], at(20)) == []
    acts = eng.observe([robot(status="idle")], at(25))
    assert [a.row.get("state") for a in acts] == ["Resolved"]


# -- re-open window / flap counting ----------------------------------------

def test_reopen_within_window_reuses_incident():
    eng = IncidentEngine(reopen_window_s=300)
    act = open_incident(eng)
    eng.observe([robot(status="active")], at(10))  # resolved
    eng.observe([robot(status="fault")], at(20))
    acts = eng.observe([robot(status="fault")], at(25))
    assert len(acts) == 1 and acts[0].op == "patch"
    assert acts[0].incident_id == act.row["id"]  # same row, no new INC
    assert acts[0].row["state"] == "Open"
    assert eng.open_incidents["AMR-07"].flap_count == 1


def test_reopen_after_window_opens_new_incident():
    eng = IncidentEngine(reopen_window_s=60)
    act = open_incident(eng)
    eng.observe([robot(status="active")], at(10))  # resolved at t=10
    eng.observe([robot(status="fault")], at(200))
    acts = eng.observe([robot(status="fault")], at(205))
    assert [a.op for a in acts] == ["open"]
    assert acts[0].row["id"] != act.row["id"]


def test_flap_threshold_tags_flapping():
    eng = IncidentEngine(reopen_window_s=10_000, flap_threshold=2)
    open_incident(eng)
    t = 10.0
    last = None
    for _ in range(2):  # two resolve/re-open cycles -> flap_count 2
        eng.observe([robot(status="active")], at(t)); t += 5
        eng.observe([robot(status="fault")], at(t)); t += 5
        last = eng.observe([robot(status="fault")], at(t))[0]; t += 5
    assert "flapping" in last.row["impact"]


# -- stale sweep ------------------------------------------------------------

def test_stale_robot_auto_resolves():
    eng = IncidentEngine(stale_polls=1)
    act = open_incident(eng)
    assert eng.observe([], at(10)) == []  # 1 missing poll: within TTL
    acts = eng.observe([], at(15))        # 2nd: over TTL
    assert len(acts) == 1 and acts[0].incident_id == act.row["id"]
    assert acts[0].row["state"] == "Resolved"
    assert "stale/no-data" in acts[0].row["impact"]


# -- restart seeding --------------------------------------------------------

def test_seed_rebuilds_dedup_state():
    eng = IncidentEngine()
    eng.seed([{"id": "INC-4242", "sev": "crit", "src": "AMR-07", "dur": 5,
               "created_at": "2026-09-01T13:55:00+00:00"}])
    # robot still faulted after restart: no duplicate open, just dur patches
    acts = eng.observe([robot(status="fault")], at(0))
    acts += eng.observe([robot(status="fault")], at(5))
    assert all(a.op != "open" for a in acts)
    # recovery patches the seeded row
    acts = eng.observe([robot(status="active")], at(10))
    assert acts[0].incident_id == "INC-4242"
    assert acts[0].row["state"] == "Resolved"
