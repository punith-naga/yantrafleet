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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from yantraconform.loopback import LoopbackBroker, PahoShimClient

VERSION = "2.1.0"
MAP_NODES = ("N1", "N2", "N3")
ACTION_REPEATS = 3
ERROR_REPEATS = 3


@dataclass
class Defects:
    """Every non-compliant behaviour this fixture can be asked to exhibit."""

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
    # order handling
    accept_stale_order: bool = False
    adopt_invalid_order: bool = False
    no_order_update_error: bool = False     # rejects silently
    # actions
    silent_unknown_action: bool = False     # ignores unsupported actionType
    finish_unknown_action: bool = False     # claims FINISHED instead
    vanishing_actions: bool = False         # drops actions without a terminal
    custom_action_status: bool = False      # "DONE" instead of FINISHED
    duplicate_action_ids: bool = False      # same actionId twice in one message
    # factsheet
    no_factsheet: bool = False
    factsheet_missing_blocks: bool = False
    factsheet_not_retained: bool = False


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

    def _header(self, subtopic: str) -> dict[str, Any]:
        if self.defects.header_id_frozen:
            hid = 1
        else:
            hid = self.header_ids.get(subtopic, 0) + 1
            self.header_ids[subtopic] = hid
        serial = "WRONG_SERIAL" if self.defects.identity_mismatch else self.serial
        return {"headerId": hid, "timestamp": self._timestamp(),
                "version": VERSION, "manufacturer": self.manufacturer,
                "serialNumber": serial}

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
            self.clock += timedelta(seconds=1)
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
        if self.publish_visualization:
            self.client.publish(
                self._topic("visualization"),
                json.dumps(self._header("visualization") | {
                    "agvPosition": state.get("agvPosition"),
                    "velocity": state.get("velocity")}),
                qos=0, retain=False)

    def _build_state(self) -> dict[str, Any]:
        theta = 7.5 if self.defects.theta_out_of_range else round(math.pi / 4, 4)
        seq = 0
        node_states, edge_states = [], []
        for i, nid in enumerate(self.path):
            edge_states.append({"edgeId": f"E{i}", "sequenceId": seq + 1,
                                "released": True})
            node_states.append({"nodeId": nid, "sequenceId": seq + 2,
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
                "batteryCharge": (self.battery / 100.0
                                  if self.defects.battery_as_fraction
                                  else round(self.battery, 1)),
                "charging": False, "reach": 4200},
            "operatingMode": "AUTOMATIC",
            "errors": self._drain_errors(),
            "safetyState": {"eStop": "NONE", "fieldViolation": False},
        }
        if self.defects.missing_state_fields:
            state.pop("errors", None)
            state.pop("safetyState", None)
        return state

    def _publish_factsheet(self) -> None:
        fs = self._header("factsheet") | {
            "typeSpecification": {
                "seriesName": "fake-1", "agvKinematic": "DIFF",
                "agvClass": "CARRIER", "maxLoadMass": 500.0,
                "localizationTypes": ["NATURAL"],
                "navigationTypes": ["AUTONOMOUS"]},
            "physicalParameters": {
                "speedMin": 0.0, "speedMax": 1.5, "accelerationMax": 0.5,
                "decelerationMax": 0.5, "heightMax": 0.4, "width": 0.6,
                "length": 0.9},
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

    def _finish_node_action(self) -> None:
        if self.node_action_id is None:
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
        order_id = str(msg.get("orderId") or "")
        if not order_id:
            return
        try:
            update_id = int(msg.get("orderUpdateId") or 0)
        except (TypeError, ValueError):
            update_id = 0
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
