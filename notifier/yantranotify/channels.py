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

import hashlib
import hmac
import json
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

    Optional signing: when ``YANTRA_WEBHOOK_SECRET`` is set (env, or the
    ``secret`` arg), every POST carries an
    ``X-Yantra-Signature: sha256=<hex>`` header — an HMAC-SHA256 of the
    exact request body, keyed by the secret — so receivers can verify
    authenticity. Unset: no header, unchanged behaviour.

    ``settings`` (optional) is a live :class:`~yantranotify.settings_sync.SettingsSync`
    sitting between "explicit constructor arg" and "raw env" in the
    precedence for ``url``/``secret`` — a table override picked up by the
    next poll takes effect on the very next ``send()``, no channel
    recreation needed. ``url``/``secret`` are properties for exactly this
    reason: they read live state on every access instead of being
    computed once in ``__init__``.
    """

    name = "webhook"

    SIGNATURE_HEADER = "X-Yantra-Signature"

    def __init__(
        self,
        url: str | None = None,
        client: httpx.Client | None = None,
        dry_run: bool = False,
        timeout_s: float = WEBHOOK_TIMEOUT_S,
        fmt: str | None = None,
        secret: str | None = None,
        settings=None,
    ) -> None:
        self._url_arg = url
        self._secret_arg = secret
        self.settings = settings
        self.dry_run = dry_run
        self.fmt = (fmt or os.environ.get("WEBHOOK_FORMAT")
                    or DEFAULT_FORMAT).lower()
        self._client = client or httpx.Client(timeout=timeout_s)

    @property
    def url(self) -> str | None:
        if self._url_arg:
            return self._url_arg
        if self.settings is not None:
            return self.settings.get("WEBHOOK_URL") or None
        return os.environ.get("WEBHOOK_URL") or None

    @property
    def secret(self) -> str | None:
        if self._secret_arg:
            return self._secret_arg
        if self.settings is not None:
            return self.settings.get("YANTRA_WEBHOOK_SECRET") or None
        return os.environ.get("YANTRA_WEBHOOK_SECRET") or None

    def _post(self, payload: dict) -> bool:
        if self.dry_run or not self.url:
            reason = "dry-run" if self.dry_run else "WEBHOOK_URL unset"
            print(f"[webhook {reason}] would POST {payload!r}")
            return True
        # Serialize once so the signature covers the exact bytes on the wire.
        body = json.dumps(payload, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        headers = {"content-type": "application/json"}
        if self.secret:
            digest = hmac.new(self.secret.encode("utf-8"), body,
                              hashlib.sha256).hexdigest()
            headers[self.SIGNATURE_HEADER] = f"sha256={digest}"
        try:
            resp = self._client.post(self.url, content=body, headers=headers)
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

    ``settings`` (optional) is a live :class:`~yantranotify.settings_sync.SettingsSync`
    sitting between "explicit constructor arg" and "raw env" for each of
    the 4 credentials — a table override takes effect on the very next
    ``send()``, no channel recreation needed. All four are properties for
    exactly this reason.
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
        settings=None,
    ) -> None:
        self._sid_arg = sid
        self._token_arg = token
        self._from_arg = from_
        self._to_arg = to
        self.settings = settings
        self.dry_run = dry_run
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)

    def _get(self, arg: str | None, env_key: str) -> str | None:
        if arg:
            return arg
        if self.settings is not None:
            return self.settings.get(env_key) or None
        return os.environ.get(env_key) or None

    @property
    def sid(self) -> str | None:
        return self._get(self._sid_arg, "TWILIO_SID")

    @property
    def token(self) -> str | None:
        return self._get(self._token_arg, "TWILIO_TOKEN")

    @property
    def from_(self) -> str | None:
        return self._wa(self._get(self._from_arg, "TWILIO_FROM"))

    @property
    def to(self) -> str | None:
        return self._wa(self._get(self._to_arg, "TWILIO_TO"))

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
