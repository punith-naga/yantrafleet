"""Offline end-to-end over the REAL wire: sim -> MQTT broker -> connector.

The full VDA 5050 robot-data path on genuine localhost sockets, zero network
egress:

* an embedded amqtt broker (``yantraops.broker.EmbeddedBroker``) on an
  ephemeral 127.0.0.1 port,
* ``yantrasim``'s :class:`MqttTransport` publishing per-vehicle
  ``uagv/v2/<manufacturer>/<serial>/state`` topics through a real paho
  client,
* ``yantrabridge``'s :class:`MqttSource` subscribed to ``uagv/v2/+/+/state``,
  translating each message and writing through its real ``SupabaseSink``
  (httpx) into the in-process fake PostgREST.

Seed 9 is deterministic: AMR-09 faults within the 8-tick window, so the
connector must raise a fault alert with the VDA-legal serial ``AMR_09``
(dashes are outside the VDA serialNumber charset — the MQTT path is the one
place robot ids legitimately differ from the direct-Supabase path).

Skips cleanly when the optional MQTT stack (paho-mqtt and/or amqtt) is not
installed: ``pip install -e ops[mqtt]``.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import paho.mqtt.client  # noqa: F401
    _PAHO = True
except ImportError:  # pragma: no cover - env-dependent
    _PAHO = False

try:
    from yantraops.broker import AMQTT_AVAILABLE
except ImportError:  # pragma: no cover - yantraops not installed
    AMQTT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not (_PAHO and AMQTT_AVAILABLE),
    reason="MQTT e2e needs paho-mqtt + amqtt: pip install -e ops[mqtt]")

from fakerest import DEFAULT_SITE_ID, FakePostgREST  # noqa: E402
from yantracore import CANONICAL  # noqa: E402

#: The site every row in this world lands in (0005's column default), and
#: therefore the one the command bridge is scoped to.
SITE_ID = DEFAULT_SITE_ID
SEED = 9      # deterministic fault on AMR-09 within the window
TICKS = 8
CONNECT_BUDGET_S = 15.0
FLOW_BUDGET_S = 20.0


class MqttWorld:
    """Broker + fake backend + live connector + sim, torn down in order."""

    def __init__(self) -> None:
        from yantraops.broker import EmbeddedBroker
        from yantrabridge.commands import CommandPublisher
        from yantrabridge.sink import SupabaseSink
        from yantrabridge.sources import MqttSource
        from yantrabridge.translate import Translator
        from yantrasim.sim import FleetSim
        from yantrasim.transports.mqtt import MqttTransport

        self.fake = FakePostgREST()
        self.base_url = self.fake.start()
        self.broker = EmbeddedBroker()
        self.host, self.port = self.broker.start()

        # -- connector side: real MqttSource -> Translator -> real sink ----
        self.translator = Translator()
        self.sink = SupabaseSink(self.base_url, "test-key")
        self.pushed = 0
        # command gate: approved rows -> instantActions; acked by the
        # actionStates the sim's state messages carry back (v0.9).
        # source.publish is bound lazily via self — source exists below.
        # v0.17: a bridge serves exactly one site and says which — an
        # unfiltered publisher would also execute a command an anonymous
        # demo sandbox queued (supabase/0017_demo_command_scope.sql).
        # Rows here carry the 0005 column default, so that site is BLR-DC1.
        self.publisher = CommandPublisher(
            lambda topic, payload: self.source.publish(topic, payload),
            self.base_url, "test-key", site=SITE_ID)

        def on_state(msg: dict[str, Any]) -> None:
            self.publisher.handle_state(msg)
            robot, alerts = self.translator.feed(msg)
            self.sink.push([robot], alerts)
            self.pushed += 1

        self.source = MqttSource(
            on_state, host=self.host, port=self.port,
            client_id="yantrabridge-e2e")
        self.source_thread = threading.Thread(
            target=self.source.run_forever, daemon=True)
        self.source_thread.start()
        deadline = time.time() + CONNECT_BUDGET_S
        while not self.source._client.is_connected():  # noqa: SLF001
            if time.time() > deadline:
                raise RuntimeError("connector never connected to the broker")
            time.sleep(0.05)
        time.sleep(0.5)  # let the SUBACK land before the sim publishes

        # -- sim side: its own paho client on the same broker --------------
        self.sim = FleetSim(seed=SEED)
        self.transport = MqttTransport(
            self.sim.robots, broker=self.host, port=self.port)

        self.http = httpx.Client(
            base_url=f"{self.base_url}/rest/v1",
            headers={"apikey": "test-key", "Content-Type": "application/json"},
            timeout=5.0,
        )

    def rows(self, table: str, **params: str) -> list[dict[str, Any]]:
        resp = self.http.get(f"/{table}", params=params)
        resp.raise_for_status()
        return resp.json()

    def wait_for(self, predicate, budget_s: float):
        deadline = time.time() + budget_s
        last = None
        while time.time() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.2)
        return last

    def close(self) -> None:
        self.http.close()
        self.transport.close()
        self.source.stop()
        self.source_thread.join(timeout=10)
        self.publisher.close()
        self.sink.close()
        self.broker.stop()
        self.fake.stop()


@pytest.fixture(scope="module")
def world() -> Iterator[MqttWorld]:
    w = MqttWorld()
    try:
        # First tick with a receipt check: republishing tick 1 is an
        # idempotent upsert, so retry until the subscription demonstrably
        # delivers (guards against a slow SUBACK, no arbitrary long sleeps).
        out1 = w.sim.tick()
        assert w.wait_for(
            lambda: (w.transport.publish(out1) or w.rows("robots")),
            FLOW_BUDGET_S), "no robots rows ever arrived over MQTT"
        for _ in range(TICKS - 1):
            w.transport.publish(w.sim.tick())
        # QoS0 on localhost is quick, but the connector pushes each message
        # through real HTTP: wait until every published state (10 robots x
        # 8 ticks, plus any republished tick-1s) has been consumed, and the
        # deterministic fault alert has landed, before asserting anything.
        w.wait_for(lambda: w.pushed >= 10 * TICKS, FLOW_BUDGET_S)
        w.wait_for(lambda: len(w.rows("robots")) >= 10, FLOW_BUDGET_S)
        w.wait_for(lambda: w.rows("alerts"), FLOW_BUDGET_S)
        yield w
    finally:
        w.close()


# --------------------------------------------------------------------------
# 1. Robots rows arrive over real MQTT with canonical statuses
# --------------------------------------------------------------------------

def test_robots_rows_via_mqtt_wire(world: MqttWorld) -> None:
    robots = world.rows("robots", order="id.asc")
    assert len(robots) == 10, "one upserted row per AMR, not one per tick"
    # VDA serialNumber charset has no '-': ids arrive as AMR_01..AMR_10.
    assert [r["id"] for r in robots] == [f"AMR_{i:02d}" for i in range(1, 11)]
    for r in robots:
        assert r["status"] in CANONICAL, (
            f"{r['id']} has non-canonical status {r['status']!r}")
        assert 0.0 <= r["battery"] <= 100.0
        assert isinstance(r["pos"], list) and len(r["pos"]) == 2
        assert r["updated_at"]
    # every message really crossed the broker: >= 10 robots x 8 ticks
    assert world.pushed >= 10 * TICKS


def test_connector_heartbeat_written(world: MqttWorld) -> None:
    meta = world.rows("fleet_meta")
    assert meta and meta[0]["writer_id"] == "yantrabridge"


# --------------------------------------------------------------------------
# 2. Alerts flow: the deterministic AMR-09 fault becomes an alert row
# --------------------------------------------------------------------------

def test_fault_alert_flows_through(world: MqttWorld) -> None:
    alerts = world.rows("alerts", order="created_at.asc")
    assert alerts, "seed 9 must produce alert rows within 8 ticks"
    assert any(a["src"] == "AMR_09" for a in alerts)
    for a in alerts:
        assert a["sev"] in ("info", "warn", "crit")
        assert a["ack"] is False
    # deterministic ids: re-pushing the same alert rows is a backend no-op
    # (ignore-duplicates upsert), so retries can never double-alert.
    before = len(alerts)
    fault_alerts = [a for a in alerts if a["src"] == "AMR_09"]
    world.sink.push([], fault_alerts)
    assert len(world.rows("alerts")) == before


# --------------------------------------------------------------------------
# 3. Operator command round trip: commands table -> instantActions over the
#    real broker -> sim applies -> actionStates in the next state message ->
#    bridge PATCHes the row executed. The full VDA 5050 loop, v0.9.
# --------------------------------------------------------------------------

def test_command_round_trip_over_mqtt(world: MqttWorld) -> None:
    import uuid

    from yantrasim.commands import apply_command

    # pick a robot the pause verb is guaranteed to succeed on
    robot = next(r for r in world.sim.robots
                 if r.status in ("idle", "moving", "working",
                                 "to_charger", "charging"))
    serial = robot.robot_id.replace("-", "_")
    cid = str(uuid.uuid4())

    # console wrote pending, a human approved -> approved row in the table
    resp = world.http.post("/commands", json=[{
        "id": cid, "robot_id": serial, "cmd": "pause",
        "status": "approved", "requested_by": "console",
        "decided_by": "e2e-human", "created_at": "2026-09-02T10:00:00Z"}])
    resp.raise_for_status()

    # bridge publishes it exactly once as VDA instantActions
    assert world.publisher.poll() == 1, "approved command was not published"
    assert world.publisher.poll() == 0, "in-memory dedupe must hold"

    # sim receives it over the real broker and applies it on its own thread
    applied = world.wait_for(
        lambda: world.transport.poll_commands(
            lambda rid, cmd: apply_command(world.sim, rid, cmd)),
        FLOW_BUDGET_S)
    assert applied == 1, "instantActions never reached the simulator"
    assert robot.status == "paused"

    # the next state messages carry the actionState ack; the bridge sees it
    # and closes the command row — keep ticking until the PATCH lands.
    def executed() -> list[dict[str, Any]]:
        world.transport.publish(world.sim.tick())
        rows = world.rows("commands", id=f"eq.{cid}")
        return rows if rows and rows[0]["status"] == "executed" else []

    rows = world.wait_for(executed, FLOW_BUDGET_S)
    assert rows, "command row never reached status=executed"
    assert "paused" in (rows[0]["note"] or "")
    assert rows[0]["executed_at"]

    # and the robots table converges on the commanded state via the bridge
    paused = world.wait_for(
        lambda: [r for r in world.rows("robots", id=f"eq.{serial}")
                 if r["status"] == "paused"],
        FLOW_BUDGET_S)
    assert paused, "robots table never showed the commanded 'paused' status"
