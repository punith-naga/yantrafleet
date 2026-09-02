"""MQTT instantActions handling + actionStates emission — offline.

The command path in --mqtt mode: an instantActions message arrives on a
robot's topic (queued on the MQTT thread), poll_commands drains it on
the sim thread and applies the yantra.* verb, and the result rides back
as an actionStates entry in the robot's next few state messages.
"""
import json

from yantrasim.commands import apply_command
from yantrasim.sim import FleetSim
from yantrasim.transports.mqtt import ACTION_STATE_REPEATS, MqttTransport
from yantrasim import vda

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


def test_subscribes_each_robots_instant_actions_topic():
    fleet, fake, _ = make()
    topics = [t for t, _ in fake.subscribed]
    assert len(topics) == 10
    for r in fleet.robots:
        assert vda.topic(r.vendor, r.robot_id, "instantActions") in topics
    assert all(q == 0 for _, q in fake.subscribed)  # instantActions QoS 0


def test_pause_action_applied_via_poll_commands():
    fleet, fake, t = make()
    r = fleet.robots[0]
    assert r.status not in ("paused", "fault")
    topic, payload = instant_actions(r, [{
        "actionType": "yantra.pause", "actionId": "cmd-1",
        "blockingType": "HARD"}])
    t.handle_instant_actions(topic, payload)
    # queued, not yet applied (MQTT thread only queues)
    assert r.status != "paused"
    n = t.poll_commands(lambda rid, cmd: apply_command(fleet, rid, cmd))
    assert n == 1
    assert r.status == "paused"
    assert r.speed == 0.0


def test_action_state_finished_rides_next_state_message():
    fleet, fake, t = make()
    r = fleet.robots[0]
    serial = vda.sanitize_serial(r.robot_id)
    topic, payload = instant_actions(r, [{
        "actionType": "yantra.pause", "actionId": "cmd-2"}])
    t.handle_instant_actions(topic, payload)
    t.poll_commands(lambda rid, cmd: apply_command(fleet, rid, cmd))
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))
    states = robot_states(fake, serial)
    assert len(states) == 1
    acts = [a for a in states[0]["actionStates"] if a["actionId"] == "cmd-2"]
    assert len(acts) == 1
    assert acts[0]["actionStatus"] == "FINISHED"
    assert acts[0]["actionType"] == "yantra.pause"
    assert "paused" in acts[0]["resultDescription"]
    # other robots' states are untouched
    for tpc, msg, _, _ in fake.published:
        if tpc.endswith("/state") and msg["serialNumber"] != serial:
            assert all(a.get("actionId") != "cmd-2"
                       for a in msg.get("actionStates", []))


def test_failed_verb_reports_failed_action_state():
    fleet, fake, t = make()
    r = fleet.robots[1]
    serial = vda.sanitize_serial(r.robot_id)
    # resume a robot that is not held -> commands.apply_command fails
    assert r.status not in ("paused", "estopped")
    topic, payload = instant_actions(r, [{
        "actionType": "yantra.resume", "actionId": "cmd-3"}])
    t.handle_instant_actions(topic, payload)
    assert t.poll_commands(lambda rid, cmd: apply_command(fleet, rid, cmd)) == 1
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))
    act = [a for a in robot_states(fake, serial)[0]["actionStates"]
           if a["actionId"] == "cmd-3"][0]
    assert act["actionStatus"] == "FAILED"
    assert "not held" in act["resultDescription"]


def test_unknown_action_type_fails_without_touching_robot():
    fleet, fake, t = make()
    r = fleet.robots[2]
    before = r.status
    topic, payload = instant_actions(r, [{
        "actionType": "startPause", "actionId": "cmd-4"}])  # spec verb, not ours
    t.handle_instant_actions(topic, payload)
    calls = []
    assert t.poll_commands(lambda rid, cmd: calls.append((rid, cmd))) == 1
    assert calls == []          # apply_fn never invoked
    assert r.status == before
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))
    serial = vda.sanitize_serial(r.robot_id)
    act = [a for a in robot_states(fake, serial)[0]["actionStates"]
           if a["actionId"] == "cmd-4"][0]
    assert act["actionStatus"] == "FAILED"
    assert "unsupported actionType" in act["resultDescription"]


def test_duplicate_action_id_applied_once():
    fleet, fake, t = make()
    r = fleet.robots[0]
    topic, payload = instant_actions(r, [{
        "actionType": "yantra.estop", "actionId": "cmd-5"}])
    t.handle_instant_actions(topic, payload)
    t.handle_instant_actions(topic, payload)  # broker redelivery
    calls = []

    def apply(rid, cmd):
        calls.append((rid, cmd))
        return apply_command(fleet, rid, cmd)

    assert t.poll_commands(apply) == 1
    assert calls == [(r.robot_id, "estop")]
    assert r.status == "estopped"


def test_action_state_repeats_then_expires():
    fleet, fake, t = make()
    r = fleet.robots[0]
    serial = vda.sanitize_serial(r.robot_id)
    topic, payload = instant_actions(r, [{
        "actionType": "yantra.pause", "actionId": "cmd-6"}])
    t.handle_instant_actions(topic, payload)
    t.poll_commands(lambda rid, cmd: apply_command(fleet, rid, cmd))
    for _ in range(ACTION_STATE_REPEATS + 2):
        t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))
    states = robot_states(fake, serial)
    carrying = [s for s in states
                if any(a["actionId"] == "cmd-6"
                       for a in s.get("actionStates", []))]
    assert len(carrying) == ACTION_STATE_REPEATS
    # the repeats are the FIRST N states after execution, then it expires
    assert carrying == states[:ACTION_STATE_REPEATS]


def test_malformed_payloads_are_dropped():
    fleet, fake, t = make()
    r = fleet.robots[0]
    topic = vda.topic(r.vendor, r.robot_id, "instantActions")
    t.handle_instant_actions(topic, b"not json")
    t.handle_instant_actions(topic, json.dumps([1, 2, 3]))          # not a dict
    t.handle_instant_actions(topic, json.dumps({"actions": [
        {"actionType": "yantra.pause"},                             # no actionId
        "nonsense",                                                 # not a dict
    ]}))
    assert t.poll_commands(lambda rid, cmd: (True, "")) == 0


def test_existing_task_action_state_preserved():
    """Command actionStates append to, never replace, task actionStates."""
    fleet, fake, t = make()
    # run a few ticks so some robot is 'working' with a RUNNING action
    out = None
    for _ in range(12):
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
        working = [s for s in out.states
                   if any(a["actionStatus"] == "RUNNING"
                          for a in s["actionStates"])]
        if working:
            break
    assert working, "no robot ever started working in 12 ticks"
    serial = working[0]["serialNumber"]
    robot = next(r for r in fleet.robots
                 if vda.sanitize_serial(r.robot_id) == serial)
    topic, payload = instant_actions(robot, [{
        "actionType": "yantra.estop", "actionId": "cmd-7"}])
    t.handle_instant_actions(topic, payload)
    t.poll_commands(lambda rid, cmd: (True, "ok"))  # don't disturb the sim
    fake.published.clear()
    t.publish(out)  # republish the tick where the robot was working
    acts = robot_states(fake, serial)[0]["actionStates"]
    statuses = {a["actionId"]: a["actionStatus"] for a in acts}
    assert statuses.get("cmd-7") == "FINISHED"
    assert "RUNNING" in statuses.values()  # the task action survived
