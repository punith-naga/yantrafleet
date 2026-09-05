"""MQTT transport with proper VDA 5050 topic structure.

Topics: ``uagv/v2/<manufacturer>/<serialNumber>/state`` (QoS 0, not
retained), ``.../connection`` (QoS 1, retained), and ``.../factsheet``
(QoS 1, retained) per the spec.

v0.9: the transport also SUBSCRIBES to each robot's
``.../instantActions`` topic (QoS 0 per spec), closing the operator
command gap in ``--mqtt`` mode. Incoming ``yantra.*`` actions are queued
on the MQTT thread and drained on the sim thread through
:meth:`poll_commands` — the same hook the CLI main loop already calls
for the Supabase transport — which translates each actionType to a
``yantrasim.commands`` verb and applies it via the supplied callback.
The result is reported the real VDA way: an ``actionStates`` entry
(``actionId`` + ``actionStatus FINISHED|FAILED`` + ``resultDescription``)
rides in the robot's next few ``state`` messages, where yantrabridge
picks it up and closes the command row in the backend.

v0.10: the transport also SUBSCRIBES to each robot's ``.../order`` topic
(QoS 0 per spec) -- the core VDA 5050 master-control dispatch contract.
Incoming orders are queued the same way instantActions are and drained on
the sim thread through :meth:`poll_orders`, which validates orderUpdateId
(a stale/duplicate update to the current orderId is rejected; a new
orderId always replaces whatever the robot was doing) and drives the
robot's path/order_id/order_update_id via ``FleetSim.apply_order``. The
order flow has no ack topic, so a rejected UPDATE is reported the only way
VDA 5050 2.1 6.5 allows -- an ``errors[]`` entry of errorType
``orderUpdateError`` (WARNING) on the robot's next few state messages,
staged by ``FleetSim.apply_order`` itself.
Standard instantActions ``cancelOrder``, ``stateRequest``,
``factsheetRequest`` and ``initPosition`` are now handled too (previously
every one of these fell through to "unsupported actionType" and FAILED):
``cancelOrder`` maps to a new ``yantrasim.commands`` verb like any other
operator command; ``stateRequest`` force-publishes a fresh state message
immediately instead of waiting for the next tick; ``factsheetRequest``
publishes a static capability payload on the new ``factsheet`` topic;
``initPosition`` sets the robot's node/pose from the action's parameters
and, when the robot has a localization fault, clears it (the real-world
recovery path the fault's own hint already told an operator to use).

Note on ``blockingType``: this transport does not yet serialize an
instantAction against a RUNNING order-action per ``blockingType: HARD``.
Every instantAction this simulator supports (pause/resume/estop/charge,
cancelOrder, the three request/reinit actions) is an operator override
that is meant to pre-empt whatever the robot is doing, so gating any of
them behind "wait for the current action to finish" would be wrong, not
just incomplete -- and this simulator has no actionType that genuinely
needs the other kind of serialization to demonstrate. Modeling that
correctly needs a real distinction between override-class and
queueable-class actions, which is a bigger design question than this
pass, so ``blockingType`` is accepted on the wire but not read here.

``paho-mqtt`` is an OPTIONAL dependency: this module always imports, and
only constructing ``MqttTransport`` requires paho (install with
``pip install yantrasim[mqtt]``). Topic building lives in ``yantrasim.vda``
so it stays unit-testable without paho.

Note: a real AGV holds its own MQTT connection with a per-vehicle
last-will (CONNECTIONBROKEN) on its connection topic. This simulator
multiplexes 10 vehicles over ONE client, so it publishes ONLINE for each
robot at startup, again on every (re)connect (so a network blip is
self-healing on the wire), and OFFLINE on clean shutdown -- but cannot
register ten last-wills. Emitting real CONNECTIONBROKEN would need one
broker connection per vehicle (or per small group); that per-connection
redesign is out of scope here and remains an accepted simulator-only
deviation, documented here.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .. import vda, world
from ..sim import Robot, TickOutput, attach_pending

try:  # optional dependency
    import paho.mqtt.client as _paho
except ImportError:  # pragma: no cover - exercised via PAHO_AVAILABLE tests
    _paho = None

PAHO_AVAILABLE = _paho is not None

log = logging.getLogger(__name__)

#: instantActions actionType -> yantrasim.commands verb.
ACTION_VERBS: dict[str, str] = {
    "yantra.pause": "pause",
    "yantra.resume": "resume",
    "yantra.charge": "charge",
    "yantra.estop": "estop",
    # VDA 5050 v2.1 6.10.2 standard actionType -> our cancel_order verb.
    "cancelOrder": "cancel_order",
}

#: How many consecutive state messages carry a finished/failed
#: actionState before it is dropped. VDA keeps actionStates until the
#: next order; repeating a few times bounds memory while surviving a
#: lost QoS-0 state message (the bridge dedupes by command id anyway).
ACTION_STATE_REPEATS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MqttTransport:
    """Publishes each robot's VDA state to its own topic on one broker.

    Also subscribes to each robot's ``instantActions`` and ``order``
    topics; queued messages are applied on the sim thread via
    :meth:`poll_commands` / :meth:`poll_orders`.
    """

    def __init__(self, robots: list[Robot], broker: str = "localhost",
                 port: int = 1883, client: "object | None" = None) -> None:
        if _paho is None and client is None:
            raise RuntimeError(
                "paho-mqtt is not installed; run 'pip install yantrasim[mqtt]' "
                "(or 'pip install paho-mqtt') to use --mqtt"
            )
        self.robots = robots
        self._conn_header_ids: dict[str, int] = {r.robot_id: 0 for r in robots}
        self._factsheet_header_ids: dict[str, int] = {r.robot_id: 0 for r in robots}
        # instantActions/order bookkeeping (MQTT thread <-> sim thread)
        self._serial_to_robot: dict[str, str] = {
            vda.sanitize_serial(r.robot_id): r.robot_id for r in robots}
        self._robots_by_serial: dict[str, Robot] = {
            vda.sanitize_serial(r.robot_id): r for r in robots}
        self._lock = threading.Lock()
        #: queued (serial, action_id, action_type, actionParameters)
        #: awaiting poll_commands
        self._incoming: list[tuple[str, str, str, list]] = []
        #: actionIds already accepted (dedupe against MQTT redelivery)
        self._seen_action_ids: set[str] = set()
        #: serial -> [[actionState dict, publishes-remaining], ...]
        self._results: dict[str, list[list[Any]]] = {}
        #: queued (serial, orderId, orderUpdateId, nodeIds, actions) awaiting poll_orders
        self._incoming_orders: list[tuple[str, str, int, list[str], list[dict[str, Any]]]] = []
        if client is not None:  # injected fake for offline tests
            self.client = client
            self._wire_client(self.client)
            self._subscribe_topics(self.client)
        else:
            self.client = _paho.Client(  # type: ignore[union-attr]
                callback_api_version=_paho.CallbackAPIVersion.VERSION2,
                protocol=_paho.MQTTv311,
            )
            self._wire_client(self.client)
            self.client.connect(broker, port)
            self.client.loop_start()
            self._subscribe_topics(self.client)
        self._announce("ONLINE")

    # -- Transport protocol ------------------------------------------------

    def publish(self, out: TickOutput) -> None:
        for state in out.states:
            self._attach_action_states(state)
            t = vda.topic(state["manufacturer"], state["serialNumber"], "state")
            # state: QoS 0, non-retained per spec
            self.client.publish(t, json.dumps(state), qos=0, retain=False)

    def poll_commands(self, apply_fn: Callable[[str, str], tuple[bool, str]]) -> int:
        """Drain queued instantActions and apply them on the sim thread.

        Same signature/contract as ``SupabaseTransport.poll_commands``, so
        the CLI main loop wires it identically: ``apply_fn(robot_id, verb)
        -> (ok, detail)``. Each processed action leaves an ``actionStates``
        entry that the next state messages carry back to the bridge.
        Returns how many actions reached a terminal FINISHED/FAILED status.
        """
        with self._lock:
            batch, self._incoming = self._incoming, []
        done = 0
        for serial, action_id, action_type, params in batch:
            robot_id = self._serial_to_robot.get(serial)

            if action_type == "stateRequest":
                ok, detail = self._handle_state_request(serial)
            elif action_type == "factsheetRequest":
                ok, detail = self._handle_factsheet_request(serial)
            elif action_type == "initPosition":
                ok, detail = self._handle_init_position(serial, params)
            else:
                verb = ACTION_VERBS.get(action_type)
                if verb is None:
                    ok, detail = False, f"unsupported actionType {action_type!r}"
                elif robot_id is None:
                    ok, detail = False, f"unknown robot serial {serial!r}"
                else:
                    ok, detail = apply_fn(robot_id, verb)

            state = {
                "actionId": action_id,
                "actionType": action_type,
                "actionStatus": "FINISHED" if ok else "FAILED",
                "resultDescription": detail,
            }
            self._stage_result(serial, state)
            done += 1
            log.info("instantAction %s %s -> %s: %s",
                     action_type, serial, state["actionStatus"], detail)
        return done

    def poll_orders(
        self,
        apply_order_fn: Callable[
            [str, str, int, list[str], list[dict[str, Any]]], tuple[bool, str]],
    ) -> int:
        """Drain queued ``order`` messages and apply them on the sim thread.

        ``apply_order_fn(robot_id, order_id, order_update_id, nodes, actions)
        -> (ok, detail)`` -- the CLI wires this to ``FleetSim.apply_order``,
        mirroring how :meth:`poll_commands` wires ``apply_fn`` to
        ``yantrasim.commands.apply_command``. Returns how many order
        messages were processed (accepted or rejected).

        Unlike an instantAction, an order gets NO ``actionStates`` ack --
        which is precisely why a rejection cannot just be logged locally.
        VDA 5050 2.1 6.5 reports it in band instead: ``FleetSim.apply_order``
        stages an ``errors[]`` entry of errorType ``orderUpdateError``
        (errorLevel WARNING) that rides the robot's next few state messages.
        That is the vehicle's answer, so it is deliberately staged in the
        sim core rather than here -- the transport does not know why a
        rejection happened, and every transport owes master control the
        same answer.
        """
        with self._lock:
            batch, self._incoming_orders = self._incoming_orders, []
        done = 0
        for serial, order_id, order_update_id, nodes, actions in batch:
            robot_id = self._serial_to_robot.get(serial)
            if robot_id is None:
                log.warning("order %s: unknown robot serial %r", order_id, serial)
                done += 1
                continue
            ok, detail = apply_order_fn(robot_id, order_id, order_update_id, nodes, actions)
            done += 1
            if ok:
                log.info("order %s (%s) -> accepted: %s",
                         order_id, robot_id, detail)
            else:
                log.warning("order %s (%s) -> rejected: %s",
                            order_id, robot_id, detail)
        return done

    def close(self) -> None:
        self._announce("OFFLINE")  # clean disconnect per spec
        if hasattr(self.client, "loop_stop"):
            self.client.loop_stop()
        if hasattr(self.client, "disconnect"):
            self.client.disconnect()

    # -- instantActions (VDA 5050 operator commands) -----------------------

    def handle_instant_actions(self, topic: str, payload: "bytes | str") -> None:
        """Parse one instantActions message and queue its actions.

        Called from the MQTT network thread (or directly by tests): only
        queues — application happens on the sim thread in
        :meth:`poll_commands`. Malformed payloads are dropped silently so
        one bad message can never kill the loop.
        """
        try:
            msg = json.loads(payload)
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        parts = topic.split("/")
        # topic path is authoritative for identity (VDA header rule)
        serial = parts[3] if len(parts) >= 5 else str(msg.get("serialNumber") or "")
        for action in msg.get("actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("actionId") or "")
            action_type = str(action.get("actionType") or "")
            if not action_id:
                continue
            params = action.get("actionParameters") or []
            if not isinstance(params, list):
                params = []
            with self._lock:
                if action_id in self._seen_action_ids:
                    continue  # duplicate delivery
                self._seen_action_ids.add(action_id)
                self._incoming.append((serial, action_id, action_type, params))

    # -- order (VDA 5050 master-control dispatch) ---------------------------

    def handle_order(self, topic: str, payload: "bytes | str") -> None:
        """Parse one ``order`` message and queue it for :meth:`poll_orders`.

        Only the pieces this simulator's task model can act on are pulled
        out: the flat list of ``nodeId``s (this simulator has no
        base/horizon split, so the whole order is treated as released) and
        every node action, in order. Malformed payloads and orders without
        an ``orderId`` are dropped silently, same policy as instantActions.
        """
        try:
            msg = json.loads(payload)
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        parts = topic.split("/")
        serial = parts[3] if len(parts) >= 5 else str(msg.get("serialNumber") or "")
        order_id = str(msg.get("orderId") or "")
        if not order_id:
            return
        try:
            order_update_id = int(msg.get("orderUpdateId") or 0)
        except (TypeError, ValueError):
            order_update_id = 0
        nodes_field = msg.get("nodes")
        if not isinstance(nodes_field, list):
            nodes_field = []
        node_ids: list[str] = []
        actions: list[dict[str, Any]] = []
        for node in nodes_field:
            if not isinstance(node, dict):
                continue
            node_id = node.get("nodeId")
            if node_id:
                node_ids.append(str(node_id))
            for action in node.get("actions") or []:
                if isinstance(action, dict):
                    actions.append(action)
        with self._lock:
            self._incoming_orders.append((serial, order_id, order_update_id, node_ids, actions))

    def _attach_action_states(self, state: dict[str, Any]) -> None:
        """Append pending command actionStates to one outgoing state msg."""
        serial = state.get("serialNumber", "")
        with self._lock:
            entries = self._results.get(serial)
            if not entries:
                return
            state["actionStates"] = vda.merge_action_states(
                state.get("actionStates") or [], [dict(e[0]) for e in entries])
            for e in entries:
                e[1] -= 1
            self._results[serial] = [e for e in entries if e[1] > 0]

    def _stage_result(self, serial: str, state: dict[str, Any]) -> None:
        """Queue one actionState to ride the next few state messages."""
        if serial not in self._serial_to_robot:
            return  # unknown robot -> no state message would ever carry it
        with self._lock:
            self._results.setdefault(serial, []).append([state, ACTION_STATE_REPEATS])

    # -- standard instantActions handled directly ---------------------------

    def _handle_state_request(self, serial: str) -> tuple[bool, str]:
        """``stateRequest``: force-publish a fresh state message right now."""
        robot = self._robots_by_serial.get(serial)
        if robot is None:
            return False, f"unknown robot serial {serial!r}"
        robot.header_id += 1
        state = vda.build_state(robot, header_id=robot.header_id, timestamp=_now_iso())
        # This message must carry the same evidence a tick-published one
        # would: the sim's staged terminal actionStates and errors[] (e.g.
        # an orderUpdateError) as well as this transport's own command acks.
        # Otherwise what master control learns depends on which of the two
        # publishers got there first.
        attach_pending(robot, state)
        self._attach_action_states(state)
        t = vda.topic(robot.vendor, robot.robot_id, "state")
        self.client.publish(t, json.dumps(state), qos=0, retain=False)
        return True, "immediate state published"

    def _handle_factsheet_request(self, serial: str) -> tuple[bool, str]:
        """``factsheetRequest``: publish the static factsheet payload."""
        robot = self._robots_by_serial.get(serial)
        if robot is None:
            return False, f"unknown robot serial {serial!r}"
        self._factsheet_header_ids[robot.robot_id] = (
            self._factsheet_header_ids.get(robot.robot_id, 0) + 1)
        fs = vda.build_factsheet(
            robot, header_id=self._factsheet_header_ids[robot.robot_id],
            timestamp=_now_iso())
        t = vda.topic(robot.vendor, robot.robot_id, "factsheet")
        # factsheet: QoS 1, retained (rarely changes; a late subscriber
        # should still get the vehicle's capabilities without re-asking).
        self.client.publish(t, json.dumps(fs), qos=1, retain=True)
        return True, "factsheet published"

    def _handle_init_position(self, serial: str, params: list[Any]) -> tuple[bool, str]:
        """``initPosition``: set node/pose from actionParameters.

        Clears an active localization fault (the fault's own hint already
        tells an operator to "re-initialize position at the nearest
        fiducial marker" -- this is that recovery path). Refused while any
        other fault is active, or while the robot is actively moving/
        working, since rewriting its pose out from under motion makes no
        physical sense.
        """
        robot = self._robots_by_serial.get(serial)
        if robot is None:
            return False, f"unknown robot serial {serial!r}"
        if robot.status == "fault" and robot.fault_kind != "localization":
            return False, (
                f"{robot.robot_id} has an active {robot.fault_kind} fault; "
                "cannot initPosition")
        if robot.status not in ("idle", "fault"):
            return False, f"{robot.robot_id} must be idle to (re)initialize position"

        values: dict[str, Any] = {}
        for p in params or []:
            if isinstance(p, dict) and "key" in p:
                values[p["key"]] = p.get("value")

        node_id = values.get("lastNodeId")
        if node_id is not None:
            node_id = str(node_id)
            if node_id not in world.WAYPOINTS:
                return False, f"unknown lastNodeId {node_id!r}"
            wp = world.WAYPOINTS[node_id]
            robot.node = node_id
            robot.x, robot.y = wp.x, wp.y

        for key, attr in (("x", "x"), ("y", "y"), ("theta", "theta")):
            if key in values:
                try:
                    setattr(robot, attr, float(values[key]))
                except (TypeError, ValueError):
                    pass

        robot.path = []
        robot.last_node_sequence_id = 0
        if robot.fault_kind == "localization":
            robot.fault_kind = None
            robot.fault_ticks_left = 0
            robot.status = robot.resume_status or "idle"
        detail = f"{robot.robot_id} position initialized"
        if node_id:
            detail += f" at {node_id}"
        return True, detail

    # -- internals ---------------------------------------------------------

    def _wire_client(self, client: Any) -> None:
        """Install callbacks; tolerate minimal fakes without them."""
        try:
            client.on_message = self._handle_message
            client.on_connect = self._handle_connect
        except AttributeError:  # pragma: no cover - exotic fakes
            pass

    def _handle_connect(self, client: Any, userdata: Any, flags: Any,
                        reason_code: Any, properties: Any = None) -> None:
        # (Re)subscribe on every connect so reconnects keep the command
        # path, and re-announce ONLINE so a network blip is self-healing
        # on the retained connection topic (it does not just stay stuck at
        # whatever state it last had before the drop).
        self._subscribe_topics(client)
        self._announce("ONLINE")

    def _subscribe_topics(self, client: Any) -> None:
        if not hasattr(client, "subscribe"):
            return  # recording fakes without a broker
        for r in self.robots:
            # instantActions / order: QoS 0 per spec (both are
            # masterControl -> AGV, not retained).
            client.subscribe(vda.topic(r.vendor, r.robot_id, "instantActions"), qos=0)
            client.subscribe(vda.topic(r.vendor, r.robot_id, "order"), qos=0)

    def _handle_message(self, client: Any, userdata: Any, message: Any) -> None:
        if message.topic.endswith("/instantActions"):
            self.handle_instant_actions(message.topic, message.payload)
        elif message.topic.endswith("/order"):
            self.handle_order(message.topic, message.payload)

    def _announce(self, connection_state: str) -> None:
        ts = _now_iso()
        for r in self.robots:
            self._conn_header_ids[r.robot_id] += 1
            msg = vda.build_connection(
                r, self._conn_header_ids[r.robot_id], ts, connection_state)
            t = vda.topic(r.vendor, r.robot_id, "connection")
            # connection: QoS 1, retained per spec
            self.client.publish(t, json.dumps(msg), qos=1, retain=True)
