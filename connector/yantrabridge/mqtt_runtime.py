"""Live MQTT broker reconnection (v0.18).

``CONNECTOR_MQTT_HOST``/``PORT``/``TOPIC``/``USERNAME``/``PASSWORD`` in
``public.app_config`` (see ``supabase/0018_app_config.sql``) can change
while ``python -m yantrabridge --mqtt-host ...`` is already running.
:class:`MqttConnectionManager` is the piece that decides *when* that
actually requires tearing down the current broker connection and
building a new one — and, just as importantly, when it must NOT (an
unrelated ``app_config`` poll, or a poll where nothing changed, must
never cycle the connection).

Design choice, worth stating explicitly: if the NEW broker is
unreachable, this manager logs a warning and keeps the OLD connection
running rather than tearing it down speculatively. The alternative
(disconnect first, then try to connect) would leave the bridge with NO
broker connection at all if the new one fails — worse than staying on a
connection that was working a moment ago. The cost of this choice: a
transient failure to reach the new broker means the bridge keeps
publishing/consuming on the stale one until a later poll's connect
attempt succeeds; that poll retries on every subsequent tick because the
"applied" params are only updated on success, so the diff against the
desired params never clears until the swap actually lands.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("yantrabridge")

#: The MQTT connection params tracked for a diff -> reconnect decision.
MQTT_PARAM_KEYS = ("host", "port", "topic", "username", "password")


class MqttConnectionManager:
    """Owns the live :class:`~yantrabridge.sources.MqttSource`.

    ``build_source(params)`` is a factory the caller provides (closes
    over the on_state/on_connection callbacks); ``params`` is a dict with
    keys ``host``/``port``/``topic``/``username``/``password``.
    """

    def __init__(self, build_source: Callable[[dict], Any],
                initial_params: dict) -> None:
        self._build = build_source
        self._params = dict(initial_params)
        self._source = self._build(self._params)
        self._source.connect_start()

    @property
    def source(self) -> Any:
        """The currently-live MqttSource. Never bind a callback to this
        object directly — reconnects replace it — read this property at
        call time instead (see ``__main__.run_mqtt``'s publish indirection)."""
        return self._source

    @property
    def params(self) -> dict:
        return dict(self._params)

    def apply(self, new_params: dict) -> bool:
        """Reconnect iff ``new_params`` differs from what's currently
        applied. Returns True iff a reconnect actually happened."""
        new_params = dict(new_params)
        if new_params == self._params:
            return False
        log.info(
            "mqtt broker config changed (%s:%s/%s -> %s:%s/%s) — reconnecting",
            self._params.get("host"), self._params.get("port"),
            self._params.get("topic"),
            new_params.get("host"), new_params.get("port"),
            new_params.get("topic"),
        )
        try:
            new_source = self._build(new_params)
            new_source.connect_start()
        except Exception as exc:  # bad host/port, connection refused, ...
            log.warning(
                "mqtt reconnect to %s:%s failed (%s) — keeping the existing "
                "connection; will retry on the next config poll",
                new_params.get("host"), new_params.get("port"), exc,
            )
            return False
        old_source = self._source
        self._source = new_source
        self._params = new_params
        try:
            old_source.disconnect_stop()
        except Exception as exc:  # never let teardown of the OLD connection
            log.debug("error tearing down previous mqtt connection: %s", exc)
        return True

    def stop(self) -> None:
        try:
            self._source.disconnect_stop()
        except Exception as exc:
            log.debug("error during final mqtt disconnect: %s", exc)


class LiveMqttConfig:
    """One poll tick's worth of work: refresh the battery threshold
    (simple value swap) and reconnect the broker iff its params actually
    changed. Bundled into its own class (rather than left as closures
    inside ``__main__.run_mqtt``) so it's unit-testable without spinning
    up a real/fake MQTT connection loop — see
    ``connector/tests/test_mqtt_runtime.py``.

    ``config`` is a ``yantracore.runtime_config.TablePoller`` (or
    anything with a matching ``.get()``/``.poll_once()``); ``manager`` is
    a :class:`MqttConnectionManager`; ``deduper`` is anything with a
    mutable ``battery_threshold`` attribute (``Translator.deduper`` in
    practice). ``defaults`` supplies the CLI-flag/hardcoded fallback used
    whenever ``config`` has no row for a key — see the precedence note on
    :class:`MqttConnectionManager`'s caller in ``__main__.py``: a live
    config value, once present, always wins over these.
    """

    def __init__(self, config: Any, manager: MqttConnectionManager,
                deduper: Any, defaults: dict) -> None:
        self.config = config
        self.manager = manager
        self.deduper = deduper
        self.defaults = defaults

    def desired_mqtt_params(self) -> dict:
        from yantracore.runtime_config import coerce_int, resolve_value

        d = self.defaults
        return {
            "host": resolve_value(None, self.config, "CONNECTOR_MQTT_HOST",
                                  d["mqtt_host"]),
            "port": resolve_value(None, self.config, "CONNECTOR_MQTT_PORT",
                                  d["mqtt_port"], coerce_int),
            "topic": resolve_value(None, self.config, "CONNECTOR_MQTT_TOPIC",
                                   d["mqtt_topic"]),
            "username": resolve_value(None, self.config, "CONNECTOR_MQTT_USERNAME",
                                      d.get("mqtt_username")),
            "password": resolve_value(None, self.config, "CONNECTOR_MQTT_PASSWORD",
                                      d.get("mqtt_password")),
        }

    def poll_and_apply(self) -> bool:
        """Refresh from ``config``, apply the battery threshold live,
        and reconnect the broker iff its params changed. Returns True
        iff a reconnect happened."""
        from yantracore.runtime_config import coerce_float, resolve_value

        self.config.poll_once()
        self.deduper.battery_threshold = resolve_value(
            None, self.config, "CONNECTOR_BATTERY_THRESHOLD",
            self.defaults["battery_threshold"], coerce_float)
        return self.manager.apply(self.desired_mqtt_params())
