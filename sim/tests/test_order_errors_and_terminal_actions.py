"""VDA 5050 2.1 order/action reporting duties the simulator used to skip.

Two spec obligations, both about telling master control something rather
than just doing the right thing internally:

6.5 -- rejecting an order update is not enough. There is NO ack topic in
      the order flow, so a discarded update has to be reported in band, as
      an ``errors[]`` entry of errorType ``orderUpdateError`` (WARNING) on
      the next state messages. Without it master control cannot tell "your
      update was dropped" from "your update was applied and changed
      nothing". An update carrying the SAME orderUpdateId is a duplicate
      (exactly what MQTT redelivery produces) and is discarded silently --
      raising a warning there would make every redelivery look like a
      fault.

6.9 -- an action that stops being reported must have reached FINISHED or
      FAILED first, under the SAME actionId it ran with. A superseding
      order, a cancelOrder or a battery divert all abandon the action in
      flight; each must drive it to FAILED rather than letting it vanish.

All offline: FleetSim/MqttTransport are driven directly against a
recording fake client. See test_mqtt_real_broker_order_errors.py for the
same 6.5 duty proved over a real broker.
"""
from __future__ import annotations

import json

from yantrasim import vda, world
from yantrasim.commands import apply_command
from yantrasim.sim import ORDER_ERROR_STATE_REPEATS, FleetSim
from yantrasim.transports.mqtt import MqttTransport

from conftest import FIXED_NOW
from test_mqtt_order_and_actions import FakeClient, order_message, robot_states


# -- helpers -----------------------------------------------------------------

def state_of(fleet: FleetSim, robot) -> dict:
    """Tick once and return that robot's outgoing VDA state message."""
    out = fleet.tick(dt_s=0.01, now=FIXED_NOW)
    serial = vda.sanitize_serial(robot.robot_id)
    return next(s for s in out.states if s["serialNumber"] == serial)


def order_errors(state: dict) -> list[dict]:
    return [e for e in state.get("errors", [])
            if e.get("errorType") == vda.ORDER_UPDATE_ERROR]


def action_entry(state: dict, action_id: str) -> dict | None:
    for a in state.get("actionStates", []):
        if a.get("actionId") == action_id:
            return a
    return None


def working_on_order(fleet: FleetSim, robot, order_id: str, update_id: int,
                     action_id: str, action_type: str = "pick") -> None:
    """Put ``robot`` into 'working' under an order, with no travel involved.

    An order whose only node is the node the robot is already standing on
    has an empty path, so the robot arrives immediately and starts the
    order's action -- no ticking, and therefore no chance for a random
    fault or a battery divert to make the test flaky.
    """
    ok, detail = fleet.apply_order(
        robot.robot_id, order_id, update_id, [robot.node],
        actions=[{"actionId": action_id, "actionType": action_type}])
    assert ok, detail
    assert robot.status == "working", robot.status


# ---------------------------------------------------------------------------
# 6.5 -- a discarded order update is reported as an orderUpdateError
# ---------------------------------------------------------------------------

def test_stale_order_update_raises_order_update_error_in_state():
    fleet = FleetSim(seed=21)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-A", 5, "act-A")

    ok, detail = fleet.apply_order(r.robot_id, "order-A", 3, [r.node])
    assert not ok and "stale" in detail

    s = state_of(fleet, r)
    errs = order_errors(s)
    assert len(errs) == 1, s["errors"]
    err = errs[0]
    assert err["errorLevel"] == "WARNING"        # not FATAL: the AGV runs on
    assert "3" in err["errorDescription"]
    assert "order-A" in err["errorDescription"]
    refs = {ref["referenceKey"]: ref["referenceValue"]
            for ref in err["errorReferences"]}
    assert refs["orderId"] == "order-A"
    assert refs["orderUpdateId"] == "3"          # spec: reference values are strings
    assert refs["serialNumber"] == vda.sanitize_serial(r.robot_id)
    # The order itself was NOT rewound by the rejected update.
    assert s["orderId"] == "order-A" and s["orderUpdateId"] == 5


def test_order_update_error_repeats_then_expires():
    """State is QoS 0, so reporting the rejection once is a coin flip."""
    fleet = FleetSim(seed=22)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-B", 4, "act-B")
    assert not fleet.apply_order(r.robot_id, "order-B", 1, [r.node])[0]

    for i in range(ORDER_ERROR_STATE_REPEATS):
        assert order_errors(state_of(fleet, r)), f"missing on repeat {i}"
    assert not order_errors(state_of(fleet, r))


def test_duplicate_order_update_is_discarded_without_an_error():
    """MQTT redelivery replays an IDENTICAL order; 6.5 discards it quietly.

    Guards the other half of the rule: an orderUpdateError on every
    redelivery would flip the vehicle to 'degraded' on the dashboard and
    raise an operator alert for a message that changed nothing.
    """
    fleet = FleetSim(seed=23)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-C", 2, "act-C")

    ok, detail = fleet.apply_order(r.robot_id, "order-C", 2, [r.node])
    assert not ok and "stale" in detail
    assert order_errors(state_of(fleet, r)) == []


def test_order_update_error_survives_to_the_wire_through_the_transport():
    """The end the fleet manager actually sees: a published state message."""
    fleet = FleetSim(seed=24)
    fake = FakeClient()
    t = MqttTransport(fleet.robots, client=fake)
    r = fleet.robots[2]
    serial = vda.sanitize_serial(r.robot_id)
    working_on_order(fleet, r, "order-D", 7, "act-D")

    t.handle_order(*order_message(r, "order-D", 6, [r.node]))
    assert t.poll_orders(lambda *a: fleet.apply_order(*a)) == 1

    fake.published.clear()
    t.publish(fleet.tick(dt_s=0.01, now=FIXED_NOW))
    published = robot_states(fake, serial)
    assert published, "no state published for the robot"
    assert order_errors(published[0]), published[0]["errors"]


def test_order_update_error_also_rides_a_state_request_response():
    """A stateRequest-forced state must carry the same evidence a tick would.

    Otherwise what master control learns depends on which of the two
    publishers happened to get there first.
    """
    fleet = FleetSim(seed=25)
    fake = FakeClient()
    t = MqttTransport(fleet.robots, client=fake)
    r = fleet.robots[1]
    serial = vda.sanitize_serial(r.robot_id)
    working_on_order(fleet, r, "order-E", 9, "act-E")
    assert not fleet.apply_order(r.robot_id, "order-E", 2, [r.node])[0]

    fake.published.clear()
    ok, _ = t._handle_state_request(serial)
    assert ok
    published = robot_states(fake, serial)
    assert len(published) == 1
    assert order_errors(published[0]), published[0]["errors"]


def test_rejected_new_order_for_an_unknown_node_raises_no_update_error():
    """orderUpdateError is about UPDATES; an unexecutable new order is not one."""
    fleet = FleetSim(seed=26)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-F", 1, "act-F")
    ok, detail = fleet.apply_order(r.robot_id, "order-G", 0, ["no-such-node"])
    assert not ok and "valid on this map" in detail
    assert order_errors(state_of(fleet, r)) == []


# ---------------------------------------------------------------------------
# 6.9 -- abandoned actions reach a terminal status
# ---------------------------------------------------------------------------

def test_superseding_order_fails_the_outgoing_order_action():
    fleet = FleetSim(seed=31)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-old", 0, "act-old")
    assert action_entry(state_of(fleet, r), "act-old")["actionStatus"] == "RUNNING"

    ok, detail = fleet.apply_order(
        r.robot_id, "order-new", 0, [r.node],
        actions=[{"actionId": "act-new", "actionType": "drop"}])
    assert ok, detail

    s = state_of(fleet, r)
    old = action_entry(s, "act-old")
    assert old is not None, "the superseded action vanished without a status"
    assert old["actionStatus"] == "FAILED"
    assert "superseded by order order-new" in old["resultDescription"]
    assert old["actionType"] == "pick"          # what it WAS, not the new one
    # ...and the replacement is running under its own id, exactly once.
    assert action_entry(s, "act-new")["actionStatus"] == "RUNNING"
    ids = [a["actionId"] for a in s["actionStates"]]
    assert len(ids) == len(set(ids)), ids


def test_superseding_update_to_the_same_order_also_fails_the_dropped_action():
    """An update replaces this simulator's whole task, so its action is gone."""
    fleet = FleetSim(seed=32)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-u", 0, "act-u")
    ok, detail = fleet.apply_order(r.robot_id, "order-u", 1, [r.node])
    assert ok, detail

    old = action_entry(state_of(fleet, r), "act-u")
    assert old is not None and old["actionStatus"] == "FAILED"
    assert "update 1" in old["resultDescription"]


def test_an_order_restating_the_same_action_does_not_fail_it():
    """The action carries on; it was never abandoned, so it must not FAIL."""
    fleet = FleetSim(seed=33)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-r", 0, "act-r")
    ok, _ = fleet.apply_order(
        r.robot_id, "order-r", 1, [r.node],
        actions=[{"actionId": "act-r", "actionType": "pick"}])
    assert ok

    entry = action_entry(state_of(fleet, r), "act-r")
    assert entry["actionStatus"] == "RUNNING"


def test_cancel_order_fails_the_running_order_action():
    fleet = FleetSim(seed=34)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-x", 0, "act-x")

    ok, detail = apply_command(fleet, r.robot_id, "cancel_order")
    assert ok, detail
    entry = action_entry(state_of(fleet, r), "act-x")
    assert entry is not None, "cancelOrder dropped the action without a status"
    assert entry["actionStatus"] == "FAILED"
    assert "cancelled" in entry["resultDescription"]


def test_charge_divert_fails_the_running_order_action():
    fleet = FleetSim(seed=35)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-y", 0, "act-y")

    ok, detail = apply_command(fleet, r.robot_id, "charge")
    assert ok, detail
    entry = action_entry(state_of(fleet, r), "act-y")
    assert entry is not None and entry["actionStatus"] == "FAILED"
    assert "charger" in entry["resultDescription"]


def test_resume_that_discards_the_held_task_fails_its_action():
    fleet = FleetSim(seed=36)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-z", 0, "act-z")
    assert apply_command(fleet, r.robot_id, "pause")[0]
    assert apply_command(fleet, r.robot_id, "resume")[0]

    entry = action_entry(state_of(fleet, r), "act-z")
    assert entry is not None and entry["actionStatus"] == "FAILED"


def test_an_abandoned_actions_generated_id_is_never_reused():
    """A FAILED action must not appear to go back to RUNNING (6.9).

    The generated id used to be derived from ``tasks_done``, which an
    abandoned task never increments -- so the next task inherited the id of
    the action that had just reported FAILED.
    """
    fleet = FleetSim(seed=37)
    r = fleet.robots[0]
    working_on_order(fleet, r, "order-1", 0, "act-1")
    assert apply_command(fleet, r.robot_id, "cancel_order")[0]

    first = fleet._new_generated_action_id(r)
    second = fleet._new_generated_action_id(r)
    assert first != second

    # ...and the sim's own task assignment mints, rather than reuses.
    r.status = "idle"
    fleet._assign_task(r)
    assert r.current_action_id not in ("act-1", first, second)


def test_internal_task_reports_one_id_from_running_to_finished():
    """An action must not run under one id and finish under another."""
    fleet = FleetSim(seed=38)
    r = fleet.robots[0]
    r.status = "idle"
    fleet._assign_task(r)
    r.path = []
    fleet._arrive(r)
    assert r.status == "working"

    running = state_of(fleet, r)["actionStates"]
    assert len(running) == 1 and running[0]["actionStatus"] == "RUNNING"
    action_id = running[0]["actionId"]

    r.work_left_s = 0.0001
    s = state_of(fleet, r)
    entry = action_entry(s, action_id)
    assert entry is not None, [a["actionId"] for a in s["actionStates"]]
    assert entry["actionStatus"] == "FINISHED"


# ---------------------------------------------------------------------------
# actionStates is a map, not a log
# ---------------------------------------------------------------------------

def test_merge_action_states_replaces_rather_than_appends():
    merged = vda.merge_action_states(
        [{"actionId": "a", "actionStatus": "RUNNING"},
         {"actionId": "b", "actionStatus": "RUNNING"}],
        [{"actionId": "a", "actionStatus": "FAILED"}])
    assert [m["actionId"] for m in merged] == ["a", "b"]
    assert merged[0]["actionStatus"] == "FAILED"


def test_order_topic_rejection_is_still_counted_by_poll_orders():
    """Reporting the error must not change poll_orders' processed count."""
    fleet = FleetSim(seed=39)
    fake = FakeClient()
    t = MqttTransport(fleet.robots, client=fake)
    r = fleet.robots[5]
    target = next(n for n in world.TASK_NODES if n != r.node)
    path = list(world.shortest_path(r.node, target))
    t.handle_order(*order_message(r, "order-count", 4, path))
    assert t.poll_orders(lambda *a: fleet.apply_order(*a)) == 1
    t.handle_order(*order_message(r, "order-count", 2, path))
    assert t.poll_orders(lambda *a: fleet.apply_order(*a)) == 1
    assert r.order_update_id == 4
    assert json.loads(json.dumps(r.pending_errors))  # JSON-serializable payload
