"""Digest batching: >5 new events in one poll -> one summary message."""
from __future__ import annotations

from yantranotify.notifier import DIGEST_THRESHOLD, Notifier, render_digest
from yantranotify.source import AlertSource

from conftest import CaptureChannel, FakeRest, URL, alert_row, incident_row


def test_storm_collapses_to_single_digest(rest: FakeRest):
    rest.alerts = [alert_row(i) for i in range(1, 7)]  # 6 alerts
    rest.incidents = [incident_row(9)]                 # + 1 incident = 7 new
    source = AlertSource(url=URL, key="k", client=rest.client())
    cap = CaptureChannel()
    notifier = Notifier([cap])

    new = notifier.dispatch(source.fetch_events())
    assert len(new) == 7
    assert len(cap.sent) == 1  # ONE digest, not seven messages
    digest = cap.sent[0]
    assert "7 new notifications" in digest
    assert "6 alerts, 1 incidents" in digest
    for i in range(1, 7):
        assert f"A-{i:03d}" in digest
    assert "INC-0009" in digest


def test_exactly_threshold_sends_individual_messages():
    from yantranotify.source import Event
    events = [Event("alert", f"A-{i}", "crit", "m") for i in range(DIGEST_THRESHOLD)]
    cap = CaptureChannel()
    Notifier([cap]).dispatch(events)
    assert len(cap.sent) == DIGEST_THRESHOLD  # 5 -> no digest


def test_digest_counts_only_new_events(rest: FakeRest):
    """4 old + 3 new = 7 fetched, but only 3 new -> individual messages."""
    rest.alerts = [alert_row(i) for i in range(1, 5)]
    source = AlertSource(url=URL, key="k", client=rest.client())
    cap = CaptureChannel()
    notifier = Notifier([cap])
    notifier.dispatch(source.fetch_events())
    assert len(cap.sent) == 4

    rest.alerts = [alert_row(i) for i in range(1, 8)]
    new = notifier.dispatch(source.fetch_events())
    assert len(new) == 3
    assert len(cap.sent) == 4 + 3  # individual, not digest


def test_digest_dedups_for_next_poll(rest: FakeRest):
    rest.alerts = [alert_row(i) for i in range(1, 10)]
    source = AlertSource(url=URL, key="k", client=rest.client())
    cap = CaptureChannel()
    notifier = Notifier([cap])
    notifier.dispatch(source.fetch_events())
    assert len(cap.sent) == 1
    assert notifier.dispatch(source.fetch_events()) == []
    assert len(cap.sent) == 1


def test_render_digest_severity_summary():
    from yantranotify.source import Event
    events = [Event("alert", "A-1", "crit", "m"),
              Event("incident", "I-1", "serious", "m")]
    text = render_digest(events)
    assert text.splitlines()[0].endswith("(1 alerts, 1 incidents; 1 crit)")
    assert len(text.splitlines()) == 3
