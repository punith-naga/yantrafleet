"""WebhookChannel/WhatsAppChannel picking up a live SettingsSync value —
the behaviour the property-based refactor in channels.py exists to
guarantee: a table override takes effect on the very next send(), no
channel recreation needed.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs

from yantranotify.channels import WebhookChannel, WhatsAppChannel

from conftest import FakeRest


class FakeSettingsSync:
    """``.get()`` reflects a mutable backing dict, falling back to
    ``os.environ`` exactly like the real SettingsSync.get() contract —
    stands in for a real poll picking up a row change, without any
    network/threading."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})

    def get(self, key: str) -> str | None:
        return self.values.get(key) or os.environ.get(key) or None


# -- WebhookChannel -----------------------------------------------------------


def test_webhook_url_reflects_settings_immediately(rest: FakeRest) -> None:
    settings = FakeSettingsSync({"WEBHOOK_URL": "https://hooks.example.test/new"})
    ch = WebhookChannel(client=rest.client(), settings=settings)
    assert ch.url == "https://hooks.example.test/new"


def test_webhook_explicit_url_wins_over_settings(rest: FakeRest) -> None:
    settings = FakeSettingsSync({"WEBHOOK_URL": "https://hooks.example.test/from-panel"})
    ch = WebhookChannel(url="https://hooks.example.test/explicit",
                        client=rest.client(), settings=settings)
    assert ch.url == "https://hooks.example.test/explicit"


def test_webhook_secret_reflects_settings_and_signs(rest: FakeRest) -> None:
    settings = FakeSettingsSync({
        "WEBHOOK_URL": "https://hooks.example.test/x",
        "YANTRA_WEBHOOK_SECRET": "s3cret-from-panel",
    })
    ch = WebhookChannel(client=rest.client(), settings=settings)
    assert ch.secret == "s3cret-from-panel"
    assert ch.send("hello") is True
    req = rest.webhook_posts[0]
    assert ch.SIGNATURE_HEADER in req.headers


def test_webhook_picks_up_settings_change_with_no_recreation(rest: FakeRest) -> None:
    settings = FakeSettingsSync()  # nothing configured yet
    ch = WebhookChannel(client=rest.client(), settings=settings)
    assert ch.url is None

    # Admin sets WEBHOOK_URL via the panel — same channel instance, no
    # recreation. The next send() must pick it up.
    settings.values["WEBHOOK_URL"] = "https://hooks.example.test/live"
    assert ch.send("hello") is True
    assert len(rest.webhook_posts) == 1
    assert str(rest.webhook_posts[0].url) == "https://hooks.example.test/live"


def test_webhook_settings_present_but_key_unset_falls_through_to_env(
    rest: FakeRest, monkeypatch
) -> None:
    """settings.get() itself does the table-override-else-env fallback
    (see SettingsSync.get) — the channel just calls it."""
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/env")
    settings = FakeSettingsSync()  # no override for this key
    ch = WebhookChannel(client=rest.client(), settings=settings)
    assert ch.url == "https://hooks.example.test/env"


def test_webhook_no_settings_object_falls_back_to_raw_env(
    rest: FakeRest, monkeypatch
) -> None:
    """settings=None (the pre-existing behaviour every current test
    exercises): straight to os.environ, unchanged."""
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/env")
    ch = WebhookChannel(client=rest.client(), settings=None)
    assert ch.url == "https://hooks.example.test/env"


# -- WhatsAppChannel -----------------------------------------------------------


CREDS_FROM_PANEL = {
    "TWILIO_SID": "AC-panel", "TWILIO_TOKEN": "tok-panel",
    "TWILIO_FROM": "+14155238886", "TWILIO_TO": "+919999999999",
}


def test_whatsapp_properties_reflect_settings_immediately(rest: FakeRest) -> None:
    settings = FakeSettingsSync(CREDS_FROM_PANEL)
    ch = WhatsAppChannel(client=rest.client(), settings=settings)
    assert ch.sid == "AC-panel"
    assert ch.token == "tok-panel"
    # whatsapp: prefix normalization still applies to a settings-sourced number
    assert ch.from_ == "whatsapp:+14155238886"
    assert ch.to == "whatsapp:+919999999999"
    assert ch.configured is True


def test_whatsapp_explicit_args_win_over_settings(rest: FakeRest) -> None:
    settings = FakeSettingsSync(CREDS_FROM_PANEL)
    ch = WhatsAppChannel(sid="AC-explicit", client=rest.client(), settings=settings)
    assert ch.sid == "AC-explicit"
    assert ch.token == "tok-panel"  # not overridden -> falls through to settings


def test_whatsapp_picks_up_settings_change_with_no_recreation(rest: FakeRest) -> None:
    settings = FakeSettingsSync()  # nothing configured
    ch = WhatsAppChannel(client=rest.client(), settings=settings)
    assert ch.configured is False
    assert ch.send("robot down") is True
    assert rest.twilio_posts == []  # dry-run (creds unset)

    settings.values.update(CREDS_FROM_PANEL)
    assert ch.configured is True
    assert ch.send("robot down") is True
    assert len(rest.twilio_posts) == 1
    form = parse_qs(rest.twilio_posts[0].content.decode())
    assert form["From"] == ["whatsapp:+14155238886"]
    assert form["To"] == ["whatsapp:+919999999999"]


def test_whatsapp_settings_present_but_key_unset_falls_through_to_env(
    rest: FakeRest, monkeypatch
) -> None:
    monkeypatch.setenv("TWILIO_SID", "AC-env")
    monkeypatch.setenv("TWILIO_TOKEN", "tok-env")
    monkeypatch.setenv("TWILIO_FROM", "+1000")
    monkeypatch.setenv("TWILIO_TO", "+2000")
    settings = FakeSettingsSync()
    ch = WhatsAppChannel(client=rest.client(), settings=settings)
    assert ch.sid == "AC-env"
    assert ch.from_ == "whatsapp:+1000"


def test_whatsapp_no_settings_object_falls_back_to_raw_env(
    rest: FakeRest, monkeypatch
) -> None:
    monkeypatch.setenv("TWILIO_SID", "AC-env")
    ch = WhatsAppChannel(client=rest.client(), settings=None)
    assert ch.sid == "AC-env"
