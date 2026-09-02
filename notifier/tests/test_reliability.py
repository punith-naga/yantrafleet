"""ReliableChannel: retry/backoff, no-loss queueing, circuit breaker.

All offline and instant: a fake clock records sleeps instead of sleeping.
"""
from __future__ import annotations

import logging

import httpx

from yantranotify.channels import WebhookChannel
from yantranotify.notifier import Notifier
from yantranotify.reliability import (BREAKER_PAUSE_S, BREAKER_THRESHOLD,
                                      RETRY_DELAYS, ReliableChannel)
from yantranotify.source import AlertSource, Event

from conftest import FakeRest, URL, alert_row


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FlakyChannel:
    """Fails the first ``fail_first`` sends, then succeeds."""

    name = "flaky"

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.attempts = 0
        self.delivered: list[str] = []

    def send(self, text: str) -> bool:
        self.attempts += 1
        if self.attempts <= self.fail_first:
            return False
        self.delivered.append(text)
        return True


def test_defaults_match_spec():
    assert RETRY_DELAYS == (1.0, 4.0)  # 3 tries total
    assert BREAKER_THRESHOLD == 5
    assert BREAKER_PAUSE_S == 300.0


def test_retry_succeeds_on_third_attempt_with_backoff():
    inner, clock = FlakyChannel(fail_first=2), FakeClock()
    ch = ReliableChannel(inner, clock=clock)
    assert ch.send("hello") is True
    assert inner.attempts == 3
    assert clock.sleeps == [1.0, 4.0]  # exponential backoff between tries
    assert ch.pending == type(ch.pending)()  # nothing queued


def test_final_failure_queues_item_and_next_send_flushes_in_order():
    inner, clock = FlakyChannel(fail_first=3), FakeClock()
    ch = ReliableChannel(inner, clock=clock)
    assert ch.send("first") is False       # 3 attempts, all fail -> queued
    assert inner.attempts == 3
    assert len(ch.pending) == 1
    # next send: pending flushed first (no loss, order preserved)
    assert ch.send("second") is True
    assert inner.delivered == ["first", "second"]
    assert not ch.pending


def test_flush_stops_at_first_failure_and_keeps_rest_queued():
    inner, clock = FlakyChannel(fail_first=100), FakeClock()
    ch = ReliableChannel(inner, clock=clock)
    ch.send("a")
    ch.send("b")  # pending flush fails -> b queued without extra attempts
    assert [item[1] for item in ch.pending] == ["a", "b"]
    inner.fail_first = 0
    assert ch.flush() == 2
    assert inner.delivered == ["a", "b"]


def test_circuit_opens_after_5_consecutive_failures_and_logs_once(caplog):
    inner, clock = FlakyChannel(fail_first=10_000), FakeClock()
    ch = ReliableChannel(inner, clock=clock)
    with caplog.at_level(logging.WARNING, logger="yantranotify"):
        for i in range(5):
            ch.send(f"m{i}")
        assert ch.circuit_open is True
        attempts_at_open = inner.attempts  # 5 items x 3 tries
        assert attempts_at_open == 15
        # while open: enqueue silently, no network attempts, no new logs
        ch.send("m5")
        ch.send("m6")
    assert inner.attempts == attempts_at_open
    assert len(ch.pending) == 7
    pause_logs = [r for r in caplog.records if "pausing channel" in r.message]
    assert len(pause_logs) == 1  # logged once per opening


def test_circuit_closes_after_pause_and_pending_drains():
    inner, clock = FlakyChannel(fail_first=10_000), FakeClock()
    ch = ReliableChannel(inner, clock=clock)
    for i in range(5):
        ch.send(f"m{i}")
    assert ch.circuit_open
    clock.advance(BREAKER_PAUSE_S - 1)
    assert ch.circuit_open           # 4:59 — still paused
    clock.advance(2)
    inner.fail_first = 0             # endpoint recovered
    assert ch.circuit_open is False  # pause elapsed
    assert ch.flush() == 5           # queue drains, nothing lost
    assert inner.delivered == [f"m{i}" for i in range(5)]
    assert ch.consecutive_failures == 0


def test_reopens_for_another_pause_if_still_failing():
    inner, clock = FlakyChannel(fail_first=10_000), FakeClock()
    ch = ReliableChannel(inner, clock=clock)
    for i in range(5):
        ch.send(f"m{i}")
    clock.advance(BREAKER_PAUSE_S + 1)
    attempts = inner.attempts
    ch.flush()                        # half-open probe fails again
    assert inner.attempts == attempts + 3
    assert ch.circuit_open is True    # re-opened for another pause


def test_inner_exception_counts_as_failure_not_crash():
    class Boom:
        name = "boom"

        def send(self, text: str) -> bool:
            raise RuntimeError("kaput")

    ch = ReliableChannel(Boom(), clock=FakeClock())
    assert ch.send("x") is False
    assert len(ch.pending) == 1


def test_send_event_falls_back_to_rendered_text_for_plain_channels():
    inner, clock = FlakyChannel(), FakeClock()
    ch = ReliableChannel(inner, clock=clock)
    e = Event("alert", "A-1", "crit", "boom")
    assert ch.send_event(e) is True
    assert inner.delivered == [e.render()]


def test_webhook_default_timeout_is_5s():
    ch = WebhookChannel(url=None)
    assert ch._client.timeout == httpx.Timeout(5.0)


def test_poll_loop_keeps_failed_events_queued_until_endpoint_recovers():
    """End-to-end: webhook down for one poll -> delivered on the next."""
    rest = FakeRest()
    rest.alerts = [alert_row(1)]
    broken = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hooks.example.test":
            if broken["on"]:
                return httpx.Response(500)
            rest.webhook_posts.append(request)
            return httpx.Response(200)
        return rest.handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = AlertSource(url=URL, key="k", client=client)
    clock = FakeClock()
    webhook = ReliableChannel(
        WebhookChannel(url="https://hooks.example.test/x", client=client),
        clock=clock)
    notifier = Notifier([webhook])

    new = notifier.dispatch(source.fetch_events())
    assert len(new) == 1
    assert rest.webhook_posts == []          # endpoint down
    assert len(webhook.pending) == 1         # ... but nothing lost
    assert clock.sleeps == [1.0, 4.0]        # retried before giving up

    broken["on"] = False
    notifier.dispatch(source.fetch_events())  # next poll flushes the queue
    assert len(rest.webhook_posts) == 1
    assert not webhook.pending
