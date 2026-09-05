"""The orderUpdateError duty (VDA 5050 2.1 6.5), proved over a real broker.

Why this file exists at all, given the offline tests next to it: the
conformance tester's ``order.reject_stale_reports_error`` finding is the
ONE finding its in-process loopback run cannot reach. Running the whole
simulator loop synchronously means the simulator's own dispatcher always
overtakes the tester's order before the stale replay lands, so the replay
is a NEW order rather than a stale update and the check honestly reports
"unevaluable". Only a real broker, with the vehicle publishing on its own
thread at its own rate, reproduces the timing under which the gap shows.

So this test is the missing half: a real mosquitto over TCP, the shipped
``MqttTransport`` on a real paho connection, and a SEPARATE paho client
playing master control -- publishing an order, waiting for the vehicle to
echo it, replaying a lower orderUpdateId, and requiring an
``orderUpdateError`` to come back on the state topic.

It is the only networked test in this suite (everything else here is
strictly offline), it only ever talks to a private mosquitto this module
starts on 127.0.0.1, and it skips rather than fails when mosquitto or paho
is unavailable -- a stranger's checkout must not go red for lacking a
broker. Point it at an existing broker with::

    YANTRASIM_TEST_BROKER=localhost:1883 python3 -m pytest sim/tests
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest

from yantrasim import vda, world
from yantrasim.sim import FleetSim
from yantrasim.transports.mqtt import PAHO_AVAILABLE, MqttTransport

#: How long to wait for a vehicle publishing at its own rate to answer.
RESPONSE_TIMEOUT_S = 15.0
#: Wall-clock seconds per simulator tick, and simulated seconds per tick.
#: A SHORT sim step keeps the robot moving (rather than arriving, going
#: idle and dispatching itself a fresh order) for the whole probe window.
TICK_PERIOD_S = 0.05
TICK_DT_S = 0.1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _reachable(host: str, port: int, timeout: float = 0.75) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def broker():
    """A real MQTT broker, or a skip."""
    if not PAHO_AVAILABLE:
        pytest.skip("paho-mqtt is not installed")
    configured = os.environ.get("YANTRASIM_TEST_BROKER")
    if configured:
        host, _, port_text = configured.partition(":")
        port = int(port_text or 1883)
        if not _reachable(host, port):
            pytest.skip(f"YANTRASIM_TEST_BROKER={configured} is not reachable")
        yield host, port
        return

    mosquitto = shutil.which("mosquitto")
    if not mosquitto:
        pytest.skip("no MQTT broker: install mosquitto, or set "
                    "YANTRASIM_TEST_BROKER=host:port")
    port = _free_port()
    proc = subprocess.Popen([mosquitto, "-p", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not _reachable("127.0.0.1", port):
            if proc.poll() is not None:
                pytest.skip("mosquitto exited immediately")
            time.sleep(0.1)
        if not _reachable("127.0.0.1", port):
            pytest.skip("mosquitto did not start listening in time")
        yield "127.0.0.1", port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


class _SimThread:
    """One robot on a real broker, driven exactly like ``--mqtt`` does."""

    def __init__(self, host: str, port: int) -> None:
        self.sim = FleetSim(seed=42)
        self.sim.robots = self.sim.robots[:1]
        self.robot = self.sim.robots[0]
        self.transport = MqttTransport(self.sim.robots, broker=host, port=port)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            out = self.sim.tick(dt_s=TICK_DT_S)
            self.transport.publish(out)
            self.transport.poll_orders(
                lambda rid, oid, ouid, nodes, actions:
                    self.sim.apply_order(rid, oid, ouid, nodes, actions))
            self._stop.wait(TICK_PERIOD_S)

    def __enter__(self) -> "_SimThread":
        self._thread.start()
        time.sleep(0.3)  # let ONLINE + the first states land
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.transport.close()


class _MasterControl:
    """A second, independent paho client: publishes orders, reads state."""

    def __init__(self, host: str, port: int, robot) -> None:
        import paho.mqtt.client as paho

        self.states: list[dict] = []
        self._lock = threading.Lock()
        self._header_id = 0
        self.serial = vda.sanitize_serial(robot.robot_id)
        self.manufacturer = robot.vendor
        self.state_topic = vda.topic(robot.vendor, robot.robot_id, "state")
        self.order_topic = vda.topic(robot.vendor, robot.robot_id, "order")
        self.client = paho.Client(
            callback_api_version=paho.CallbackAPIVersion.VERSION2,
            protocol=paho.MQTTv311, client_id="yantrasim-test-master")
        self.client.on_message = self._on_message
        self.client.connect(host, port)
        self.client.loop_start()
        self.client.subscribe(self.state_topic, qos=1)

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload)
        except (UnicodeDecodeError, ValueError):  # pragma: no cover
            return
        with self._lock:
            self.states.append(payload)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def publish_order(self, order_id: str, update_id: int,
                      node_ids: list[str]) -> None:
        self._header_id += 1
        self.client.publish(self.order_topic, json.dumps({
            "headerId": self._header_id,
            "timestamp": "2026-09-05T00:00:00.000Z",
            "version": vda.VDA_VERSION,
            "manufacturer": self.manufacturer,
            "serialNumber": self.serial,
            "orderId": order_id,
            "orderUpdateId": update_id,
            "nodes": [{"nodeId": nid, "sequenceId": i * 2, "released": True,
                       "actions": []} for i, nid in enumerate(node_ids)],
            "edges": [],
        }), qos=0, retain=False)

    def wait_for(self, predicate, timeout: float = RESPONSE_TIMEOUT_S):
        """Return the first state message satisfying ``predicate``, or None."""
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            with self._lock:
                batch = self.states[seen:]
                seen = len(self.states)
            for payload in batch:
                if predicate(payload):
                    return payload
            time.sleep(0.05)
        return None


def test_stale_order_update_reports_order_update_error_over_a_real_broker(
        broker, monkeypatch):
    """The exact scenario the conformance tester's stale-replay probe runs.

    VDA 5050 has no ack topic for orders, so an update the vehicle drops
    has to come back as an ``errors[]`` entry of errorType
    ``orderUpdateError``. Before that was implemented the vehicle answered
    a rejected update with total silence -- indistinguishable, from master
    control's side, from having applied it.
    """
    import yantrasim.sim as simmod

    # No random faults: a faulted robot refuses orders outright, which would
    # make this a timing lottery rather than a protocol test.
    monkeypatch.setattr(simmod, "RANDOM_FAULT_P", 0.0)

    host, port = broker
    with _SimThread(host, port) as fleet:
        master = _MasterControl(host, port, fleet.robot)
        try:
            # A destination far enough away that the robot is still driving
            # (and so still on THIS order) when the stale replay arrives.
            start = fleet.robot.node
            target = max(
                (n for n in world.TASK_NODES if n != start),
                key=lambda n: len(world.shortest_path(start, n)))
            path = list(world.shortest_path(start, target))

            master.publish_order("mc-order", 5, path)
            adopted = master.wait_for(
                lambda p: p.get("orderId") == "mc-order"
                and p.get("orderUpdateId") == 5)
            assert adopted is not None, "the vehicle never adopted the order"

            master.publish_order("mc-order", 2, path)   # stale replay
            reported = master.wait_for(
                lambda p: any(e.get("errorType") == vda.ORDER_UPDATE_ERROR
                              for e in (p.get("errors") or [])))
            assert reported is not None, (
                "no orderUpdateError came back after a stale orderUpdateId; "
                "master control cannot tell the update was dropped")

            err = next(e for e in reported["errors"]
                       if e["errorType"] == vda.ORDER_UPDATE_ERROR)
            assert err["errorLevel"] == "WARNING"
            refs = {r["referenceKey"]: r["referenceValue"]
                    for r in err["errorReferences"]}
            assert refs["orderId"] == "mc-order"
            assert refs["orderUpdateId"] == "2"
            # The rejection did not rewind the order actually in progress.
            assert reported["orderId"] == "mc-order"
            assert reported["orderUpdateId"] == 5
        finally:
            master.close()
