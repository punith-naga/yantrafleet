"""Dedup + digest dispatcher.

The :class:`Notifier` sits between an :class:`~yantranotify.source.AlertSource`
and a list of channels:

* **Dedup** — the same alert/incident is never announced twice. Seen
  keys live in memory and, when ``state_path`` is given, in a small JSON
  state file so restarts stay quiet too.
* **Digest** — when one poll yields more than :data:`DIGEST_THRESHOLD`
  *new* events, they are collapsed into a single summary message
  (alarm-fatigue discipline: one digest instead of a message storm).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Sequence

from .channels import Channel
from .source import Event

log = logging.getLogger("yantranotify")

DIGEST_THRESHOLD = 5  # >5 new events in one poll -> single digest message


def render_digest(events: Sequence[Event]) -> str:
    """One summary message for a batch of new events."""
    alerts = [e for e in events if e.kind == "alert"]
    incidents = [e for e in events if e.kind == "incident"]
    crit = sum(1 for e in events if e.sev == "crit")
    head = (
        f"Yantrika digest: {len(events)} new notifications "
        f"({len(alerts)} alerts, {len(incidents)} incidents; {crit} crit)"
    )
    lines = [head] + [f"- {e.render()}" for e in events]
    return "\n".join(lines)


class Notifier:
    def __init__(
        self,
        channels: Sequence[Channel],
        state_path: str | Path | None = None,
    ) -> None:
        self.channels = list(channels)
        self.state_path = Path(state_path) if state_path else None
        self.seen: set[str] = set()
        self._load_state()

    # -- state file ---------------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self.seen.update(str(k) for k in data.get("seen", []))
        except (ValueError, OSError) as exc:
            log.warning("could not load state file %s: %s", self.state_path, exc)

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"seen": sorted(self.seen)}))
        except OSError as exc:
            log.warning("could not save state file %s: %s", self.state_path, exc)

    # -- dispatch -----------------------------------------------------------

    def _each_channel(self, deliver) -> None:
        for ch in self.channels:
            try:
                deliver(ch)
            except Exception as exc:  # defence in depth; channels shouldn't raise
                log.warning("channel %s failed: %s", getattr(ch, "name", ch), exc)

    def _broadcast_event(self, event: Event) -> None:
        """Prefer a channel's structured ``send_event``; else plain text."""
        def deliver(ch):
            fn = getattr(ch, "send_event", None)
            fn(event) if fn else ch.send(event.render())
        self._each_channel(deliver)

    def _broadcast_digest(self, events: Sequence[Event]) -> None:
        """Prefer a channel's structured ``send_digest``; else plain text."""
        def deliver(ch):
            fn = getattr(ch, "send_digest", None)
            fn(events) if fn else ch.send(render_digest(events))
        self._each_channel(deliver)

    def flush_channels(self) -> None:
        """Give reliable channels a chance to retry queued items."""
        def deliver(ch):
            fn = getattr(ch, "flush", None)
            if fn:
                fn()
        self._each_channel(deliver)

    def dispatch(self, events: Iterable[Event]) -> list[Event]:
        """Filter already-seen events, notify the rest, return what was new.

        More than :data:`DIGEST_THRESHOLD` new events -> one digest
        message; otherwise one message per event. Channels wrapped in
        :class:`~yantranotify.reliability.ReliableChannel` keep failed
        items queued; every dispatch retries those first (no loss).
        """
        self.flush_channels()
        new = [e for e in events if e.dedup_key not in self.seen]
        if not new:
            return []
        self.seen.update(e.dedup_key for e in new)
        if len(new) > DIGEST_THRESHOLD:
            self._broadcast_digest(new)
        else:
            for e in new:
                self._broadcast_event(e)
        self._save_state()
        return new
