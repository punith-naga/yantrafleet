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
from typing import Protocol

import httpx

log = logging.getLogger("yantranotify")

TWILIO_API = "https://api.twilio.com"


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


class WebhookChannel:
    """POST ``{"text": <message>}`` to a Slack-compatible webhook."""

    name = "webhook"

    def __init__(
        self,
        url: str | None = None,
        client: httpx.Client | None = None,
        dry_run: bool = False,
        timeout_s: float = 10.0,
    ) -> None:
        self.url = url or os.environ.get("WEBHOOK_URL") or None
        self.dry_run = dry_run
        self._client = client or httpx.Client(timeout=timeout_s)

    def send(self, text: str) -> bool:
        payload = {"text": text}
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
