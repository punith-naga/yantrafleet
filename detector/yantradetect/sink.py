"""Sinks turn engine :class:`~yantradetect.engine.Action`s into effects.

``PostgRESTSink`` writes to the ``incidents`` table via PostgREST
(Supabase). Opens are POSTed with ``on_conflict=id`` +
``Prefer: resolution=merge-duplicates`` so the deterministic ids make
retries idempotent; patches are ``PATCH ?id=eq.<id>``. An injectable
``httpx.Client`` keeps every test offline (``httpx.MockTransport``).

``DryRunSink`` prints each action instead — ``--dry-run``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

import httpx

from .engine import Action

log = logging.getLogger(__name__)

DEFAULT_URL = "https://flwyvhsmgrrqpmhcqlzd.supabase.co"
DEFAULT_KEY = "sb_publishable_7rqvPRggmPDRKNL8Jurcqg_Hf531puf"


def resolve_config(url: str | None = None, key: str | None = None) -> tuple[str, str]:
    """Precedence: explicit arg > env var > embedded default."""
    return (
        (url or os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/"),
        key or os.environ.get("SUPABASE_KEY") or DEFAULT_KEY,
    )


class PostgRESTSink:
    """Upsert/patch ``incidents`` rows; also polls ``robots`` for the CLI."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url, self.key = resolve_config(url, key)
        self.rest = f"{self.base_url}/rest/v1"
        self._client = client or httpx.Client(timeout=timeout_s)

    # -- Sink protocol ------------------------------------------------------

    def apply(self, actions: Iterable[Action]) -> int:
        """Apply actions in order; failures are logged, never fatal
        (next poll re-derives the state). Returns actions attempted."""
        n = 0
        for a in actions:
            n += 1
            try:
                if a.op == "open":
                    resp = self._client.post(
                        f"{self.rest}/incidents",
                        params={"on_conflict": "id"},
                        json=[a.row],
                        headers={**self._headers(),
                                 "Prefer": "resolution=merge-duplicates, return=minimal"},
                    )
                else:
                    resp = self._client.patch(
                        f"{self.rest}/incidents",
                        params={"id": f"eq.{a.incident_id}"},
                        json=a.row,
                        headers={**self._headers(), "Prefer": "return=minimal"},
                    )
                if resp.status_code >= 400:
                    log.warning("incidents %s %s -> %s: %s", a.op,
                                a.incident_id, resp.status_code, resp.text[:200])
            except httpx.HTTPError as exc:
                log.warning("incidents %s %s failed: %s", a.op, a.incident_id, exc)
        return n

    # -- reads used by the CLI ---------------------------------------------

    def fetch_robots(self) -> list[dict[str, Any]]:
        resp = self._client.get(
            f"{self.rest}/robots",
            params={"select": "id,status,fault_msg,updated_at",
                    "order": "id.asc"},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json() or []

    def fetch_open_incidents(self) -> list[dict[str, Any]]:
        """Open rows for engine restart seeding; [] on any failure."""
        try:
            resp = self._client.get(
                f"{self.rest}/incidents",
                params={"state": "eq.Open",
                        "select": "id,sev,src,dur,created_at"},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as exc:  # noqa: BLE001 - availability over purity
            log.warning("open-incident seed fetch failed: %s", exc)
            return []

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }


class DryRunSink:
    """Print actions instead of writing them (``--dry-run``)."""

    def __init__(self) -> None:
        self.applied: list[Action] = []

    def apply(self, actions: Iterable[Action]) -> int:
        n = 0
        for a in actions:
            n += 1
            self.applied.append(a)
            print(f"[dry-run] {a.op.upper():5s} {a.incident_id} {a.row}")
        return n

    def close(self) -> None:  # pragma: no cover - symmetry with PostgRESTSink
        pass
