"""End-to-end: grade this repo's own simulator, the real one.

Everything else in the suite grades a fixture written alongside the tester, by
the same author, from the same reading of the spec -- which proves the checks
are internally consistent and proves nothing about whether they survive contact
with an independent implementation. These tests point the tester at
``sim/yantrasim``: a separate package, written for a different purpose, that
speaks VDA 5050 v2.1 over MQTT because it has to, not because a conformance
test asked it to.

TWO LEVELS, AND THEY PROVE DIFFERENT THINGS
-------------------------------------------
``test_loopback_*``  runs the simulator's real ``MqttTransport`` against the
    in-process :class:`~yantraconform.loopback.LoopbackBroker`. The protocol
    logic on both sides is genuine and the result is bit-for-bit
    deterministic, so this can assert exact scores. What it does NOT prove:
    that ``PahoSession`` connects, that real subscriptions and QoS work, that
    retained messages come back from a real broker, or that any of it survives
    real concurrency.

``test_real_broker_*``  does all of that -- two real MQTT clients over TCP
    through a real broker -- and is therefore the one that proves the tool
    works at all outside a test harness. It is skipped when no broker is
    reachable, so it can never be the reason a stranger's checkout goes red.
    It asserts a floor, not an exact score, because real timing means the
    number of state messages in a window is not fixed.

Run the networked one explicitly with::

    # either let the test start its own mosquitto (if installed) ...
    python3 -m pytest tools/conformance/tests/test_sim_integration.py -q
    # ... or point it at any broker you already have
    YANTRACONFORM_TEST_BROKER=localhost:1883 python3 -m pytest ...
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time

import pytest

from harness import SimFleet, checks_by_id, fast_options, run_against, statuses
from yantraconform.loopback import LoopbackBroker

# ---------------------------------------------------------------------------
# What the shipped simulator is known to get wrong.
#
# These are not "expected failures" in the sense of a test we gave up on --
# they are findings the tester produced about a real implementation, and each
# one is a genuine deviation from VDA 5050 2.1 that is documented in the
# simulator's own source. Pinning them here means the integration test fails
# if the simulator gets FIXED (so the expectation gets updated) and also if it
# gets WORSE (so a regression is caught). Either way somebody looks.
# ---------------------------------------------------------------------------

#: yantrasim multiplexes every vehicle over ONE MQTT connection, so it cannot
#: register a per-vehicle last-will. ``sim/yantrasim/transports/mqtt.py`` says
#: so in its module docstring and calls it an accepted simulator-only
#: deviation. A real AGV holds its own connection and has no such excuse.
SIM_KNOWN_FAILURES = {"connection.lastwill"}

#: Found by the REAL-BROKER run, not the loopback one -- which is the whole
#: argument for keeping both. yantrasim rejects a stale orderUpdateId
#: correctly (``FleetSim.apply_order`` returns ``(False, "stale ...")``) but
#: only writes it to its own log: ``MqttTransport.poll_orders`` states in its
#: docstring that a rejection "has no dedicated VDA ack topic to report on",
#: which is the exact misreading this check exists to catch. VDA 5050 2.1 §6.5
#: reports it in-band, as an ``errors[]`` entry of errorType
#: ``orderUpdateError`` in the next state message. Until the simulator does
#: that, master control cannot distinguish "your update was dropped" from
#: "your update was applied and nothing changed".
SIM_KNOWN_ORDER_GAPS = {"order.reject_stale_reports_error"}

#: An order action left RUNNING when the order it belonged to is superseded.
#: Timing-dependent (it needs the tester's order probe to still be in flight
#: when the simulator dispatches its own next order), so it is tolerated
#: rather than required in either direction.
SIM_TIMING_DEPENDENT = {"actions.terminal_reported"}

#: initPosition is refused while the vehicle is driving, and the simulator's
#: robots are almost never idle. The check treats an explicit FAILED with a
#: reason as a warning rather than a failure precisely because "I cannot do
#: this right now, and here is why" is a conformant answer.
SIM_KNOWN_WARNINGS = {"instant.initPosition"}


# ---------------------------------------------------------------------------
# in-process, deterministic
# ---------------------------------------------------------------------------

def test_loopback_the_simulator_scores_well():
    broker = LoopbackBroker()
    fleet = SimFleet(broker, robots=2)
    document = run_against(lambda ticks: fleet.tick(ticks), broker,
                           options=fast_options(max_robots=2))

    summary = document["summary"]
    assert summary["robots"] == 2
    assert [r["serial"] for r in document["robots"]] == ["AMR_01", "AMR_02"]
    # A real implementation of the protocol scores in the high 90s against
    # this rule set. If this number collapses, either the simulator broke or
    # the tester started inventing findings; both need a human.
    assert summary["score"] >= 95.0, [
        (c["id"], c["observed"]) for r in document["robots"]
        for c in r["checks"] if c["status"] == "fail"]
    assert summary["counts"]["pass"] > 80


def test_loopback_the_only_failures_are_the_known_ones():
    """No surprise findings against an independent implementation."""
    broker = LoopbackBroker()
    fleet = SimFleet(broker, robots=2)
    document = run_against(lambda ticks: fleet.tick(ticks), broker,
                           options=fast_options(max_robots=2))

    for index, robot in enumerate(document["robots"]):
        status_map = statuses(document, index)
        failing = {cid for cid, s in status_map.items() if s == "fail"}
        warning = {cid for cid, s in status_map.items() if s == "warn"}
        assert failing <= (SIM_KNOWN_FAILURES | SIM_KNOWN_ORDER_GAPS
                           | SIM_TIMING_DEPENDENT), (
            robot["serial"], sorted(failing))
        assert warning <= SIM_KNOWN_WARNINGS, (robot["serial"], sorted(warning))


def test_loopback_the_simulator_really_is_missing_its_last_will():
    """The known finding is a finding, not a check that silently skipped."""
    broker = LoopbackBroker()
    fleet = SimFleet(broker, robots=1)
    document = run_against(lambda ticks: fleet.tick(ticks), broker,
                           options=fast_options(max_robots=1))
    finding = checks_by_id(document)["connection.lastwill"]
    assert finding["status"] == "fail"
    assert "last-will" in finding["observed"]
    assert finding["remediation"]


def test_loopback_the_core_protocol_checks_actually_pass():
    """Name the checks the simulator gets RIGHT, so a silent skip cannot pass.

    Without this, a bug that made the tester collect no evidence would still
    satisfy "score >= 95" (every check skipped, nothing scored) -- so the
    positive result has to be pinned to specific check ids.
    """
    broker = LoopbackBroker()
    fleet = SimFleet(broker, robots=1)
    document = run_against(lambda ticks: fleet.tick(ticks), broker,
                           options=fast_options(max_robots=1))
    by_id = checks_by_id(document)
    for cid in ("connection.published", "connection.state_enum",
                "connection.online", "connection.retained",
                "state.published", "state.required_fields",
                "state.field_types", "state.header",
                "state.header_id_monotonic", "state.timestamp_format",
                "state.identity", "state.version_field",
                "state.not_retained", "state.safety_state",
                "order.accept_new", "order.echo",
                "actions.status_enum", "actions.required_fields",
                "instant.stateRequest", "instant.cancelOrder",
                "instant.unknown_rejected",
                "factsheet.published", "factsheet.required_blocks",
                "factsheet.retained",
                "protocol.topic_scheme", "protocol.major_version_segment",
                "protocol.json_valid", "protocol.supported_version"):
        assert by_id[cid]["status"] == "pass", (cid, by_id[cid]["observed"])


def test_loopback_the_simulator_rejects_a_stale_order_update():
    """The single most important behavioural check, against the real thing.

    The simulator is its own master control and keeps dispatching orders to
    itself, so the tester's stale-replay probe is often overtaken by the
    simulator's next order -- in which case the check correctly reports
    'unevaluable' rather than a verdict it cannot support. Driving
    ``FleetSim`` directly removes that race, so the behaviour itself can be
    asserted rather than the report's honesty about not knowing.
    """
    from yantrasim.sim import FleetSim

    sim = FleetSim(seed=7)
    robot = sim.robots[0]
    ok, _ = sim.apply_order(robot.robot_id, "conform-1", 0, [], [])
    assert ok
    ok, detail = sim.apply_order(robot.robot_id, "conform-1", 1, [], [])
    assert ok, detail
    # replay of an update the vehicle has already passed
    ok, detail = sim.apply_order(robot.robot_id, "conform-1", 0, [], [])
    assert not ok, "a stale orderUpdateId must not be adopted"
    assert "stale" in detail.lower()
    assert robot.order_update_id == 1


# ---------------------------------------------------------------------------
# over a real broker, over real TCP
# ---------------------------------------------------------------------------

def _reachable(host: str, port: int, timeout: float = 0.75) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_broker():
    """A real MQTT broker, or a skip.

    Preference order: an explicitly configured broker, then a mosquitto this
    fixture starts itself (a private one, so no retained message from an
    earlier run can leak in), then skip.
    """
    configured = os.environ.get("YANTRACONFORM_TEST_BROKER")
    if configured:
        host, _, port_text = configured.partition(":")
        port = int(port_text or 1883)
        if not _reachable(host, port):
            pytest.skip(f"YANTRACONFORM_TEST_BROKER={configured} is not reachable")
        yield host, port
        return

    mosquitto = shutil.which("mosquitto")
    if not mosquitto:
        pytest.skip("no MQTT broker: install mosquitto, or set "
                    "YANTRACONFORM_TEST_BROKER=host:port")
    port = _free_port()
    proc = subprocess.Popen(
        [mosquitto, "-p", str(port)],
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
    """Runs yantrasim against a real broker on its own thread, like the CLI."""

    def __init__(self, host: str, port: int, robots: int = 1,
                 period: float = 0.25) -> None:
        from yantrasim.commands import apply_command
        from yantrasim.sim import FleetSim
        from yantrasim.transports.mqtt import MqttTransport

        self.period = period
        self._apply_command = apply_command
        self.sim = FleetSim(seed=42)
        self.sim.robots = self.sim.robots[:robots]
        self.transport = MqttTransport(self.sim.robots, broker=host, port=port)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            out = self.sim.tick(dt_s=1.0)
            self.transport.publish(out)
            self.transport.poll_commands(
                lambda rid, cmd: self._apply_command(self.sim, rid, cmd))
            self.transport.poll_orders(
                lambda rid, oid, ouid, nodes, actions:
                    self.sim.apply_order(rid, oid, ouid, nodes, actions))
            self._stop.wait(self.period)

    def __enter__(self) -> "_SimThread":
        self._thread.start()
        time.sleep(0.5)   # let ONLINE + the first states land
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.transport.close()


@pytest.mark.slow
def test_real_broker_end_to_end(live_broker):
    """The whole tool, over TCP, against the real simulator. No loopback.

    This is the run a stranger performs: ``yantra-conform run --broker ...``
    with a real paho connection, real subscriptions, real retained messages
    and a vehicle publishing on another thread at its own rate.
    """
    from yantraconform.runner import run_conformance
    from yantraconform.session import PahoSession, parse_broker

    host, port = live_broker
    with _SimThread(host, port, robots=1):
        target = parse_broker(f"mqtt://{host}:{port}")
        session = PahoSession(target, client_id="yantra-conform-test")
        try:
            report = run_conformance(
                session,
                _live_options(),
                broker_label=str(target))
        finally:
            session.close()

    document = report.to_dict()
    assert document["robots"], (
        "no vehicle discovered over a real broker: "
        f"warnings={document['warnings']}")
    robot = document["robots"][0]
    assert robot["serial"] == "AMR_01"
    assert robot["message_counts"]["state"] >= 3
    assert robot["message_counts"]["connection"] >= 1

    status_map = {c["id"]: c for c in robot["checks"]}
    # Retained messages really came back from a real broker.
    assert status_map["connection.retained"]["status"] == "pass"
    assert status_map["factsheet.retained"]["status"] == "pass"
    assert status_map["state.not_retained"]["status"] == "pass"
    # The core of the protocol really was exercised over the wire.
    for cid in ("connection.published", "connection.online",
                "state.published", "state.required_fields",
                "state.header_id_monotonic", "state.timestamp_format",
                "state.identity", "protocol.topic_scheme",
                "protocol.json_valid", "factsheet.published"):
        assert status_map[cid]["status"] == "pass", (cid,
                                                     status_map[cid]["observed"])
    # A floor, not an exact number: over real time the message counts vary.
    assert document["summary"]["score"] >= 85.0, [
        (c["id"], c["observed"]) for c in robot["checks"]
        if c["status"] == "fail"]

    unexpected = ({c["id"] for c in robot["checks"] if c["status"] == "fail"}
                  - SIM_KNOWN_FAILURES - SIM_KNOWN_ORDER_GAPS
                  - SIM_TIMING_DEPENDENT)
    assert unexpected == set(), sorted(unexpected)


@pytest.mark.slow
def test_real_broker_html_report_is_written(live_broker, tmp_path):
    """The artefact a stranger actually walks away with, produced for real."""
    from yantraconform.__main__ import main

    host, port = live_broker
    out_html = tmp_path / "report.html"
    out_json = tmp_path / "report.json"
    with _SimThread(host, port, robots=1):
        code = main(["run", "--broker", f"mqtt://{host}:{port}",
                     "--discover-seconds", "1.5", "--observe-seconds", "2",
                     "--settle-seconds", "0.8", "--retain-seconds", "1",
                     "--html", str(out_html), "--json", str(out_json),
                     "--quiet"])
    assert code == 0
    html = out_html.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "AMR_01" in html
    assert "yantrika.ai" in html
    assert out_json.exists()


def _live_options():
    from yantraconform.runner import RunOptions

    return RunOptions(discover_seconds=1.5, observe_seconds=2.0,
                      settle_seconds=0.8, retain_seconds=1.0, max_robots=2)
