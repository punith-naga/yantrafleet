"""Supabase (PostgREST) transport: bulk upsert robots, insert alerts.

Free-tier friendly per the Supabase spec:
  * one bulk POST per table per tick (single transaction each)
  * ``Prefer: resolution=merge-duplicates, return=minimal`` for upserts
  * ``resolution=ignore-duplicates`` for alerts, whose deterministic ids
    make retries idempotent
The anon (publishable) key is client-safe and embedded as a default;
override with env vars SUPABASE_URL / SUPABASE_KEY or CLI flags.

An injectable ``httpx.Client`` keeps every test offline
(``httpx.MockTransport``) — supabase.co is never reached in CI.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from ..sim import TickOutput
from ..translate import alert_row, fleet_meta_row, robot_row, telemetry_row

log = logging.getLogger(__name__)

DEFAULT_URL = "https://flwyvhsmgrrqpmhcqlzd.supabase.co"
DEFAULT_KEY = "sb_publishable_7rqvPRggmPDRKNL8Jurcqg_Hf531puf"


def resolve_config(url: str | None = None, key: str | None = None) -> tuple[str, str]:
    """Precedence: explicit arg > env var > embedded default."""
    return (
        (url or os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/"),
        key or os.environ.get("SUPABASE_KEY") or DEFAULT_KEY,
    )


class SupabaseTransport:
    """Translates each tick to table rows and POSTs them via PostgREST."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        writer_id: str = "yantrasim",
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
        history_every: int = 3,
    ) -> None:
        self.base_url, self.key = resolve_config(url, key)
        self.rest = f"{self.base_url}/rest/v1"
        self.writer_id = writer_id
        self.history_every = history_every
        self._client = client or httpx.Client(timeout=timeout_s)

    # -- Transport protocol ------------------------------------------------

    def publish(self, out: TickOutput) -> None:
        ts = out.states[0]["timestamp"] if out.states else ""
        rows = [robot_row(s, out.extras[self._rid(s, out)]) for s in out.states]
        self._post("robots", rows, on_conflict="id", merge=True)
        if out.events:
            alerts = [alert_row(e, ts) for e in out.events]
            self._post("alerts", alerts, on_conflict="id", merge=False)
        meta = fleet_meta_row(self.writer_id, out.sim_time_s, out.throughput_per_h, ts)
        self._post("fleet_meta", [meta], on_conflict="id", merge=True)
        # v0.3: downsampled history for replay/analytics (every Nth tick).
        if self.history_every and out.tick % self.history_every == 0:
            samples = [telemetry_row(s, out.extras[self._rid(s, out)], ts)
                       for s in out.states]
            self._insert("robot_telemetry", samples)

    def _insert(self, table: str, rows: list) -> None:
        """Append-only insert (history tables); failures logged, not fatal."""
        try:
            resp = self._client.post(
                f"{self.rest}/{table}", json=rows,
                headers={**self._headers(), "Prefer": "return=minimal"},
            )
            if resp.status_code >= 400:
                log.warning("supabase %s INSERT -> %s: %s",
                            table, resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("supabase %s INSERT failed: %s", table, exc)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def poll_commands(self, apply_fn) -> int:
        """v0.2 human-in-the-loop gate, executor side.

        Fetch ``approved`` commands, apply each via ``apply_fn(robot_id, cmd)
        -> (ok, detail)``, and PATCH the row to ``executed`` / ``failed``
        with the detail in ``note``. Returns how many were processed.
        Network errors are logged and swallowed — the sim never dies
        because the gate is unreachable.
        """
        try:
            resp = self._client.get(
                f"{self.rest}/commands",
                params={"status": "eq.approved", "order": "created_at.asc",
                        "limit": "20", "select": "id,robot_id,cmd"},
                headers=self._headers(),
            )
            resp.raise_for_status()
            rows = resp.json() or []
        except Exception as exc:  # noqa: BLE001 - availability over purity
            log.warning("command poll failed: %s", exc)
            return 0
        done = 0
        for row in rows:
            ok, detail = apply_fn(row.get("robot_id", ""), row.get("cmd", ""))
            body = {"status": "executed" if ok else "failed", "note": detail,
                    "executed_at": datetime.now(timezone.utc).isoformat()}
            try:
                self._client.patch(
                    f"{self.rest}/commands",
                    params={"id": f"eq.{row['id']}"},
                    json=body,
                    headers=self._headers(),
                ).raise_for_status()
            except Exception as exc:  # noqa: BLE001
                log.warning("command ack failed for %s: %s", row.get("id"), exc)
            done += 1
            log.info("command %s %s -> %s: %s", row.get("cmd"),
                     row.get("robot_id"), body["status"], detail)
        return done

    def close(self) -> None:
        self._client.close()

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _rid(state: dict[str, Any], out: TickOutput) -> str:
        """Recover the fleet robot id for a state's extras entry.

        The VDA serial is sanitized (AMR_07); extras are keyed by the raw
        fleet id (AMR-07). Serial with '_' -> '-' restores the raw id.
        """
        raw = state["serialNumber"].replace("_", "-")
        return raw if raw in out.extras else state["serialNumber"]

    def _post(self, table: str, rows: list[dict[str, Any]],
              on_conflict: str, merge: bool) -> None:
        """Bulk upsert (merge=True) or insert-if-absent (merge=False)."""
        resolution = "merge-duplicates" if merge else "ignore-duplicates"
        try:
            resp = self._client.post(
                f"{self.rest}/{table}",
                params={"on_conflict": on_conflict},
                json=rows,
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "Prefer": f"resolution={resolution}, return=minimal",
                },
            )
            if resp.status_code >= 400:
                log.warning("supabase %s POST -> %s: %s",
                            table, resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            # A dropped tick is fine for telemetry; next tick overwrites.
            log.warning("supabase %s POST failed: %s", table, exc)
