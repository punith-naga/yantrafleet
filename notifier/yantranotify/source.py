"""Read side: poll ``alerts`` and ``incidents`` via PostgREST.

Only rows worth waking a human for are fetched:

* alerts   — ``ack=eq.false`` and ``sev=in.(crit,serious)``
* incidents — ``state=eq.Open``

Both queries additionally filter ``site_id=eq.<site>`` (v0.5.x
multi-site groundwork) unless the source is built site-less
(``python -m yantranotify --all-sites``). The ``site_id`` column
defaults to the site value server-side (0005_sites.sql), so legacy rows
still match.

An injectable :class:`httpx.Client` keeps every test offline
(``httpx.MockTransport``), mirroring the other YantraFleet components.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from yantracore import site_id as _default_site_id

DEFAULT_URL = "https://flwyvhsmgrrqpmhcqlzd.supabase.co"
DEFAULT_KEY = "sb_publishable_7rqvPRggmPDRKNL8Jurcqg_Hf531puf"

NOTIFY_SEVERITIES = ("crit", "serious")


def resolve_config(url: str | None = None, key: str | None = None) -> tuple[str, str]:
    """Precedence: explicit arg > env var > embedded default."""
    return (
        (url or os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/"),
        key or os.environ.get("SUPABASE_KEY") or DEFAULT_KEY,
    )


@dataclass(frozen=True)
class Event:
    """One notifiable thing: an alert row or an incident row."""

    kind: str  # "alert" | "incident"
    id: str
    sev: str
    text: str  # rendered one-line human message (without severity tag)

    @property
    def dedup_key(self) -> str:
        return f"{self.kind}:{self.id}"

    def render(self) -> str:
        return f"[{self.sev.upper()}] {self.kind.upper()} {self.id}: {self.text}"


def _alert_event(row: dict[str, Any]) -> Event:
    bits = [str(row.get("msg") or "(no message)")]
    if row.get("src"):
        bits.append(f"src={row['src']}")
    if row.get("tlabel"):
        bits.append(f"at {row['tlabel']}")
    return Event("alert", str(row.get("id")), str(row.get("sev") or "crit"),
                 " · ".join(bits))


def _incident_event(row: dict[str, Any]) -> Event:
    bits = [str(row.get("title") or "(untitled incident)")]
    if row.get("impact"):
        bits.append(str(row["impact"]))
    return Event("incident", str(row.get("id")), str(row.get("sev") or "crit"),
                 " · ".join(bits))


class AlertSource:
    """Fetches notifiable alert/incident rows from PostgREST."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
        site: str | None = None,
        all_sites: bool = False,
    ) -> None:
        """``site`` pins the queries to one site (default: this
        process's ``yantracore.site_id()``); ``all_sites=True`` drops
        the site filter entirely (``--all-sites``)."""
        self.base_url, self.key = resolve_config(url, key)
        self.rest = f"{self.base_url}/rest/v1"
        self.site: str | None = None if all_sites else (site or _default_site_id())
        self._client = client or httpx.Client(timeout=timeout_s)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }

    def fetch_events(self) -> list[Event]:
        """One poll: unacked crit/serious alerts + Open incidents."""
        events: list[Event] = []
        sev_in = ",".join(NOTIFY_SEVERITIES)
        site_filter = {} if self.site is None else {"site_id": f"eq.{self.site}"}

        resp = self._client.get(
            f"{self.rest}/alerts",
            params={
                "ack": "eq.false",
                "sev": f"in.({sev_in})",
                "order": "created_at.asc",
                **site_filter,
            },
            headers=self._headers,
        )
        resp.raise_for_status()
        events.extend(_alert_event(r) for r in resp.json())

        resp = self._client.get(
            f"{self.rest}/incidents",
            params={"state": "eq.Open", "order": "created_at.asc",
                    **site_filter},
            headers=self._headers,
        )
        resp.raise_for_status()
        events.extend(_incident_event(r) for r in resp.json())
        return events

    def close(self) -> None:
        self._client.close()
