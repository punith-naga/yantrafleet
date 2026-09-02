"""WebhookChannel HMAC signing via YANTRA_WEBHOOK_SECRET.

Secret set: every POST carries ``X-Yantra-Signature: sha256=<hex>``, an
HMAC-SHA256 of the exact request body keyed by the secret. Secret unset:
no signature header — behaviour unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from yantranotify.channels import WebhookChannel
from yantranotify.source import _alert_event

from conftest import FakeRest, alert_row

HOOK = "https://hooks.example.test/services/T000/B000/XXX"
SECRET = "yantra-signing-secret"


def _expected_sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_present_and_correct_when_env_set(rest: FakeRest, monkeypatch):
    monkeypatch.setenv("YANTRA_WEBHOOK_SECRET", SECRET)
    ch = WebhookChannel(url=HOOK, client=rest.client())
    assert ch.send("[CRIT] ALERT A-001: battery critical") is True

    (req,) = rest.webhook_posts
    sig = req.headers.get("X-Yantra-Signature")
    assert sig is not None and sig.startswith("sha256=")
    # The signature verifies against the exact bytes that were posted.
    assert hmac.compare_digest(sig, _expected_sig(req.content))
    # And the body is still the normal JSON payload.
    assert json.loads(req.content.decode()) == {
        "text": "[CRIT] ALERT A-001: battery critical"
    }


def test_signature_covers_event_payloads_too(rest: FakeRest, monkeypatch):
    monkeypatch.setenv("YANTRA_WEBHOOK_SECRET", SECRET)
    ch = WebhookChannel(url=HOOK, client=rest.client())
    event = _alert_event(alert_row(1))
    assert ch.send_event(event) is True

    (req,) = rest.webhook_posts
    assert hmac.compare_digest(
        req.headers["X-Yantra-Signature"], _expected_sig(req.content)
    )


def test_wrong_secret_does_not_verify(rest: FakeRest, monkeypatch):
    monkeypatch.setenv("YANTRA_WEBHOOK_SECRET", SECRET)
    ch = WebhookChannel(url=HOOK, client=rest.client())
    ch.send("hello")
    (req,) = rest.webhook_posts
    assert req.headers["X-Yantra-Signature"] != _expected_sig(
        req.content, secret="some-other-secret"
    )


def test_no_signature_header_when_secret_unset(rest: FakeRest, monkeypatch):
    monkeypatch.delenv("YANTRA_WEBHOOK_SECRET", raising=False)
    ch = WebhookChannel(url=HOOK, client=rest.client())
    assert ch.send("hello") is True

    (req,) = rest.webhook_posts
    assert "X-Yantra-Signature" not in req.headers
    assert json.loads(req.content.decode()) == {"text": "hello"}


def test_explicit_secret_arg_wins_over_env(rest: FakeRest, monkeypatch):
    monkeypatch.setenv("YANTRA_WEBHOOK_SECRET", "env-secret")
    ch = WebhookChannel(url=HOOK, client=rest.client(), secret=SECRET)
    ch.send("hello")
    (req,) = rest.webhook_posts
    assert hmac.compare_digest(
        req.headers["X-Yantra-Signature"], _expected_sig(req.content)
    )
