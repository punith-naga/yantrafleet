"""MQTT transport with proper VDA 5050 topic structure.

Topics: ``uagv/v2/<manufacturer>/<serialNumber>/state`` (QoS 0, not
retained) and ``.../connection`` (QoS 1, retained) per the spec.

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
from datetime import datetime, timezone

from .. import vda
from ..sim import Robot, TickOutput

try:  # optional dependency
    import paho.mqtt.client as _paho
except ImportError:  # pragma: no cover - exercised via PAHO_AVAILABLE tests
    _paho = None

PAHO_AVAILABLE = _paho is not None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MqttTransport:
    """Publishes each robot's VDA state to its own topic on one broker."""

    def __init__(self, robots: list[Robot], broker: str = "localhost",
                 port: int = 1883, client: "object | None" = None) -> None:
        if _paho is None and client is None:
            raise RuntimeError(
                "paho-mqtt is not installed; run 'pip install yantrasim[mqtt]' "
                "(or 'pip install paho-mqtt') to use --mqtt"
            )
        self.robots = robots
        self._conn_header_ids: dict[str, int] = {r.robot_id: 0 for r in robots}
        if client is not None:  # injected fake for offline tests
            self.client = client
        else:
            self.client = _paho.Client(  # type: ignore[union-attr]
                callback_api_version=_paho.CallbackAPIVersion.VERSION2,
                protocol=_paho.MQTTv311,
            )
            self.client.connect(broker, port)
            self.client.loop_start()
        self._announce("ONLINE")

    # -- Transport protocol ------------------------------------------------

    def publish(self, out: TickOutput) -> None:
        for state in out.states:
            t = vda.topic(state["manufacturer"], state["serialNumber"], "state")
            # state: QoS 0, non-retained per spec
            self.client.publish(t, json.dumps(state), qos=0, retain=False)

    def close(self) -> None:
        self._announce("OFFLINE")  # clean disconnect per spec
        if hasattr(self.client, "loop_stop"):
            self.client.loop_stop()
        if hasattr(self.client, "disconnect"):
            self.client.disconnect()

    # -- internals ---------------------------------------------------------

    def _announce(self, connection_state: str) -> None:
        ts = _now_iso()
        for r in self.robots:
            self._conn_header_ids[r.robot_id] += 1
            msg = vda.build_connection(
                r, self._conn_header_ids[r.robot_id], ts, connection_state)
            t = vda.topic(r.vendor, r.robot_id, "connection")
            # connection: QoS 1, retained per spec
            self.client.publish(t, json.dumps(msg), qos=1, retain=True)
