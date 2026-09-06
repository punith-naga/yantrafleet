"""Dedup: the same alert/incident is never announced twice."""
from __future__ import annotations

import json

import httpx

from yantranotify.notifier import Notifier
from yantranotify.source import AlertSource

from conftest import CaptureChannel, FakeRest, URL, alert_row, incident_row


def test_second_poll_is_silent(rest: FakeRest):
    rest.alerts = [alert_row(1), alert_row(2, sev="serious")]
    rest.incidents = [incident_row(7)]
    source = AlertSource(url=URL, key="k", client=rest.client())
    cap = CaptureChannel()
    notifier = Notifier([cap])

    first = notifier.dispatch(source.fetch_events())
    assert len(first) == 3
    assert len(cap.sent) == 3

    second = notifier.dispatch(source.fetch_events())
    assert second == []
    assert len(cap.sent) == 3  # nothing new -> nothing sent


def test_new_event_among_old_is_the_only_one_sent(rest: FakeRest):
    rest.alerts = [alert_row(1)]
    source = AlertSource(url=URL, key="k", client=rest.client())
    cap = CaptureChannel()
    notifier = Notifier([cap])
    notifier.dispatch(source.fetch_events())

    rest.alerts = [alert_row(1), alert_row(2)]
    new = notifier.dispatch(source.fetch_events())
    assert [e.id for e in new] == ["A-002"]
    assert len(cap.sent) == 2
    assert "A-002" in cap.sent[-1]


def test_alert_and_incident_with_same_id_are_distinct():
    from yantranotify.source import Event
    a = Event("alert", "X-1", "crit", "m")
    i = Event("incident", "X-1", "crit", "m")
    cap = CaptureChannel()
    notifier = Notifier([cap])
    assert len(notifier.dispatch([a, i])) == 2


def test_state_file_survives_restart(rest: FakeRest, tmp_path):
    rest.alerts = [alert_row(1)]
    state = tmp_path / "seen.json"
    source = AlertSource(url=URL, key="k", client=rest.client())

    cap1 = CaptureChannel()
    Notifier([cap1], state_path=state).dispatch(source.fetch_events())
    assert len(cap1.sent) == 1
    assert "alert:A-001" in json.loads(state.read_text())["seen"]

    # fresh process: same state file -> already-seen alert stays silent
    cap2 = CaptureChannel()
    notifier2 = Notifier([cap2], state_path=state)
    assert notifier2.dispatch(source.fetch_events()) == []
    assert cap2.sent == []


def test_corrupt_state_file_is_tolerated(tmp_path):
    state = tmp_path / "seen.json"
    state.write_text("{not json")
    notifier = Notifier([CaptureChannel()], state_path=state)
    assert notifier.seen == set()


def test_source_query_filters(rest: FakeRest, monkeypatch):
    """The poll asks PostgREST only for notifiable rows — of this site."""
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    source = AlertSource(url=URL, key="k", client=rest.client())
    source.fetch_events()
    alert_req = next(r for r in rest.requests if r.url.path.endswith("/alerts"))
    inc_req = next(r for r in rest.requests if r.url.path.endswith("/incidents"))
    assert alert_req.url.params["ack"] == "eq.false"
    assert alert_req.url.params["sev"] == "in.(crit,serious)"
    assert inc_req.url.params["state"] == "eq.Open"
    # v0.5.x multi-site: both queries pin site_id (default site).
    assert alert_req.url.params["site_id"] == "eq.BLR-DC1"
    assert inc_req.url.params["site_id"] == "eq.BLR-DC1"
    assert alert_req.headers["apikey"] == "k"
    assert alert_req.headers["authorization"] == "Bearer k"


def test_source_site_filter_from_env(rest: FakeRest, monkeypatch):
    monkeypatch.setenv("YANTRA_SITE_ID", "PNQ-DC2")
    source = AlertSource(url=URL, key="k", client=rest.client())
    source.fetch_events()
    for req in rest.requests:
        assert req.url.params["site_id"] == "eq.PNQ-DC2"


def test_source_all_sites_drops_site_filter(rest: FakeRest):
    source = AlertSource(url=URL, key="k", client=rest.client(),
                         all_sites=True)
    source.fetch_events()
    for req in rest.requests:
        assert "site_id" not in req.url.params


def test_source_site_filter_updates_live_without_recreating_source(
    rest: FakeRest, monkeypatch,
):
    """v0.18.1: AlertSource.site is resolved fresh on every fetch_events()
    call (a property, not a value cached in __init__), so a live
    yantracore.site.start_site_sync() override reaches an ALREADY-
    CONSTRUCTED source's next poll — the same object, no restart."""
    from yantracore.site import start_site_sync, stop_site_sync

    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    source = AlertSource(url=URL, key="k", client=rest.client())
    source.fetch_events()
    assert rest.requests[-1].url.params["site_id"] == "eq.BLR-DC1"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"key": "YANTRA_SITE_ID", "value": "PNQ-WH7"}])

    try:
        start_site_sync(URL, "key",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
        source.fetch_events()
        assert rest.requests[-1].url.params["site_id"] == "eq.PNQ-WH7"
    finally:
        stop_site_sync()
