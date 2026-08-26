"""VDA 5050 v2.1 compliance of emitted state messages."""
import math

from yantrasim import vda
from yantrasim.sim import FleetSim

from conftest import FIXED_NOW

REQUIRED_STATE_FIELDS = (
    "headerId", "timestamp", "version", "manufacturer", "serialNumber",
    "orderId", "orderUpdateId", "lastNodeId", "lastNodeSequenceId",
    "nodeStates", "edgeStates", "driving", "actionStates", "batteryState",
    "operatingMode", "errors", "safetyState",
)


def test_required_fields_present(one_tick):
    for s in one_tick.states:
        for f in REQUIRED_STATE_FIELDS:
            assert f in s, f"missing {f}"
        assert s["version"] == "2.1.0"
        assert isinstance(s["errors"], list)
        assert s["operatingMode"] in (
            "AUTOMATIC", "SEMIAUTOMATIC", "MANUAL", "SERVICE", "TEACHIN")
        assert s["safetyState"]["eStop"] in ("AUTOACK", "MANUAL", "REMOTE", "NONE")
        assert isinstance(s["safetyState"]["fieldViolation"], bool)
        bs = s["batteryState"]
        assert 0.0 <= bs["batteryCharge"] <= 100.0
        assert isinstance(bs["charging"], bool)


def test_header_id_increments_per_robot(sim):
    o1 = sim.tick(dt_s=10.0, now=FIXED_NOW)
    o2 = sim.tick(dt_s=10.0, now=FIXED_NOW)
    by_serial_1 = {s["serialNumber"]: s["headerId"] for s in o1.states}
    for s in o2.states:
        assert s["headerId"] == by_serial_1[s["serialNumber"]] + 1


def test_timestamp_iso_utc_ms(one_tick):
    ts = one_tick.states[0]["timestamp"]
    assert ts == "2026-08-26T10:15:32.512Z"


def test_serial_number_charset_sanitized(one_tick):
    for s in one_tick.states:
        serial = s["serialNumber"]
        assert all(c.isalnum() or c in "_.:" for c in serial)
    assert vda.sanitize_serial("AMR-07") == "AMR_07"


def test_topic_structure():
    t = vda.topic("nexomotion", "AMR-01", "state")
    assert t == "uagv/v2/nexomotion/AMR_01/state"
    for seg in t.split("/"):
        assert "$" not in seg
    # manufacturer segment sanitized too
    assert vda.topic("bad/vendor$x", "A", "state").split("/")[2] == "bad_vendor_x"


def test_theta_wrapped_to_pi(sim):
    for _ in range(50):
        out = sim.tick(dt_s=10.0, now=FIXED_NOW)
        for s in out.states:
            th = s["agvPosition"]["theta"]
            assert -math.pi - 1e-6 <= th <= math.pi + 1e-6


def test_errors_empty_when_healthy_and_populated_on_fault():
    fleet = FleetSim(seed=8)
    out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
    healthy = [s for s in out.states if s["serialNumber"] != "AMR_07"]
    assert all(s["errors"] == [] for s in healthy if not s["errors"])
    # Drive to the scripted fault tick.
    from yantrasim.sim import SCRIPTED_FAULT_TICK
    while fleet.tick_count < SCRIPTED_FAULT_TICK:
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
    s07 = next(s for s in out.states if s["serialNumber"] == "AMR_07")
    assert len(s07["errors"]) == 1
    err = s07["errors"][0]
    assert err["errorType"] == "localizationError"
    assert err["errorLevel"] == "FATAL"
    assert err["errorHint"]  # 2.1 field
    assert err["errorReferences"][0]["referenceValue"] == "AMR_07"
    # Localization fault => position no longer initialized, not driving.
    assert s07["agvPosition"]["positionInitialized"] is False
    assert s07["driving"] is False


def test_node_edge_states_alternate_sequence_ids(sim):
    # Find a robot with a path (drive a few ticks).
    for _ in range(5):
        out = sim.tick(dt_s=10.0, now=FIXED_NOW)
        for s in out.states:
            if s["nodeStates"]:
                # edges odd, nodes even, interleaved and increasing
                ids = []
                for e, n in zip(s["edgeStates"], s["nodeStates"]):
                    assert e["sequenceId"] % 2 == 1
                    assert n["sequenceId"] % 2 == 0
                    ids += [e["sequenceId"], n["sequenceId"]]
                assert ids == sorted(ids)
                assert all(n["released"] for n in s["nodeStates"])
                return
    raise AssertionError("no robot ever had a pending path")


def test_connection_message():
    fleet = FleetSim(seed=2)
    r = fleet.robots[0]
    msg = vda.build_connection(r, 1, "2026-08-26T10:00:00.000Z", "ONLINE")
    assert msg["connectionState"] == "ONLINE"
    assert msg["serialNumber"] == vda.sanitize_serial(r.robot_id)
    assert msg["manufacturer"] == r.vendor
    assert msg["version"] == "2.1.0"
