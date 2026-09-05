"""Operator-command publisher: backend ``commands`` table -> VDA 5050
instantActions -> robot, closed by the actionStates the robot's next
``state`` message carries back.

The full loop (the real VDA 5050 pattern):

1. :meth:`CommandPublisher.poll` fetches ``status=approved`` rows from
   the ``commands`` table (PostgREST, same httpx patterns as
   :class:`yantrabridge.sink.SupabaseSink`) and publishes each once as an
   instantActions message on the robot's topic
   ``uagv/v2/<manufacturer>/<serialNumber>/instantActions`` with
   ``actions: [{actionType: "yantra.<cmd>", actionId: <command uuid>,
   blockingType: "HARD"}]``.
2. The robot (yantrasim's ``MqttTransport``, or a real VDA AGV adapter)
   applies the action and reports an ``actionStates`` entry
   (``actionStatus FINISHED|FAILED`` + ``resultDescription``) in its next
   state messages.
3. :meth:`CommandPublisher.handle_state` — called for every state message
   the bridge already consumes — spots actionIds it published and
   PATCHes the command row to ``executed`` / ``failed`` with the result
   description in ``note``.

The DB status vocabulary is unchanged (pending/approved/rejected/
executed/failed — there is deliberately NO intermediate 'sent' state):
a published-but-unacked command stays ``approved`` in the table and is
deduped in memory by command id, so the bridge never republishes it.
Caveat, documented: because instantActions are QoS 0 and published
exactly once, a command published while the robot is offline is lost
until a human re-approves (a fresh row); rows the bridge cannot yet
route (robot never seen, lookup failing) are NOT marked published and
retry on the next poll.

All I/O is injectable (``httpx.MockTransport`` + any ``publish(topic,
payload)`` callable), so unit tests run fully offline.

v0.17, SECURITY: a bridge serves exactly one site. ``site`` (or
``YANTRA_SITE_ID``) is REQUIRED — construction raises without one — and
both the command poll and the robot lookup are filtered by it. Before
v0.17 the filter was opt-in, which meant an unfiltered bridge holding a
service key would happily publish an ``estop`` instantAction that an
anonymous demo-sandbox visitor had queued against a real robot id. See
supabase/0017_demo_command_scope.sql and docs/SECURITY.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from yantrabridge.sink import DEFAULT_SUPABASE_KEY, DEFAULT_SUPABASE_URL

log = logging.getLogger(__name__)

#: command verbs the platform supports (matches the commands.cmd CHECK).
VERBS = ("pause", "resume", "charge", "estop")

#: VDA topic prefix pieces (must match yantrasim.vda / MqttSource).
INTERFACE_NAME = "uagv"
VDA_MAJOR = "v2"
VDA_VERSION = "2.1.0"

_SERIAL_BAD = re.compile(r"[^A-Za-z0-9_.:]")
_SEGMENT_BAD = re.compile(r"[/$]")

#: Explicit, deliberate opt-out of the command site filter (v0.17).
ANY_SITE = "*"

_NO_SITE = (
    "CommandPublisher: a site is required. This publisher turns approved "
    "`commands` rows into VDA 5050 instantActions on real robots, and it "
    "usually holds a key that bypasses RLS — without a site it would also "
    "execute a command an anonymous demo-sandbox visitor queued (see "
    "supabase/0009_demo_sandbox.sql and docs/SECURITY.md). Pass "
    "`yantrabridge --site <SITE>`, or set YANTRA_SITE_ID=<SITE>, or — if "
    "this bridge really must serve every site — pass --site '*' to opt out "
    "deliberately."
)


def resolve_site(site: str | None = None) -> str:
    """The one site whose approved commands this bridge may publish.

    Precedence: explicit arg > ``YANTRA_SITE_ID``. There is NO default:
    guessing a site would silently stop a multi-site deployment's other
    bridges, and defaulting to "every site" is the bug this exists to fix.
    Raises :class:`ValueError` when neither is set, so the failure lands at
    startup with an actionable message instead of at the first estop.
    """
    resolved = (site or os.environ.get("YANTRA_SITE_ID") or "").strip()
    if not resolved:
        raise ValueError(_NO_SITE)
    return resolved


def _sanitize_serial(robot_id: str) -> str:
    """VDA-legal serialNumber (charset A-Za-z0-9_.:), e.g. AMR-07 -> AMR_07.

    In MQTT mode the backend's robot ids are already sanitized serials;
    sanitizing again is a no-op there and a safety net for raw fleet ids.
    """
    return _SERIAL_BAD.sub("_", robot_id)


def _sanitize_segment(segment: str) -> str:
    return _SEGMENT_BAD.sub("_", segment)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class CommandPublisher:
    """Publishes approved operator commands as VDA instantActions and
    closes them from the actionStates seen in state messages.

    Parameters
    ----------
    mqtt_publish:
        ``callable(topic: str, payload: str)`` that puts one message on
        the wire (e.g. ``MqttSource.publish``). Injectable for tests.
    url, key:
        Supabase project URL / anon key (env vars and embedded defaults
        as fallback, mirroring ``SupabaseSink``).
    site:
        The one facility this bridge serves: only commands with that
        ``site_id`` are polled, and only robots at that site are
        routable. REQUIRED as of v0.17 — falls back to
        ``YANTRA_SITE_ID`` and otherwise raises ``ValueError`` at
        construction. ``"*"`` restores the pre-v0.17 unfiltered
        behaviour, loudly and on purpose.
    transport:
        Optional ``httpx.BaseTransport`` (``httpx.MockTransport``) for
        offline tests.
    """

    def __init__(
        self,
        mqtt_publish: Callable[[str, str], None],
        url: str | None = None,
        key: str | None = None,
        *,
        site: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        limit: int = 20,
    ) -> None:
        self._publish = mqtt_publish
        self.site = resolve_site(site)
        self.limit = limit
        self._warned_missing_site = False
        if self.site == ANY_SITE:
            log.warning(
                "command gate: site filter DISABLED (--site '%s') — this "
                "bridge will publish approved commands from EVERY site, "
                "including an anonymous demo sandbox if 0009 is applied. "
                "See docs/SECURITY.md.", ANY_SITE)
        else:
            log.info("command gate: only approved commands at site %r will "
                     "be published", self.site)
        resolved_url = (url or os.environ.get("SUPABASE_URL")
                        or DEFAULT_SUPABASE_URL).rstrip("/")
        resolved_key = key or os.environ.get("SUPABASE_KEY") or DEFAULT_SUPABASE_KEY
        self._client = httpx.Client(
            base_url=f"{resolved_url}/rest/v1",
            headers={
                "apikey": resolved_key,
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )
        self._lock = threading.Lock()
        #: command id -> serial, published and awaiting an actionState ack
        self._pending: dict[str, str] = {}
        #: command ids fully closed (PATCHed executed/failed)
        self._done: set[str] = set()
        #: serial -> manufacturer, learned from consumed state messages
        self._manufacturers: dict[str, str] = {}
        self._header_ids: dict[str, int] = {}

    # -- publishing side (poll thread) --------------------------------------

    def poll(self) -> int:
        """Publish new approved commands **for this bridge's site**.

        Returns how many went out. Never raises: backend/broker trouble is
        logged and retried on the next poll — the bridge must not die
        because the gate is flaky.

        v0.17, SECURITY: the ``site_id`` filter is mandatory (see
        :func:`resolve_site`). An approved row is only ever an
        instantAction on a real robot, and this publisher normally holds a
        key that bypasses RLS, so an unfiltered poll would dispatch a
        command an anonymous demo-sandbox visitor queued for a real
        ``robot_id`` (supabase/0017_demo_command_scope.sql closes the
        insert side; this closes the execute side). Three controls, all
        independent: the query is filtered server-side, every returned row
        is re-checked here, and :meth:`_manufacturer_for` will only resolve
        a robot that is registered at this bridge's site.
        """
        params: dict[str, str] = {
            "status": "eq.approved",
            "order": "created_at.asc",
            "limit": str(self.limit),
            "select": "id,robot_id,cmd,site_id",
        }
        if self.site != ANY_SITE:
            params["site_id"] = f"eq.{self.site}"
        try:
            resp = self._client.get("/commands", params=params)
            resp.raise_for_status()
            rows = resp.json() or []
        except Exception as exc:  # noqa: BLE001 - availability over purity
            log.warning("command poll failed: %s", exc)
            return 0

        rows = [row for row in rows if self._site_ok(row)]
        published = 0
        for row in rows:
            cid = str(row.get("id") or "")
            if not cid:
                continue
            with self._lock:
                if cid in self._pending or cid in self._done:
                    continue  # already published / already closed
            cmd = str(row.get("cmd") or "")
            if cmd not in VERBS:
                # cannot reach a robot: close the row immediately
                self._patch_command(cid, ok=False,
                                    note=f"unsupported command {cmd!r}")
                with self._lock:
                    self._done.add(cid)
                continue
            serial = _sanitize_serial(str(row.get("robot_id") or ""))
            manufacturer = self._manufacturer_for(serial)
            if manufacturer is None:
                log.warning("command %s: unknown manufacturer for robot %s; "
                            "will retry", cid, serial)
                continue  # not marked published -> retried next poll
            topic = "/".join((INTERFACE_NAME, VDA_MAJOR,
                              _sanitize_segment(manufacturer), serial,
                              "instantActions"))
            self._header_ids[topic] = self._header_ids.get(topic, 0) + 1
            payload = json.dumps({
                "headerId": self._header_ids[topic],
                "timestamp": _now_iso(),
                "version": VDA_VERSION,
                "manufacturer": manufacturer,
                "serialNumber": serial,
                "actions": [{
                    "actionType": f"yantra.{cmd}",
                    "actionId": cid,
                    "blockingType": "HARD",
                    "actionParameters": [],
                }],
            })
            try:
                self._publish(topic, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("command %s: publish failed: %s", cid, exc)
                continue  # retried next poll
            with self._lock:
                self._pending[cid] = serial
            published += 1
            log.info("command %s (%s %s) published on %s", cid, cmd, serial, topic)
        return published

    # -- publishing side: VDA 5050 orders (master-control dispatch) ---------

    def publish_order(
        self,
        robot_id: str,
        order_id: str,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]] | None = None,
        *,
        order_update_id: int = 0,
    ) -> bool:
        """Publish one VDA 5050 v2.1 ``order`` message to a robot's topic.

        This is the outbound half of the order contract that closes the
        core VDA 5050 gap: a caller with a mission to dispatch (a console
        action, a scheduler) builds ``nodes``/``edges`` in VDA node/edge
        shape -- each node at least ``{"nodeId", "sequenceId", "released",
        "actions": [...]}`` -- and this publishes them as one order on
        ``uagv/v2/<manufacturer>/<serialNumber>/order``. Unlike an
        instantActions command there is no ``commands`` table row to poll
        or ack here: a caller tracks progress/completion the same way any
        VDA consumer does, by watching the resulting state messages'
        ``orderId``/``orderUpdateId``/``nodeStates``/``edgeStates``/
        ``actionStates`` (translate.translate_state already surfaces
        these).

        Returns whether the message was actually published (``False`` when
        the robot's manufacturer cannot yet be resolved -- same
        retry-by-caller contract ``poll()`` uses for an unrouteable
        command; never raises).
        """
        serial = _sanitize_serial(robot_id)
        manufacturer = self._manufacturer_for(serial)
        if manufacturer is None:
            log.warning("order %s: unknown manufacturer for robot %s",
                        order_id, serial)
            return False
        topic = "/".join((INTERFACE_NAME, VDA_MAJOR,
                          _sanitize_segment(manufacturer), serial, "order"))
        self._header_ids[topic] = self._header_ids.get(topic, 0) + 1
        payload = json.dumps({
            "headerId": self._header_ids[topic],
            "timestamp": _now_iso(),
            "version": VDA_VERSION,
            "manufacturer": manufacturer,
            "serialNumber": serial,
            "orderId": order_id,
            "orderUpdateId": order_update_id,
            "nodes": nodes,
            "edges": edges or [],
        })
        try:
            self._publish(topic, payload)
        except Exception as exc:  # noqa: BLE001 - availability over purity
            log.warning("order %s: publish failed: %s", order_id, exc)
            return False
        log.info("order %s (%s) published on %s", order_id, serial, topic)
        return True

    # -- ack side (state-message thread) ------------------------------------

    def handle_state(self, msg: dict[str, Any]) -> None:
        """Learn robot identity and close commands acked via actionStates.

        Call for every consumed VDA state message. For each actionState
        whose actionId matches a command this publisher sent, PATCH the
        command row to ``executed`` (FINISHED) or ``failed`` (FAILED) with
        the robot's resultDescription in ``note``. Idempotent per command:
        once PATCHed, later repeats of the actionState are ignored.
        """
        serial = msg.get("serialNumber")
        manufacturer = msg.get("manufacturer")
        if serial and manufacturer:
            with self._lock:
                self._manufacturers[str(serial)] = str(manufacturer)
        for action in msg.get("actionStates") or []:
            if not isinstance(action, dict):
                continue
            aid = str(action.get("actionId") or "")
            status = str(action.get("actionStatus") or "").upper()
            if status not in ("FINISHED", "FAILED"):
                continue  # RUNNING/WAITING etc. — not terminal
            with self._lock:
                if aid not in self._pending:
                    continue  # not ours, or already closed
            ok = status == "FINISHED"
            note = str(action.get("resultDescription") or "")
            if self._patch_command(aid, ok=ok, note=note):
                with self._lock:
                    self._pending.pop(aid, None)
                    self._done.add(aid)
                log.info("command %s closed: %s (%s)",
                         aid, "executed" if ok else "failed", note)
            # on PATCH failure the id stays pending; the actionState
            # repeats in the next state messages -> natural retry.

    # -- internals -----------------------------------------------------------

    def _site_ok(self, row: dict[str, Any]) -> bool:
        """Client-side half of the site gate — never trust the projection.

        A row whose ``site_id`` disagrees with this bridge's site is dropped
        and logged: getting here means the server-side filter was not
        honoured (a stale PostgREST, a proxy that ate the query string, a
        stand-in backend), which is exactly what the second control is for.

        A row with NO ``site_id`` key is a backend that did not return the
        column rather than a mismatch; it is still published, with a
        one-time warning, so a bridge pointed at an older backend does not
        silently stop. The server-side filter is the control there — and
        :meth:`_manufacturer_for` still refuses to route a robot that is
        not registered at this site.
        """
        if self.site == ANY_SITE:
            return True
        if "site_id" not in row:
            if not self._warned_missing_site:
                self._warned_missing_site = True
                log.warning(
                    "command poll: backend returned no site_id column — the "
                    "server-side site filter (site_id=eq.%s) is the only "
                    "control on this connection", self.site)
            return True
        if row.get("site_id") != self.site:
            log.warning(
                "command %s REFUSED: it belongs to site %r, this bridge "
                "serves %r (the backend ignored the site filter)",
                row.get("id"), row.get("site_id"), self.site)
            return False
        return True

    def _manufacturer_for(self, serial: str) -> str | None:
        """Manufacturer for a serial: state-message registry, then the
        robots table (``vendor`` column), else None (caller retries).

        v0.17: the table lookup is site-scoped, so a command naming a robot
        that is not registered at this bridge's site can never be routed
        even if it somehow reached :meth:`poll`.
        """
        with self._lock:
            known = self._manufacturers.get(serial)
        if known:
            return known
        params = {"id": f"eq.{serial}", "select": "id,vendor", "limit": "1"}
        if self.site != ANY_SITE:
            params["site_id"] = f"eq.{self.site}"
        try:
            resp = self._client.get("/robots", params=params)
            resp.raise_for_status()
            rows = resp.json() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("robot lookup for %s failed: %s", serial, exc)
            return None
        vendor = rows[0].get("vendor") if rows else None
        if vendor:
            with self._lock:
                self._manufacturers[serial] = str(vendor)
            return str(vendor)
        return None

    def _patch_command(self, cid: str, *, ok: bool, note: str) -> bool:
        body = {
            "status": "executed" if ok else "failed",
            "note": note,
            "executed_at": _now_iso(),
        }
        try:
            resp = self._client.patch(
                "/commands", params={"id": f"eq.{cid}"}, json=body)
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("command ack failed for %s: %s", cid, exc)
            return False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CommandPublisher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
