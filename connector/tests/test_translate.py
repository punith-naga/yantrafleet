"""Offline unit tests for the pure translation + alert-dedup layer."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from yantrabridge.translate import (
    AlertDeduper,
    Translator,
    translate_connection,
    translate_state,
)


def make_state(**overrides: Any) -> dict[str, Any]:
    """Minimal healthy VDA 5050 v2.1 state message, overridable per test."""
    base: dict[str, Any] = {
        "headerId": 1,
        "timestamp": "2026-08-26T10:15:32.512Z",
        "version": "2.1.0",
        "manufacturer": "yantra",
        "serialNumber": "AGV-001",
        "orderId": "ord-1",
        "orderUpdateId": 0,
        "lastNodeId": "n1",
        "lastNodeSequenceId": 0,
        "nodeStates": [],
        "edgeStates": [],
        "driving": True,
        "paused": False,
        "agvPosition": {"x": 14.2, "y": 6.8, "theta": 1.57,
                        "mapId": "warehouse-L1", "positionInitialized": True},
        "velocity": {"vx": 0.6, "vy": 0.8, "omega": 0.0},
        "batteryState": {"batteryCharge": 72.5, "charging": False, "reach": 4200},
        "operatingMode": "AUTOMATIC",
        "errors": [],
        "safetyState": {"eStop": "NONE", "fieldViolation": False},
        "actionStates": [],
    }
    base.update(copy.deepcopy(overrides))
    return base


FATAL_ERROR = {
    "errorType": "gripperStalled",
    "errorLevel": "FATAL",
    "errorDescription": "Gripper motor stalled",
    "errorHint": "Clear jam",
    "errorReferences": [],
}
WARN_ERROR = {
    "errorType": "batteryLow",
    "errorLevel": "WARNING",
    "errorDescription": "SoC below planning threshold",
    "errorReferences": [],
}


# ---------------------------------------------------------------------------
# translate_state -> robots row
# ---------------------------------------------------------------------------

class TestTranslateState:
    def test_healthy_driving_row(self) -> None:
        row = translate_state(make_state())
        assert row["id"] == "AGV-001"
        assert row["vendor"] == "yantra"
        assert row["status"] == "active"
        assert row["battery"] == 72.5
        assert row["pos"] == [14.2, 6.8]
        assert row["speed"] == 1.0  # hypot(0.6, 0.8)
        assert row["health"] == 100.0
        assert row["fault_msg"] is None
        assert row["updated_at"] == "2026-08-26T10:15:32.512Z"
        # vendor extensions absent -> columns omitted, never nulled
        assert "motor_temp" not in row
        assert "tasks_done" not in row

    def test_status_priority_fault_beats_everything(self) -> None:
        row = translate_state(make_state(
            errors=[FATAL_ERROR],
            batteryState={"batteryCharge": 50, "charging": True},
            safetyState={"eStop": "MANUAL", "fieldViolation": True},
        ))
        assert row["status"] == "fault"
        assert row["fault_msg"] == "Gripper motor stalled"

    def test_status_estop(self) -> None:
        row = translate_state(make_state(
            safetyState={"eStop": "AUTOACK", "fieldViolation": False}))
        assert row["status"] == "estop"
        row = translate_state(make_state(
            safetyState={"eStop": "NONE", "fieldViolation": True}))
        assert row["status"] == "estop"

    def test_status_charging_paused_idle(self) -> None:
        assert translate_state(make_state(
            batteryState={"batteryCharge": 40, "charging": True},
        ))["status"] == "charging"
        assert translate_state(make_state(paused=True))["status"] == "paused"
        assert translate_state(make_state(driving=False))["status"] == "idle"

    def test_task_kind_running_action_then_transit(self) -> None:
        row = translate_state(make_state(actionStates=[
            {"actionId": "a1", "actionType": "pick", "actionStatus": "RUNNING"}]))
        assert row["task_kind"] == "pick"
        # no running action but driving on an order -> transit
        assert translate_state(make_state())["task_kind"] == "transit"
        # idle, no order
        row = translate_state(make_state(driving=False, orderId=""))
        assert row["task_kind"] is None

    def test_health_penalties(self) -> None:
        assert translate_state(make_state(errors=[WARN_ERROR]))["health"] == 90.0
        assert translate_state(make_state(errors=[FATAL_ERROR]))["health"] == 60.0
        low = translate_state(make_state(
            batteryState={"batteryCharge": 15.0, "charging": False}))
        assert low["health"] == 90.0
        assert low["battery"] == 15.0

    def test_vendor_extensions_passthrough(self) -> None:
        row = translate_state(make_state(motorTemp=61.5, tasksDone=42))
        assert row["motor_temp"] == 61.5
        assert row["tasks_done"] == 42

    def test_missing_optional_blocks(self) -> None:
        msg = make_state()
        del msg["agvPosition"], msg["velocity"]
        row = translate_state(msg)
        assert row["pos"] is None
        assert row["speed"] == 0.0

    def test_missing_serial_raises(self) -> None:
        msg = make_state()
        del msg["serialNumber"]
        with pytest.raises(ValueError):
            translate_state(msg)


# ---------------------------------------------------------------------------
# AlertDeduper
# ---------------------------------------------------------------------------

class TestAlertDedup:
    def test_error_alert_shape(self) -> None:
        alerts = AlertDeduper().process(make_state(errors=[FATAL_ERROR]))
        assert len(alerts) == 1
        a = alerts[0]
        assert a["sev"] == "crit"
        assert a["src"] == "AGV-001"
        assert "gripperStalled" in a["msg"] and "Clear jam" in a["msg"]
        assert a["id"].startswith("al-agv-001-gripperstalled-")
        assert a["ack"] is False
        assert a["tlabel"] == "10:15:32"
        assert a["created_at"] == "2026-08-26T10:15:32.512Z"

    def test_same_error_not_realerted(self) -> None:
        d = AlertDeduper()
        assert len(d.process(make_state(errors=[FATAL_ERROR]))) == 1
        # same ongoing error in the next state message -> no new alert
        assert d.process(make_state(errors=[FATAL_ERROR])) == []
        # even with reworded description (identity = type + level)
        reworded = dict(FATAL_ERROR, errorDescription="different words")
        assert d.process(make_state(errors=[reworded])) == []

    def test_cleared_then_recurring_error_realerts_with_new_id(self) -> None:
        d = AlertDeduper()
        first = d.process(make_state(errors=[FATAL_ERROR]))
        assert d.process(make_state(errors=[])) == []  # cleared
        again = d.process(make_state(
            errors=[FATAL_ERROR], timestamp="2026-08-26T10:20:00.000Z"))
        assert len(again) == 1
        assert again[0]["id"] != first[0]["id"]

    def test_dedup_is_per_robot(self) -> None:
        d = AlertDeduper()
        assert len(d.process(make_state(errors=[FATAL_ERROR]))) == 1
        other = make_state(errors=[FATAL_ERROR], serialNumber="AGV-002")
        assert len(d.process(other)) == 1  # independent ledger

    def test_two_distinct_errors_two_alerts(self) -> None:
        alerts = AlertDeduper().process(make_state(errors=[FATAL_ERROR, WARN_ERROR]))
        assert {a["sev"] for a in alerts} == {"crit", "warn"}

    def test_battery_alert_and_hysteresis(self) -> None:
        d = AlertDeduper()

        def at(charge: float) -> list[dict]:
            return d.process(make_state(
                batteryState={"batteryCharge": charge, "charging": False}))

        assert at(50.0) == []
        low = at(18.0)
        assert len(low) == 1 and low[0]["sev"] == "warn"
        assert "18.0%" in low[0]["msg"]
        assert at(17.0) == []            # still low -> deduped
        assert at(22.0) == []            # inside hysteresis band -> not reset
        assert at(19.0) == []            # dips again -> still considered same episode
        assert at(30.0) == []            # recovered above threshold + hysteresis
        assert len(at(15.0)) == 1        # fresh episode -> new alert

    def test_battery_critical_severity(self) -> None:
        alerts = AlertDeduper().process(make_state(
            batteryState={"batteryCharge": 8.0, "charging": False}))
        assert len(alerts) == 1 and alerts[0]["sev"] == "crit"


# ---------------------------------------------------------------------------
# translate_connection -> robots row (or None)
# ---------------------------------------------------------------------------

class TestTranslateConnection:
    def test_offline_maps_to_fault_status_with_message(self) -> None:
        row = translate_connection({
            "serialNumber": "AGV-001", "connectionState": "OFFLINE",
            "timestamp": "2026-08-26T10:16:00.000Z",
        })
        assert row == {
            "id": "AGV-001",
            "status": "fault",
            "fault_msg": "MQTT connection closed (OFFLINE)",
            "updated_at": "2026-08-26T10:16:00.000Z",
        }

    def test_connectionbroken_maps_to_fault_status(self) -> None:
        row = translate_connection({
            "serialNumber": "AGV-002", "connectionState": "CONNECTIONBROKEN",
        })
        assert row["status"] == "fault"
        assert "CONNECTIONBROKEN" in row["fault_msg"] or "last-will" in row["fault_msg"]
        assert row["updated_at"]  # defaulted, not empty

    def test_online_returns_none(self) -> None:
        assert translate_connection({
            "serialNumber": "AGV-001", "connectionState": "ONLINE",
        }) is None

    def test_unknown_connection_state_returns_none(self) -> None:
        assert translate_connection({
            "serialNumber": "AGV-001", "connectionState": "SOMETHING_NEW",
        }) is None

    def test_missing_serial_raises(self) -> None:
        with pytest.raises(ValueError):
            translate_connection({"connectionState": "OFFLINE"})

    def test_row_only_has_columns_the_schema_already_has(self) -> None:
        # No new column invented -- upsert_robots must never be handed a
        # key the live `robots` table (supabase/0001_init.sql) can't take.
        row = translate_connection({
            "serialNumber": "AGV-001", "connectionState": "OFFLINE",
        })
        assert set(row) <= {
            "id", "vendor", "status", "battery", "pos", "speed", "task_kind",
            "health", "motor_temp", "tasks_done", "fault_msg", "updated_at",
        }


# ---------------------------------------------------------------------------
# Translator facade
# ---------------------------------------------------------------------------

class TestTranslator:
    def test_feed_many_last_write_wins_and_alert_accumulation(self) -> None:
        t = Translator()
        msgs = [
            make_state(),
            make_state(serialNumber="AGV-003",
                       batteryState={"batteryCharge": 14.0, "charging": False}),
            make_state(errors=[FATAL_ERROR], driving=False,
                       timestamp="2026-08-26T10:15:47.104Z"),
        ]
        robots, alerts = t.feed_many(msgs)
        assert len(robots) == 2  # AGV-001 appears once (last write wins)
        agv1 = next(r for r in robots if r["id"] == "AGV-001")
        assert agv1["status"] == "fault"
        assert agv1["updated_at"] == "2026-08-26T10:15:47.104Z"
        assert len(alerts) == 2  # battery(AGV-003) + fatal(AGV-001)
