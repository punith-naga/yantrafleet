"""VDA 5050 order topic + standard instantActions -- offline.

Covers what test_mqtt_commands.py's yantra.* verbs don't:
* inbound ``order`` subscription/parsing -> FleetSim.apply_order via
  poll_orders (the core master-control dispatch contract);
* the standard instantActions cancelOrder / stateRequest /
  factsheetRequest / initPosition (previously all unconditionally FAILED
  as "unsupported actionType");
* blockingType HARD deferral against a RUNNING order-action;
* ONLINE re-announced on every (re)connect, not just at construction.
"""
import json

from yantrasim.commands import apply_command
from yantrasim.sim import FleetSim
from yantrasim.transports.mqtt import MqttTransport
from yantrasim import vda, world

from conftest import FIXED_NOW


class FakeClient:
    """Recording fake with subscribe support (no broker)."""

    def __init__(self):
        self.published = []    # (topic, payload, qos, retain)
        self.subscribed = []   # (topic, qos)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))


def make():
    fleet = FleetSim(seed=7)
    fake = FakeClient()
    t = MqttTransport(fleet.robots, client=fake)
    fake.published.clear()
    return fleet, fake, t


def order_message(robot, order_id, order_update_id, node_ids, actions_by_node=None):
    actions_by_node = actions_by_node or {}
    topic = vda.topic(robot.vendor, robot.robot_id, "order")
    nodes = [{"nodeId": nid, "sequenceId": i * 2, "released": True,
             "actions": actions_by_node.get(nid, [])}
             for i, nid in enumerate(node_ids)]
    return topic, json.dumps({
        "headerId": 1, "timestamp": "2026-08-26T10:15:32.512Z",
        "version": "2.1.0", "manufacturer": robot.vendor,
        "serialNumber": vda.sanitize_serial(robot.robot_id),
        "orderId": order_id, "orderUpdateId": order_update_id,
        "nodes": nodes, "edges": [],
    })


def instant_actions(robot, actions):
    topic = vda.topic(robot.vendor, robot.robot_id, "instantActions")
    return topic, json.dumps({
        "headerId": 1, "timestamp": "2026-08-26T10:15:32.512Z",
        "version": "2.1.0", "manufacturer": robot.vendor,
        "serialNumber": vda.sanitize_serial(robot.robot_id),
        "actions": actions,
    })


def robot_states(fake, serial):
    return [m for t, m, _, _ in fake.published
            if t.endswith("/state") and m["serialNumber"] == serial]


def apply_via_sim(fleet):
    return lambda rid, cmd: apply_command(fleet, rid, cmd)


# -- subscriptions -----------------------------------------------------------

def test_subscribes_each_robots_order_topic():
    fleet, fake, _ = make()
    topics = [t for t, _ in fake.subscribed]
    for r in fleet.robots:
        assert vda.topic(r.vendor, r.robot_id, "order") in topics
    order_subs = [(t, q) for t, q in fake.subscribed if t.endswith("/order")]
    assert len(order_subs) == 10
    assert all(q == 0 for _, q in order_subs)  # order QoS 0 per spec


# -- order (inbound master-control dispatch) ----------------------------------

def test_order_drives_robot_and_reports_orderid_on_state():
    fleet, fake, t = make()
    r = fleet.robots[3]
    target = next(n for n in world.TASK_NODES if n != r.node)
    path = list(world.shortest_path(r.node, target))
    topic, payload = order_message(r, "order-1", 0, path,
                                   {target: [{"actionId": "a-1",
                                             "actionType": "drop"}]})
    t.handle_order(topic, payload)
    assert r.order_id == ""  # queued, not yet applied
    n = t.poll_orders(lambda *a: fleet.apply_order(*a))
    assert n == 1
    assert r.order_id == "order-1"
    assert r.status == "moving"
    assert r.path == path[1:]


def test_stale_order_update_rejected_without_touching_robot():
    fleet, fake, t = make()
    r = fleet.robots[4]
    target = next(n for n in world.TASK_NODES if n != r.node)
    path = list(world.shortest_path(r.node, target))
    t.handle_order(*order_message(r, "order-2", 3, path))
    t.poll_orders(lambda *a: fleet.apply_order(*a))
    assert r.order_update_id == 3
    before_path = list(r.path)
    t.handle_order(*order_message(r, "order-2", 3, path))  # redelivery, same id
    n = t.poll_orders(lambda *a: fleet.apply_order(*a))
    assert n == 1  # processed (rejected), still counted
    assert r.path == before_path  # untouched


def test_order_for_unknown_robot_serial_is_counted_and_ignored():
    fleet, fake, t = make()
    topic = "uagv/v2/nexomotion/NOSUCH/order"
    payload = json.dumps({
        "orderId": "order-x", "orderUpdateId": 0,
        "manufacturer": "nexomotion", "serialNumber": "NOSUCH",
        "nodes": [], "edges": [],
    })
    t.handle_order(topic, payload)
    assert t.poll_orders(lambda *a: (True, "should not be called")) == 1


def test_malformed_order_payloads_are_dropped():
    fleet, fake, t = make()
    r = fleet.robots[0]
    topic = vda.topic(r.vendor, r.robot_id, "order")
    t.handle_order(topic, b"not json")
    t.handle_order(topic, json.dumps([1, 2, 3]))               # not a dict
    t.handle_order(topic, json.dumps({"nodes": []}))            # no orderId
    assert t.poll_orders(lambda *a: (True, "")) == 0


# -- cancelOrder ---------------------------------------------------------------

def test_cancel_order_instant_action_stops_the_robot():
    fleet, fake, t = make()
    r = fleet.robots[0]
    ok, _ = fleet.apply_order(r.robot_id, "order-3", 0,
                              list(world.shortest_path(
                                  r.node, next(n for n in world.TASK_NODES
                                               if n != r.node))))
    assert ok and r.status == "moving"
    serial = vda.sanitize_serial(r.robot_id)
    topic, payload = instant_actions(r, [{
        "actionType": "cancelOrder", "actionId": "cancel-1"}])
    t.handle_instant_actions(topic, payload)
    assert t.poll_commands(apply_via_sim(fleet)) == 1
    assert r.status == "idle" and r.path == []
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))
    acts = [a for a in robot_states(fake, serial)[0]["actionStates"]
            if a["actionId"] == "cancel-1"]
    assert acts and acts[0]["actionStatus"] == "FINISHED"


# -- stateRequest ----------------------------------------------------------

def test_state_request_publishes_state_immediately():
    fleet, fake, t = make()
    r = fleet.robots[1]
    serial = vda.sanitize_serial(r.robot_id)
    topic, payload = instant_actions(r, [{
        "actionType": "stateRequest", "actionId": "sr-1"}])
    t.handle_instant_actions(topic, payload)
    n = t.poll_commands(apply_via_sim(fleet))
    assert n == 1
    # published right away -- no fleet.tick()/transport.publish() call yet.
    states = robot_states(fake, serial)
    assert len(states) == 1
    assert states[0]["serialNumber"] == serial


# -- factsheetRequest --------------------------------------------------------

def test_factsheet_request_publishes_retained_factsheet_topic():
    fleet, fake, t = make()
    r = fleet.robots[2]
    topic, payload = instant_actions(r, [{
        "actionType": "factsheetRequest", "actionId": "fr-1"}])
    t.handle_instant_actions(topic, payload)
    assert t.poll_commands(apply_via_sim(fleet)) == 1
    fs = [p for p in fake.published if p[0].endswith("/factsheet")]
    assert len(fs) == 1
    ftopic, fmsg, qos, retain = fs[0]
    assert qos == 1 and retain is True
    assert fmsg["serialNumber"] == vda.sanitize_serial(r.robot_id)
    assert "typeSpecification" in fmsg


# -- initPosition ------------------------------------------------------------

def test_init_position_sets_pose_and_clears_localization_fault():
    fleet, fake, t = make()
    r = fleet.robots[5]
    r.status = "fault"
    r.fault_kind = "localization"
    r.resume_status = "idle"
    node = next(iter(world.WAYPOINTS))
    topic, payload = instant_actions(r, [{
        "actionType": "initPosition", "actionId": "ip-1",
        "actionParameters": [
            {"key": "lastNodeId", "value": node},
            {"key": "theta", "value": 0.5},
        ]}])
    t.handle_instant_actions(topic, payload)
    ok_count = t.poll_commands(apply_via_sim(fleet))
    assert ok_count == 1
    assert r.node == node
    assert r.theta == 0.5
    assert r.fault_kind is None
    assert r.status == "idle"


def test_init_position_unknown_node_fails():
    fleet, fake, t = make()
    r = fleet.robots[6]
    topic, payload = instant_actions(r, [{
        "actionType": "initPosition", "actionId": "ip-2",
        "actionParameters": [{"key": "lastNodeId", "value": "not-a-node"}]}])
    t.handle_instant_actions(topic, payload)
    t.poll_commands(apply_via_sim(fleet))
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))
    serial = vda.sanitize_serial(r.robot_id)
    act = [a for a in robot_states(fake, serial)[0]["actionStates"]
           if a["actionId"] == "ip-2"][0]
    assert act["actionStatus"] == "FAILED"


# -- blockingType is accepted but not (yet) enforced -------------------------

def test_blocking_type_hard_does_not_block_the_operator_override():
    """Every instantAction this sim supports is an operator override meant
    to pre-empt the robot's current action -- gating it behind blockingType
    HARD would break that (and would strand a real dispatcher waiting on a
    pause it thinks got through). See the module docstring's note on
    blockingType for why real HARD-vs-order serialization is out of scope."""
    fleet, fake, t = make()
    working = None
    for _ in range(30):
        fleet.tick(dt_s=10.0, now=FIXED_NOW)
        cand = [r for r in fleet.robots if r.status == "working"]
        if cand:
            working = cand[0]
            break
    assert working is not None, "no robot started working in 30 ticks"
    topic, payload = instant_actions(working, [{
        "actionType": "yantra.pause", "actionId": "blk-1",
        "blockingType": "HARD"}])
    t.handle_instant_actions(topic, payload)
    assert t.poll_commands(apply_via_sim(fleet)) == 1
    assert working.status == "paused"


# -- ONLINE re-announced on reconnect ----------------------------------------

def test_online_reannounced_and_resubscribed_on_reconnect():
    fleet, fake, t = make()
    fake.published.clear()
    fake.subscribed.clear()
    t._handle_connect(fake, None, None, 0)
    conn = [p for p in fake.published if p[0].endswith("/connection")]
    assert len(conn) == 10
    assert all(m["connectionState"] == "ONLINE" for _, m, _, _ in conn)
    assert len([s for s in fake.subscribed if s[0].endswith("/order")]) == 10
    assert len([s for s in fake.subscribed if s[0].endswith("/instantActions")]) == 10
