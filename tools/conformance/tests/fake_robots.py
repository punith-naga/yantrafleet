"""A reference AGV and a deliberately-broken one, for testing the tester.

``FakeRobot`` speaks VDA 5050 2.1 correctly by default. Every deviation it can
produce is an explicit flag on :class:`Defects`, so a test can switch on ONE
non-compliant behaviour and assert that exactly the corresponding check flips
to ``fail`` while the rest keep passing. That is the property that makes the
tester trustworthy: it has to fail things, and it has to fail only the right
things.

It runs on :class:`yantraconform.loopback.PahoShimClient`, the same paho-shaped
adapter the yantrasim integration test uses, so this fixture and the real
simulator are driven through identical plumbing.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from yantraconform.loopback import LoopbackBroker, PahoShimClient

VERSION = "2.1.0"
MAP_NODES = ("N1", "N2", "N3")
ACTION_REPEATS = 3
ERROR_REPEATS = 3


@dataclass
class Defects:
    """Every non-compliant behaviour this fixture can be asked to exhibit.

    Each flag is meant to be switched on ALONE, so a test can assert both
    halves of the property that makes a conformance tester worth anything:
    the corresponding check flips to ``fail`` (it detects the defect) and no
    unrelated check does (it does not smear one defect across the report).
    Where one defect genuinely breaks two clauses at once -- a header without
    ``version`` violates both the header rule and the version rule -- the test
    names both, rather than the fixture pretending the blast radius is one.
    """

    # connection topic
    no_connection: bool = False
    bad_connection_state: bool = False      # "CONNECTED" instead of ONLINE
    connection_not_retained: bool = False
    no_lastwill: bool = False
    # state topic
    no_state: bool = False
    missing_state_fields: bool = False      # drops errors + safetyState
    header_id_frozen: bool = False          # headerId never advances
    local_timestamps: bool = False          # no 'Z', not UTC
    identity_mismatch: bool = False         # payload serial != topic serial
    retained_state: bool = False
    theta_out_of_range: bool = False
    string_order_update_id: bool = False
    bad_topic_scheme: bool = False          # publishes state 4 segments deep
    battery_as_fraction: bool = False       # 0..1 instead of 0..100
    state_header_drop_version: bool = False  # header without 'version'
    timestamps_go_backwards: bool = False
    drop_recommended_fields: bool = False   # no agvPosition/velocity/paused/...
    string_battery_charge: bool = False     # batteryCharge as "84.0"
    battery_out_of_range: bool = False      # 150 %
    bad_operating_mode: bool = False        # "AUTO"
    bad_estop: bool = False                 # eStop "ESTOPPED"
    malformed_error_entry: bool = False     # errorLevel outside the enum
    wrong_sequence_parity: bool = False     # nodes odd, edges even
    future_major_version: bool = False      # reports 3.0.0 on v2 topics
    version_skew: bool = False              # connection/factsheet disagree
    extra_subtopic: bool = False            # publishes .../telemetry as well
    malformed_json: bool = False            # non-JSON body on the state topic
    # order handling
    accept_stale_order: bool = False
    adopt_invalid_order: bool = False
    no_order_update_error: bool = False     # rejects silently
    ignore_orders: bool = False             # never subscribes / never adopts
    ignore_order_updates: bool = False      # takes new orders, drops updates
    # actions
    silent_unknown_action: bool = False     # ignores unsupported actionType
    finish_unknown_action: bool = False     # claims FINISHED instead
    vanishing_actions: bool = False         # drops actions without a terminal
    custom_action_status: bool = False      # "DONE" instead of FINISHED
    duplicate_action_ids: bool = False      # same actionId twice in one message
    action_without_type: bool = False       # actionStates entry has no type
    action_regresses: bool = False          # FINISHED -> RUNNING -> FINISHED
    #: actionTypes on instantActions to drop on the floor. Empty tuple means
    #: "answer everything"; ("stateRequest",) isolates one check.
    ignore_instant_types: tuple[str, ...] = ()
    # factsheet
    no_factsheet: bool = False
    factsheet_missing_blocks: bool = False
    factsheet_not_retained: bool = False
    factsheet_bad_enums: bool = False       # agvKinematic "TRACKED"
    factsheet_incomplete_physical: bool = False   # no speedMax
    factsheet_no_header: bool = False
    # visualization
    broken_visualization: bool = False      # header only, no pose or velocity


class FakeRobot:
    """A single VDA 5050 vehicle on a :class:`LoopbackBroker`."""

    def __init__(self, broker: LoopbackBroker, manufacturer: str = "acme",
                 serial: str = "AGV_01", defects: Defects | None = None,
                 interface: str = "uagv", major: str = "v2",
                 publish_visualization: bool = False,
                 roam: bool = True) -> None:
        self.broker = broker
        #: when idle, drive to the next node on the map. A real AGV almost
        #: always has SOME horizon to report, and without one the tester has
        #: no nodeStates/edgeStates evidence to grade (and no discovered
        #: nodeId to aim an order probe at).
        self.roam = roam
        self.manufacturer = manufacturer
        self.serial = serial
        self.defects = defects or Defects()
        self.interface = interface
        self.major = major
        self.publish_visualization = publish_visualization

        self.header_ids: dict[str, int] = {}
        self.clock = datetime(2026, 8, 26, 10, 0, 0, tzinfo=timezone.utc)
        self.order_id = ""
        self.order_update_id = 0
        self.node = MAP_NODES[0]
        self.path: list[str] = []
        self.driving = False
        self.battery = 84.0
        #: [actionState dict, publishes-remaining]
        self.pending_actions: list[list[Any]] = []
        self.pending_errors: list[list[Any]] = []
        self.seen_action_ids: set[str] = set()
        self.ticks = 0
        #: ticks remaining in the FINISHED -> RUNNING -> FINISHED regression
        self._regression: list[Any] | None = None
        if self.defects.malformed_error_entry:
            self.pending_errors.append([
                {"errorType": "sensorFault", "errorLevel": "CRITICAL",
                 "errorDescription": "errorLevel is outside the spec's enum"},
                10 ** 6])

        self.client = PahoShimClient(broker, name=f"robot-{serial}")
        self.client.on_message = self._on_message
        if not self.defects.no_lastwill:
            self.client.will_set(
                self._topic("connection"),
                json.dumps(self._header("connection")
                           | {"connectionState": "CONNECTIONBROKEN"}),
                qos=1, retain=True)
        self.client.subscribe(self._topic("order"), qos=0)
        self.client.subscribe(self._topic("instantActions"), qos=0)
        self.announce("ONLINE")
        if not self.defects.no_factsheet:
            self._publish_factsheet()

    # -- topics / header ---------------------------------------------------

    def _topic(self, subtopic: str) -> str:
        return "/".join((self.interface, self.major, self.manufacturer,
                         self.serial, subtopic))

    def _timestamp(self) -> str:
        if self.defects.local_timestamps:
            return self.clock.astimezone(
                timezone(timedelta(hours=2))).replace(tzinfo=None).isoformat()
        return self.clock.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _version(self, subtopic: str) -> str:
        if self.defects.future_major_version:
            return "3.0.0"
        if self.defects.version_skew and subtopic != "state":
            # One publisher was updated and the others were not: the exact bug
            # protocol.version_consistency exists to catch.
            return "2.0.0"
        return VERSION

    def _header(self, subtopic: str) -> dict[str, Any]:
        if self.defects.header_id_frozen:
            hid = 1
        else:
            hid = self.header_ids.get(subtopic, 0) + 1
            self.header_ids[subtopic] = hid
        serial = "WRONG_SERIAL" if self.defects.identity_mismatch else self.serial
        header = {"headerId": hid, "timestamp": self._timestamp(),
                  "version": self._version(subtopic),
                  "manufacturer": self.manufacturer, "serialNumber": serial}
        if subtopic == "state" and self.defects.state_header_drop_version:
            header.pop("version")
        return header

    # -- publishing --------------------------------------------------------

    def announce(self, connection_state: str) -> None:
        if self.defects.no_connection:
            return
        if self.defects.bad_connection_state and connection_state == "ONLINE":
            connection_state = "CONNECTED"
        self.client.publish(
            self._topic("connection"),
            json.dumps(self._header("connection")
                       | {"connectionState": connection_state}),
            qos=1, retain=not self.defects.connection_not_retained)

    def tick(self, n: int = 1) -> None:
        """Advance the vehicle and publish that many state messages."""
        for _ in range(n):
            self.ticks += 1
            if self.defects.timestamps_go_backwards and self.ticks % 2 == 0:
                self.clock -= timedelta(seconds=3)
            else:
                self.clock += timedelta(seconds=1)
            self._advance_regression()
            if self.ticks == 1:
                # Re-announce ONLINE on the first tick, the way a real vehicle
                # does after every (re)connect. Without this the ONLINE sent in
                # __init__ is only visible to the tester via the RETAINED copy,
                # so switching off retention would make the vehicle vanish
                # entirely and 'connection.retained' could never be observed
                # failing on its own.
                self.announce("ONLINE")
            if self.path:
                self.node = self.path.pop(0)
                self.driving = bool(self.path)
                if not self.path:
                    self._finish_node_action()
            if not self.path and self.roam:
                nxt = MAP_NODES[(MAP_NODES.index(self.node) + 1)
                                % len(MAP_NODES)] if self.node in MAP_NODES \
                    else MAP_NODES[0]
                self.path = [nxt]
                self.driving = True
            self.battery = max(0.0, self.battery - 0.05)
            self.publish_state()

    def publish_state(self) -> None:
        if self.defects.no_state:
            return
        state = self._build_state()
        topic = self._topic("state")
        if self.defects.bad_topic_scheme:
            topic = "/".join((self.interface, self.manufacturer, self.serial,
                              "state"))
        self.client.publish(topic, json.dumps(state), qos=0,
                            retain=self.defects.retained_state)
        if self.defects.malformed_json:
            # A truncated / non-JSON body on an otherwise correct topic: the
            # single most common "the vehicle went quiet" support ticket.
            self.client.publish(self._topic("state"), "{not json at all",
                                qos=0, retain=False)
        if self.defects.extra_subtopic:
            # Five segments, so it routes to this vehicle, but a sub-topic
            # name no compliant fleet manager subscribes to.
            self.client.publish(self._topic("telemetry"),
                                json.dumps(self._header("telemetry")), qos=0,
                                retain=False)
        if self.publish_visualization:
            payload = self._header("visualization")
            if not self.defects.broken_visualization:
                # Built from the pose directly, NOT from ``state`` -- a defect
                # that strips optional blocks out of state must not silently
                # break the visualization topic too, or the blast-radius
                # assertions would be measuring the fixture, not the tester.
                payload |= {
                    "agvPosition": {"x": 1.5, "y": 2.5,
                                    "theta": round(math.pi / 4, 4),
                                    "mapId": "warehouse-1",
                                    "positionInitialized": True},
                    "velocity": {"vx": 0.4 if self.driving else 0.0,
                                 "vy": 0.0, "omega": 0.0}}
            self.client.publish(self._topic("visualization"),
                                json.dumps(payload), qos=0, retain=False)

    def _build_state(self) -> dict[str, Any]:
        theta = 7.5 if self.defects.theta_out_of_range else round(math.pi / 4, 4)
        seq = 0
        node_states, edge_states = [], []
        # Nodes take even sequenceIds and edges odd ones (6.6); the defect
        # swaps the two parities without otherwise disturbing the ordering.
        node_off, edge_off = (1, 2) if self.defects.wrong_sequence_parity else (2, 1)
        for i, nid in enumerate(self.path):
            edge_states.append({"edgeId": f"E{i}", "sequenceId": seq + edge_off,
                                "released": True})
            node_states.append({"nodeId": nid, "sequenceId": seq + node_off,
                                "released": True})
            seq += 2
        ouid: Any = self.order_update_id
        if self.defects.string_order_update_id:
            ouid = str(self.order_update_id)
        state = self._header("state") | {
            "orderId": self.order_id,
            "orderUpdateId": ouid,
            "lastNodeId": self.node,
            "lastNodeSequenceId": 0,
            "nodeStates": node_states,
            "edgeStates": edge_states,
            "actionStates": self._drain_action_states(),
            "driving": self.driving,
            "paused": False,
            "newBaseRequest": False,
            "agvPosition": {"x": 1.5, "y": 2.5, "theta": theta,
                            "mapId": "warehouse-1", "positionInitialized": True},
            "velocity": {"vx": 0.4 if self.driving else 0.0, "vy": 0.0,
                         "omega": 0.0},
            "batteryState": {
                "batteryCharge": self._battery_charge(),
                "charging": False, "reach": 4200},
            "operatingMode": ("AUTO" if self.defects.bad_operating_mode
                              else "AUTOMATIC"),
            "errors": self._drain_errors(),
            "safetyState": {
                "eStop": "ESTOPPED" if self.defects.bad_estop else "NONE",
                "fieldViolation": False},
        }
        if self.defects.missing_state_fields:
            state.pop("errors", None)
            state.pop("safetyState", None)
        if self.defects.drop_recommended_fields:
            for name in ("agvPosition", "velocity", "paused", "newBaseRequest"):
                state.pop(name, None)
        return state

    def _battery_charge(self) -> Any:
        if self.defects.string_battery_charge:
            return str(round(self.battery, 1))
        if self.defects.battery_out_of_range:
            return 150.0
        if self.defects.battery_as_fraction:
            return self.battery / 100.0
        return round(self.battery, 1)

    def _publish_factsheet(self) -> None:
        header: dict[str, Any] = ({} if self.defects.factsheet_no_header
                                  else self._header("factsheet"))
        physical = {
            "speedMin": 0.0, "speedMax": 1.5, "accelerationMax": 0.5,
            "decelerationMax": 0.5, "heightMax": 0.4, "width": 0.6,
            "length": 0.9}
        if self.defects.factsheet_incomplete_physical:
            physical.pop("speedMax")
        fs = header | {
            "typeSpecification": {
                "seriesName": "fake-1",
                "agvKinematic": ("TRACKED" if self.defects.factsheet_bad_enums
                                 else "DIFF"),
                "agvClass": "CARRIER", "maxLoadMass": 500.0,
                "localizationTypes": ["NATURAL"],
                "navigationTypes": ["AUTONOMOUS"]},
            "physicalParameters": physical,
            "protocolLimits": {"maxStringLens": {}, "maxArrayLens": {},
                               "timing": {"minOrderInterval": 0.5,
                                          "minStateInterval": 0.5}},
            "protocolFeatures": {"optionalParameters": [], "agvActions": []},
            "agvGeometry": {"wheelDefinitions": [], "envelopes2d": []},
            "loadSpecification": {"loadPositions": [], "loadSets": []},
        }
        if self.defects.factsheet_missing_blocks:
            fs.pop("protocolLimits", None)
            fs.pop("loadSpecification", None)
        self.client.publish(self._topic("factsheet"), json.dumps(fs), qos=1,
                            retain=not self.defects.factsheet_not_retained)

    # -- actionStates / errors --------------------------------------------

    def _stage_action(self, action_id: str, action_type: str, status: str,
                      detail: str = "", repeats: int = ACTION_REPEATS) -> None:
        """Set the reported status of ``action_id``.

        actionStates is keyed by actionId, so re-staging an action REPLACES
        its entry rather than adding a second one. (Appending instead is a
        real and common vendor bug -- it makes an action look like it went
        FINISHED and then back to RUNNING within a single message -- and it is
        what ``duplicate_action_ids`` reproduces on purpose.)
        """
        if self.defects.custom_action_status and status == "FINISHED":
            status = "DONE"
        entry: dict[str, Any] = {"actionId": action_id,
                                 "actionType": action_type,
                                 "actionStatus": status}
        if self.defects.action_without_type:
            entry.pop("actionType")
        if detail:
            entry["resultDescription"] = detail
        if not self.defects.duplicate_action_ids:
            self.pending_actions = [e for e in self.pending_actions
                                    if e[0].get("actionId") != action_id]
        self.pending_actions.append([entry, repeats])

    def _stage_error(self, error_type: str, description: str) -> None:
        self.pending_errors.append([
            {"errorType": error_type, "errorLevel": "WARNING",
             "errorDescription": description}, ERROR_REPEATS])

    def _drain_action_states(self) -> list[dict[str, Any]]:
        out = [dict(e[0]) for e in self.pending_actions]
        for e in self.pending_actions:
            e[1] -= 1
        self.pending_actions = [e for e in self.pending_actions if e[1] > 0]
        return out

    def _drain_errors(self) -> list[dict[str, Any]]:
        out = [dict(e[0]) for e in self.pending_errors]
        for e in self.pending_errors:
            e[1] -= 1
        self.pending_errors = [e for e in self.pending_errors if e[1] > 0]
        return out

    def _advance_regression(self) -> None:
        """Drive the FINISHED -> RUNNING -> FINISHED regression, one tick each.

        The action ends the run terminal, so ``actions.terminal_reported`` is
        satisfied and the only clause actually broken is the one that says a
        terminal status is absorbing.
        """
        if self._regression is None:
            return
        action_id, action_type, step = self._regression
        if step == 0:
            self._stage_action(action_id, action_type, "RUNNING", repeats=1)
            self._regression = [action_id, action_type, 1]
        elif step == 1:
            self._stage_action(action_id, action_type, "FINISHED",
                               "finished (again)", repeats=10 ** 6)
            self._regression = None

    def _finish_node_action(self) -> None:
        if self.node_action_id is None:
            return
        if self.defects.action_regresses:
            self._stage_action(self.node_action_id,
                               self.node_action_type or "move", "FINISHED",
                               "node action complete", repeats=1)
            self._regression = [self.node_action_id,
                                self.node_action_type or "move", 0]
            self.node_action_id = None
            return
        if self.defects.vanishing_actions:
            # No terminal status at all: the RUNNING entry staged when the
            # order arrived simply expires and the action is never heard of
            # again -- the "did it finish or is it stuck?" bug.
            self.node_action_id = None
            return
        self._stage_action(self.node_action_id, self.node_action_type or "move",
                           "FINISHED", "node action complete")
        self.node_action_id = None

    node_action_id: str | None = None
    node_action_type: str | None = None

    # -- inbound -----------------------------------------------------------

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            msg = json.loads(message.payload)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        if message.topic.endswith("/order"):
            self._handle_order(msg)
        elif message.topic.endswith("/instantActions"):
            self._handle_instant_actions(msg)

    def _handle_order(self, msg: dict[str, Any]) -> None:
        if self.defects.ignore_orders:
            return
        order_id = str(msg.get("orderId") or "")
        if not order_id:
            return
        try:
            update_id = int(msg.get("orderUpdateId") or 0)
        except (TypeError, ValueError):
            update_id = 0
        if (self.defects.ignore_order_updates and order_id == self.order_id
                and update_id > self.order_update_id):
            # Takes a brand-new order happily, silently drops every update to
            # the order it is already running -- so master control can never
            # extend the vehicle's base while it drives.
            return
        if (order_id == self.order_id and update_id <= self.order_update_id
                and not self.defects.accept_stale_order):
            if not self.defects.no_order_update_error:
                self._stage_error(
                    "orderUpdateError",
                    f"orderUpdateId {update_id} is not newer than "
                    f"{self.order_update_id}")
            return
        nodes = [str(n.get("nodeId")) for n in (msg.get("nodes") or [])
                 if isinstance(n, dict) and n.get("nodeId")]
        unknown = [n for n in nodes if n not in MAP_NODES]
        if unknown and not self.defects.adopt_invalid_order:
            self._stage_error("validationError",
                              f"unknown node id(s): {', '.join(unknown)}")
            return
        self.order_id = order_id
        self.order_update_id = update_id
        self.path = [n for n in nodes if n != self.node]
        self.driving = bool(self.path)
        actions = [a for n in (msg.get("nodes") or [])
                   if isinstance(n, dict)
                   for a in (n.get("actions") or []) if isinstance(a, dict)]
        if actions:
            self.node_action_id = str(actions[0].get("actionId") or "") or None
            self.node_action_type = str(actions[0].get("actionType") or "move")
            if self.node_action_id:
                # Normally the RUNNING entry is held until a terminal status
                # replaces it; with ``vanishing_actions`` it expires on its own.
                self._stage_action(
                    self.node_action_id, self.node_action_type, "RUNNING",
                    repeats=2 if self.defects.vanishing_actions else 99)
        if not self.path:
            self._finish_node_action()

    def _handle_instant_actions(self, msg: dict[str, Any]) -> None:
        for action in msg.get("actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("actionId") or "")
            action_type = str(action.get("actionType") or "")
            if not action_id or action_id in self.seen_action_ids:
                continue
            self.seen_action_ids.add(action_id)
            if action_type in self.defects.ignore_instant_types:
                continue
            if action_type == "stateRequest":
                self._stage_action(action_id, action_type, "FINISHED",
                                   "state published")
                self.publish_state()
            elif action_type == "factsheetRequest":
                self._stage_action(action_id, action_type, "FINISHED",
                                   "factsheet published")
                if not self.defects.no_factsheet:
                    self._publish_factsheet()
            elif action_type == "cancelOrder":
                # Per 6.10.2 every outstanding order action is FAILED first.
                if self.node_action_id:
                    self._stage_action(self.node_action_id,
                                       self.node_action_type or "move",
                                       "FAILED", "order cancelled")
                    self.node_action_id = None
                self.path = []
                self.driving = False
                self._stage_action(action_id, action_type, "FINISHED",
                                   "order cancelled")
            elif action_type == "initPosition":
                self._stage_action(action_id, action_type, "FINISHED",
                                   "position initialized")
            else:
                if self.defects.silent_unknown_action:
                    continue
                if self.defects.finish_unknown_action:
                    self._stage_action(action_id, action_type, "FINISHED",
                                       "pretended to do it")
                else:
                    self._stage_action(action_id, action_type, "FAILED",
                                       f"unsupported actionType {action_type!r}")

    # -- lifecycle ---------------------------------------------------------

    def kill(self) -> None:
        """Drop the network: the broker publishes the last-will."""
        self.client.kill()

    def close(self) -> None:
        self.announce("OFFLINE")
        self.client.disconnect()
