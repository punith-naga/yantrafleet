"""Delivery reliability: retry, no-loss queueing, per-channel breaker.

:class:`ReliableChannel` wraps any channel and adds:

* **Retry** — each item gets up to 3 attempts with exponential backoff
  (1s, then 4s) before being declared failed.
* **No loss** — an item that still fails after the retries is logged and
  kept in an in-memory pending queue; the next poll (or the next send)
  flushes the queue first, preserving order.
* **Circuit breaker** — after 5 *consecutive* final failures the channel
  is paused for 5 minutes (logged once per opening). While paused,
  items go straight to the pending queue without touching the network;
  after the pause the channel is retried and the queue drains.

Time is injectable (``clock`` needs ``monotonic()`` and ``sleep(s)``) so
tests run with a fake clock — no real sleeping, no real network.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Sequence

from .source import Event

log = logging.getLogger("yantranotify")

RETRY_DELAYS: tuple[float, ...] = (1.0, 4.0)  # 3 attempts: 0s, +1s, +4s
BREAKER_THRESHOLD = 5      # consecutive final failures before pausing
BREAKER_PAUSE_S = 300.0    # 5 minutes
MAX_PENDING = 500          # queue cap; oldest dropped beyond this


class SystemClock:
    """Real time. Tests inject a fake with the same two methods."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ReliableChannel:
    """Wrap ``inner`` with retry/backoff, a pending queue and a breaker.

    Exposes the same duck-typed surface the :class:`Notifier` speaks:
    ``send(text)``, ``send_event(event)``, ``send_digest(events)`` and
    ``flush()``; falls back to ``inner.send(rendered_text)`` when the
    inner channel has no structured methods.
    """

    def __init__(
        self,
        inner,
        clock=None,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        breaker_threshold: int = BREAKER_THRESHOLD,
        breaker_pause_s: float = BREAKER_PAUSE_S,
        max_pending: int = MAX_PENDING,
    ) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", inner.__class__.__name__)
        self.clock = clock or SystemClock()
        self.retry_delays = tuple(retry_delays)
        self.breaker_threshold = breaker_threshold
        self.breaker_pause_s = breaker_pause_s
        self.max_pending = max_pending
        self.pending: deque = deque()
        self.consecutive_failures = 0
        self._open_until: float | None = None

    # -- circuit breaker ----------------------------------------------------

    @property
    def circuit_open(self) -> bool:
        """True while the channel is paused (breaker tripped)."""
        if self._open_until is None:
            return False
        if self.clock.monotonic() >= self._open_until:
            self._open_until = None  # half-open: allow the next attempt
            log.info("channel %s: pause over, resuming delivery", self.name)
            return False
        return True

    def _record_failure(self) -> None:
        self.consecutive_failures += 1
        if (self.consecutive_failures >= self.breaker_threshold
                and self._open_until is None):
            self._open_until = self.clock.monotonic() + self.breaker_pause_s
            log.warning(
                "channel %s: %d consecutive failures — pausing channel "
                "for %.0fs (queued items are kept)",
                self.name, self.consecutive_failures, self.breaker_pause_s)

    # -- delivery -----------------------------------------------------------

    def _deliver(self, item: tuple) -> bool:
        kind, payload = item
        if kind == "event":
            fn = getattr(self.inner, "send_event", None)
            return fn(payload) if fn else self.inner.send(payload.render())
        if kind == "digest":
            fn = getattr(self.inner, "send_digest", None)
            if fn:
                return fn(list(payload))
            from .notifier import render_digest
            return self.inner.send(render_digest(list(payload)))
        return self.inner.send(payload)

    def _attempt(self, item: tuple) -> bool:
        """One delivery with retries; trips the breaker on final failure."""
        attempts = len(self.retry_delays) + 1
        for i in range(attempts):
            try:
                ok = bool(self._deliver(item))
            except Exception as exc:  # channels shouldn't raise, but be safe
                log.debug("channel %s: send raised: %s", self.name, exc)
                ok = False
            if ok:
                self.consecutive_failures = 0
                return True
            if i < len(self.retry_delays):
                delay = self.retry_delays[i]
                log.info("channel %s: send failed (attempt %d/%d), "
                         "retrying in %.0fs", self.name, i + 1, attempts,
                         delay)
                self.clock.sleep(delay)
        log.warning("channel %s: delivery failed after %d attempts — "
                    "keeping item queued for next poll", self.name, attempts)
        self._record_failure()
        return False

    def _enqueue(self, item: tuple) -> None:
        if len(self.pending) >= self.max_pending:
            self.pending.popleft()
            log.warning("channel %s: pending queue full (%d) — dropped "
                        "oldest item", self.name, self.max_pending)
        self.pending.append(item)

    def _send_item(self, item: tuple) -> bool:
        if self.circuit_open:
            self._enqueue(item)
            return False
        if self.pending:
            self.flush()
            if self.pending:  # still blocked: keep order, don't attempt
                self._enqueue(item)
                return False
        if self._attempt(item):
            return True
        self._enqueue(item)
        return False

    def flush(self) -> int:
        """Retry queued items in order; stop at the first failure.

        Returns how many queued items were delivered.
        """
        delivered = 0
        while self.pending and not self.circuit_open:
            if not self._attempt(self.pending[0]):
                break
            self.pending.popleft()
            delivered += 1
        return delivered

    # -- channel surface ----------------------------------------------------

    def send(self, text: str) -> bool:
        return self._send_item(("text", text))

    def send_event(self, event: Event) -> bool:
        return self._send_item(("event", event))

    def send_digest(self, events: Sequence[Event]) -> bool:
        return self._send_item(("digest", tuple(events)))
