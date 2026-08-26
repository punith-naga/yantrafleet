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
from typing import Any

import httpx

from ..sim import TickOutput
from ..translate import alert_row, fleet_meta_row, robot_row

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
    ) -> None:
        self.base_url, self.key = resolve_config(url, key)
        self.rest = f"{self.base_url}/rest/v1"
        self.writer_id = writer_id
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
