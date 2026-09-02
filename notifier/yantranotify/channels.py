"""Dispatch channels. Each channel takes a rendered message string.

* :class:`ConsoleChannel` — log lines (always safe).
* :class:`WebhookChannel` — POSTs Slack-compatible ``{"text": ...}`` JSON
  to ``WEBHOOK_URL``; Slack, Discord (``/slack`` endpoint) and Teams
  (via an adapter/workflow) all accept that shape.
* :class:`WhatsAppChannel` — Twilio Messages REST endpoint via raw
  ``httpx`` (no Twilio SDK). When credentials are missing, or in
  ``--dry-run``, it prints the payload instead — credentials are never
  hard-required.

Channels never raise out of :meth:`send`: a broken webhook must not take
down the poll loop. Failures are logged and reported via the boolean
return value.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol, Sequence

import httpx

from .formats import (DEFAULT_FORMAT, digest_payload_for, payload_for,
                      render_digest_grouped)
from .source import Event

log = logging.getLogger("yantranotify")

TWILIO_API = "https://api.twilio.com"
WEBHOOK_TIMEOUT_S = 5.0


class Channel(Protocol):
    name: str

    def send(self, text: str) -> bool:  # pragma: no cover - protocol
        ...


class ConsoleChannel:
    """Emit each notification as a log line (INFO)."""

    name = "console"

    def send(self, text: str) -> bool:
        log.info("NOTIFY %s", text)
        return True

    def send_event(self, event: Event) -> bool:
        return self.send(event.render())

    def send_digest(self, events: Sequence[Event]) -> bool:
        return self.send(render_digest_grouped(events))


class WebhookChannel:
    """POST notifications to a webhook, in one of three payload formats.

    * ``json`` (default) — ``{"text": <message>}``, the original
      Slack-compatible generic contract.
    * ``slack`` — Slack Block Kit (via :func:`~yantranotify.formats.payload_for`).
    * ``discord`` — Discord embeds.

    Format precedence: ``fmt`` arg > ``WEBHOOK_FORMAT`` env > ``json``.
    """

    name = "webhook"

    def __init__(
        self,
        url: str | None = None,
        client: httpx.Client | None = None,
        dry_run: bool = False,
        timeout_s: float = WEBHOOK_TIMEOUT_S,
        fmt: str | None = None,
    ) -> None:
        self.url = url or os.environ.get("WEBHOOK_URL") or None
        self.dry_run = dry_run
        self.fmt = (fmt or os.environ.get("WEBHOOK_FORMAT")
                    or DEFAULT_FORMAT).lower()
        self._client = client or httpx.Client(timeout=timeout_s)

    def _post(self, payload: dict) -> bool:
        if self.dry_run or not self.url:
            reason = "dry-run" if self.dry_run else "WEBHOOK_URL unset"
            print(f"[webhook {reason}] would POST {payload!r}")
            return True
        try:
            resp = self._client.post(self.url, json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("webhook send failed: %s", exc)
            return False

    def send(self, text: str) -> bool:
        return self._post({"text": text})

    def send_event(self, event: Event) -> bool:
        return self._post(payload_for(event, self.fmt))

    def send_digest(self, events: Sequence[Event]) -> bool:
        return self._post(digest_payload_for(events, self.fmt))


class WhatsAppChannel:
    """WhatsApp via the Twilio Messages REST API (no SDK).

    ``POST /2010-04-01/Accounts/{sid}/Messages.json`` with HTTP basic
    auth ``(sid, token)`` and form fields ``From``/``To``/``Body``.
    ``From``/``To`` get a ``whatsapp:`` prefix if not already present.
    Without ``TWILIO_SID``/``TWILIO_TOKEN``/``TWILIO_FROM``/``TWILIO_TO``
    the channel degrades to printing the payload (dry-run).
    """

    name = "whatsapp"

    def __init__(
        self,
        sid: str | None = None,
        token: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        client: httpx.Client | None = None,
        dry_run: bool = False,
        api_base: str = TWILIO_API,
        timeout_s: float = 10.0,
    ) -> None:
        self.sid = sid or os.environ.get("TWILIO_SID") or None
        self.token = token or os.environ.get("TWILIO_TOKEN") or None
        self.from_ = self._wa(from_ or os.environ.get("TWILIO_FROM"))
        self.to = self._wa(to or os.environ.get("TWILIO_TO"))
        self.dry_run = dry_run
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)

    @staticmethod
    def _wa(number: str | None) -> str | None:
        if not number:
            return None
        return number if number.startswith("whatsapp:") else f"whatsapp:{number}"

    @property
    def configured(self) -> bool:
        return all((self.sid, self.token, self.from_, self.to))

    def send(self, text: str) -> bool:
        data = {"From": self.from_ or "whatsapp:<unset>",
                "To": self.to or "whatsapp:<unset>",
                "Body": text}
        if self.dry_run or not self.configured:
            reason = "dry-run" if self.dry_run else "creds unset"
            print(f"[whatsapp {reason}] would POST Messages.json {data!r}")
            return True
        url = f"{self.api_base}/2010-04-01/Accounts/{self.sid}/Messages.json"
        try:
            resp = self._client.post(url, data=data, auth=(self.sid, self.token))
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("whatsapp send failed: %s", exc)
            return False
