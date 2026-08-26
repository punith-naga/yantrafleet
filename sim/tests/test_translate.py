"""State -> Supabase row translation (pure, offline)."""
from yantrasim.sim import Event, FleetSim, SCRIPTED_FAULT_TICK
from yantrasim.translate import alert_row, derive_status, fleet_meta_row, robot_row

from conftest import FIXED_NOW

ROBOTS_COLUMNS = {
    "id", "vendor", "status", "battery", "pos", "speed", "task_kind",
    "health", "motor_temp", "tasks_done", "fault_msg", "updated_at",
}
ALERTS_COLUMNS = {"id", "sev", "msg", "src", "tlabel", "ack", "created_at"}


def _state(**over):
    base = {
        "manufacturer": "nexomotion",
        "serialNumber": "AMR_01",
        "timestamp": "2026-08-26T10:15:32.512Z",
        "driving": False,
        "paused": False,
        "agvPosition": {"x": 4.0, "y": 8.0, "theta": 0.0,
                        "mapId": "warehouse-L1", "positionInitialized": True},
        "velocity": {"vx": 0.0, "vy": 0.0, "omega": 0.0},
        "batteryState": {"batteryCharge": 72.5, "charging": False, "reach": 4350},
        "errors": [],
        "safetyState": {"eStop": "NONE", "fieldViolation": False},
        "actionStates": [],
    }
    base.update(over)
    return base


EXTRAS = {"robot_id": "AMR-01", "task_kind": "pick", "health": 98.0,
          "motor_temp": 41.2, "tasks_done": 3}


def test_derive_status_priority_order():
    fatal = [{"errorType": "x", "errorLevel": "FATAL", "errorDescription": "boom"}]
    warn = [{"errorType": "x", "errorLevel": "WARNING", "errorDescription": "warm"}]
    assert derive_status(_state(errors=fatal, driving=True)) == "fault"
    assert derive_status(_state(safetyState={"eStop": "MANUAL", "fieldViolation": False})) == "estop"
    assert derive_status(_state(paused=True)) == "paused"
    assert derive_status(_state(errors=warn)) == "degraded"
    assert derive_status(_state(batteryState={"batteryCharge": 50, "charging": True})) == "charging"
    assert derive_status(_state(actionStates=[{"actionId": "a", "actionType": "pick",
                                               "actionStatus": "RUNNING"}])) == "active"
    assert derive_status(_state(driving=True)) == "active"
    assert derive_status(_state()) == "idle"


def test_robot_row_shape_and_values():
    row = robot_row(_state(velocity={"vx": 0.9, "vy": 1.2, "omega": 0.0}), EXTRAS)
    assert set(row) == ROBOTS_COLUMNS
    assert row["id"] == "AMR-01"          # raw fleet id, not VDA serial
    assert row["vendor"] == "nexomotion"
    assert row["battery"] == 72.5
    assert row["pos"] == [4.0, 8.0]       # jsonb [x, y]
    assert row["speed"] == 1.5            # hypot(0.9, 1.2)
    assert row["fault_msg"] is None
    assert row["updated_at"] == "2026-08-26T10:15:32.512Z"


def test_robot_row_fault_message():
    err = [{"errorType": "localizationError", "errorLevel": "FATAL",
            "errorDescription": "Localization lost: LiDAR scan does not match map"}]
    row = robot_row(_state(errors=err), EXTRAS)
    assert row["status"] == "fault"
    assert "Localization lost" in row["fault_msg"]


def test_alert_row_deterministic_id():
    e = Event(kind="fault", robot_id="AMR-07", sev="crit", msg="boom", tick=20)
    row = alert_row(e, "2026-08-26T10:15:32.512Z")
    assert set(row) == ALERTS_COLUMNS
    assert row["id"] == "al-AMR-07-fault-20"   # idempotent under retry
    assert row["tlabel"] == "10:15:32"
    assert row["ack"] is False
    assert row["sev"] == "crit"


def test_fleet_meta_row():
    row = fleet_meta_row("yantrasim-abc", 1800.0, 42.5, "2026-08-26T10:15:32.512Z")
    assert row["id"] == 1
    assert row["sim_min"] == 30.0
    assert row["throughput"] == 42.5
    assert row["writer_id"] == "yantrasim-abc"


def test_end_to_end_rows_from_real_tick():
    fleet = FleetSim(seed=8)
    out = None
    while fleet.tick_count < SCRIPTED_FAULT_TICK:
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
    rows = [robot_row(s, out.extras[s["serialNumber"].replace("_", "-")])
            for s in out.states]
    assert len(rows) == 10
    r07 = next(r for r in rows if r["id"] == "AMR-07")
    assert r07["status"] == "fault"
    assert r07["fault_msg"]
    assert len({r["vendor"] for r in rows}) == 3
