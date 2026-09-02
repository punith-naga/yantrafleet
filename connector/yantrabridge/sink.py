"""Supabase (PostgREST) sink for translated rows.

Writes go through ``httpx``; the transport is injectable so unit tests run
fully offline with ``httpx.MockTransport``. Batch upserts follow the
PostgREST rules: ``on_conflict=<pk>`` + ``Prefer: resolution=merge-duplicates``
for robots / fleet_meta, and ``resolution=ignore-duplicates`` for alerts so
retries are idempotent (alert ids are deterministic per occurrence).

Because PostgREST requires every row in one batch to carry identical keys,
rows are grouped by key-set before sending.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

# Client-safe publishable anon key — fine to embed; override via env vars.
DEFAULT_SUPABASE_URL = "https://flwyvhsmgrrqpmhcqlzd.supabase.co"
DEFAULT_SUPABASE_KEY = "sb_publishable_7rqvPRggmPDRKNL8Jurcqg_Hf531puf"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _group_by_keyset(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split rows into batches whose dicts share the exact same key set."""
    groups: dict[frozenset[str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(frozenset(row.keys()), []).append(row)
    return list(groups.values())


class SupabaseSink:
    """Thin PostgREST writer for the YantraFleet tables.

    Parameters
    ----------
    url, key:
        Supabase project URL and anon key. Defaults come from the
        ``SUPABASE_URL`` / ``SUPABASE_KEY`` env vars, then the embedded
        project defaults.
    transport:
        Optional ``httpx.BaseTransport`` (e.g. ``httpx.MockTransport``) for
        offline testing.
    writer_id:
        Identifier written to ``fleet_meta.writer_id`` on heartbeat.
    """

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        writer_id: str = "yantrabridge",
        timeout: float = 10.0,
    ) -> None:
        self.url = (url or os.environ.get("SUPABASE_URL") or DEFAULT_SUPABASE_URL).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_KEY") or DEFAULT_SUPABASE_KEY
        self.writer_id = writer_id
        self._client = httpx.Client(
            base_url=f"{self.url}/rest/v1",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    # -- low-level ----------------------------------------------------------

    def _post(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        on_conflict: str,
        resolution: str,
    ) -> None:
        """One PostgREST bulk write; raises on HTTP error."""
        resp = self._client.post(
            f"/{table}",
            params={"on_conflict": on_conflict},
            json=rows,
            headers={"Prefer": f"resolution={resolution}, return=minimal"},
        )
        resp.raise_for_status()

    # -- table writers ------------------------------------------------------

    def upsert_robots(self, rows: Iterable[dict[str, Any]]) -> int:
        """Upsert robot rows (merge on id). Returns rows sent."""
        rows = list(rows)
        for batch in _group_by_keyset(rows):
            self._post("robots", batch, on_conflict="id", resolution="merge-duplicates")
        return len(rows)

    def insert_alerts(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert alert rows; duplicates (same id) are ignored -> retry-safe."""
        rows = list(rows)
        if rows:
            for batch in _group_by_keyset(rows):
                self._post("alerts", batch, on_conflict="id",
                           resolution="ignore-duplicates")
        return len(rows)

    def insert_telemetry(
        self, rows: Iterable[dict[str, Any]], *, chunk_size: int = 500
    ) -> int:
        """Bulk-insert ``robot_telemetry`` history rows. Returns rows sent.

        The table's primary key is a generated identity, so this is a plain
        insert (no on_conflict); rows are chunked to keep request bodies
        small on large imports.
        """
        rows = list(rows)
        for group in _group_by_keyset(rows):
            for i in range(0, len(group), chunk_size):
                resp = self._client.post(
                    "/robot_telemetry",
                    json=group[i:i + chunk_size],
                    headers={"Prefer": "return=minimal"},
                )
                resp.raise_for_status()
        return len(rows)

    def heartbeat(self) -> None:
        """Upsert fleet_meta row 1 with our writer id + timestamp.

        Only touches writer_id/updated_at, so sim_min/throughput written by
        other writers are preserved (PostgREST updates only supplied columns).
        """
        self._post(
            "fleet_meta",
            [{"id": 1, "writer_id": self.writer_id, "updated_at": _now_iso()}],
            on_conflict="id",
            resolution="merge-duplicates",
        )

    # -- convenience --------------------------------------------------------

    def push(
        self,
        robots: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        *,
        heartbeat: bool = True,
    ) -> dict[str, int]:
        """Write one translated batch. Returns counts for logging."""
        n_robots = self.upsert_robots(robots) if robots else 0
        n_alerts = self.insert_alerts(alerts) if alerts else 0
        if heartbeat:
            self.heartbeat()
        return {"robots": n_robots, "alerts": n_alerts}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SupabaseSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
