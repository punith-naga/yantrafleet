"""MQTT transport with proper VDA 5050 topic structure.

Topics: ``uagv/v2/<manufacturer>/<serialNumber>/state`` (QoS 0, not
retained) and ``.../connection`` (QoS 1, retained) per the spec.

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

``paho-mqtt`` is an OPTIONAL dependency: this module always imports, and
only constructing ``MqttTransport`` requires paho (install with
``pip install yantrasim[mqtt]``). Topic building lives in ``yantrasim.vda``
so it stays unit-testable without paho.

Note: a real AGV holds its own MQTT connection with a per-vehicle
last-will (CONNECTIONBROKEN) on its connection topic. This simulator
multiplexes 10 vehicles over ONE client, so it publishes ONLINE for each
robot at startup and OFFLINE on clean shutdown, but cannot register ten
last-wills — an accepted simulator-only deviation, documented here.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .. import vda
from ..sim import Robot, TickOutput

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

    Also subscribes to each robot's ``instantActions`` topic; queued
    actions are applied on the sim thread via :meth:`poll_commands`.
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
        # instantActions bookkeeping (MQTT thread <-> sim thread)
        self._serial_to_robot: dict[str, str] = {
            vda.sanitize_serial(r.robot_id): r.robot_id for r in robots}
        self._lock = threading.Lock()
        #: queued (serial, action_id, action_type) awaiting poll_commands
        self._incoming: list[tuple[str, str, str]] = []
        #: actionIds already accepted (dedupe against MQTT redelivery)
        self._seen_action_ids: set[str] = set()
        #: serial -> [[actionState dict, publishes-remaining], ...]
        self._results: dict[str, list[list[Any]]] = {}
        if client is not None:  # injected fake for offline tests
            self.client = client
            self._wire_client(self.client)
            self._subscribe_instant_actions(self.client)
        else:
            self.client = _paho.Client(  # type: ignore[union-attr]
                callback_api_version=_paho.CallbackAPIVersion.VERSION2,
                protocol=_paho.MQTTv311,
            )
            self._wire_client(self.client)
            self.client.connect(broker, port)
            self.client.loop_start()
            self._subscribe_instant_actions(self.client)
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
        Returns how many actions were processed.
        """
        with self._lock:
            batch, self._incoming = self._incoming, []
        done = 0
        for serial, action_id, action_type in batch:
            verb = ACTION_VERBS.get(action_type)
            robot_id = self._serial_to_robot.get(serial)
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
            if serial in self._serial_to_robot:  # else no state msg would carry it
                with self._lock:
                    self._results.setdefault(serial, []).append(
                        [state, ACTION_STATE_REPEATS])
            done += 1
            log.info("instantAction %s %s -> %s: %s",
                     action_type, serial, state["actionStatus"], detail)
        return done

    def close(self) -> None:
        self._announce("OFFLINE")  # clean disconnect per spec
        if hasattr(self.client, "loop_stop"):
            self.client.loop_stop()
        if hasattr(self.client, "disconnect"):
            self.client.disconnect()

    # -- instantActions (VDA 5050 operator commands) -----------------------

    def handle_instant_actions(self, topic: str, payload: "bytes | str") -> None:
        """Parse one instantActions message and queue its yantra actions.

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
            with self._lock:
                if action_id in self._seen_action_ids:
                    continue  # duplicate delivery
                self._seen_action_ids.add(action_id)
                self._incoming.append((serial, action_id, action_type))

    def _attach_action_states(self, state: dict[str, Any]) -> None:
        """Append pending command actionStates to one outgoing state msg."""
        serial = state.get("serialNumber", "")
        with self._lock:
            entries = self._results.get(serial)
            if not entries:
                return
            state["actionStates"] = list(state.get("actionStates") or []) + [
                dict(e[0]) for e in entries]
            for e in entries:
                e[1] -= 1
            self._results[serial] = [e for e in entries if e[1] > 0]

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
        # (Re)subscribe on every connect so reconnects keep the command path.
        self._subscribe_instant_actions(client)

    def _subscribe_instant_actions(self, client: Any) -> None:
        if not hasattr(client, "subscribe"):
            return  # recording fakes without a broker
        for r in self.robots:
            # instantActions: QoS 0 per spec
            client.subscribe(vda.topic(r.vendor, r.robot_id, "instantActions"), qos=0)

    def _handle_message(self, client: Any, userdata: Any, message: Any) -> None:
        if message.topic.endswith("/instantActions"):
            self.handle_instant_actions(message.topic, message.payload)

    def _announce(self, connection_state: str) -> None:
        ts = _now_iso()
        for r in self.robots:
            self._conn_header_ids[r.robot_id] += 1
            msg = vda.build_connection(
                r, self._conn_header_ids[r.robot_id], ts, connection_state)
            t = vda.topic(r.vendor, r.robot_id, "connection")
            # connection: QoS 1, retained per spec
            self.client.publish(t, json.dumps(msg), qos=1, retain=True)
