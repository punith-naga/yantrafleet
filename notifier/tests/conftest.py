"""Shared offline fixtures — nothing here touches the network."""
from __future__ import annotations

import httpx
import pytest

from yantracore.site import stop_site_sync

URL = "https://example.test"


@pytest.fixture(autouse=True)
def _clean_site_sync():
    """yantranotify.__main__.run() starts a live YANTRA_SITE_ID sync
    (yantracore.site.start_site_sync) on every call; without this, the
    first test to run it would leave its SiteSync active (and its fake
    client polling in a background thread) for every later test in this
    session — see core/yantracore/site.py's module docstring."""
    stop_site_sync()
    yield
    stop_site_sync()


def alert_row(i: int, sev: str = "crit", ack: bool = False) -> dict:
    return {"id": f"A-{i:03d}", "sev": sev, "msg": f"battery critical {i}",
            "src": f"AMR-{i:02d}", "tlabel": "14:0" + str(i % 10),
            "ack": ack, "site_id": "BLR-DC1",
            "created_at": f"2026-09-01T14:00:{i:02d}Z"}


def incident_row(i: int, state: str = "Open") -> dict:
    return {"id": f"INC-{i:04d}", "sev": "serious",
            "title": f"AMR-{i:02d} fault — overtemp", "src": f"AMR-{i:02d}",
            "tlabel": "14:10", "state": state,
            "impact": "robot out of service", "dur": 3,
            "site_id": "BLR-DC1",
            "created_at": f"2026-09-01T14:10:{i:02d}Z"}


class FakeRest:
    """Programmable PostgREST double behind an httpx.MockTransport."""

    def __init__(self) -> None:
        self.alerts: list[dict] = []
        self.incidents: list[dict] = []
        self.app_config: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.webhook_posts: list[httpx.Request] = []
        self.twilio_posts: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "hooks.example.test":
            self.webhook_posts.append(request)
            return httpx.Response(200, text="ok")
        if "api.twilio.com" in request.url.host:
            self.twilio_posts.append(request)
            return httpx.Response(201, json={"sid": "SM123"})
        if path.endswith("/alerts"):
            return httpx.Response(200, json=self.alerts)
        if path.endswith("/incidents"):
            return httpx.Response(200, json=self.incidents)
        if path.endswith("/app_config"):
            return httpx.Response(200, json=self.app_config)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture()
def rest() -> FakeRest:
    return FakeRest()


class CaptureChannel:
    """In-memory channel recording every message it is asked to send."""

    name = "capture"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True
