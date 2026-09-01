"""WhatsAppChannel: Twilio REST request shape + credential-free dry-run."""
from __future__ import annotations

import base64
from urllib.parse import parse_qs

import httpx

from yantranotify.channels import WhatsAppChannel

from conftest import FakeRest

CREDS = dict(sid="AC123", token="tok", from_="+14155238886", to="+919999999999")


def test_request_shape_when_configured(rest: FakeRest):
    ch = WhatsAppChannel(client=rest.client(), **CREDS)
    assert ch.configured
    assert ch.send("robot down") is True
    assert len(rest.twilio_posts) == 1
    req = rest.twilio_posts[0]
    assert req.method == "POST"
    assert req.url.host == "api.twilio.com"
    assert req.url.path == "/2010-04-01/Accounts/AC123/Messages.json"
    # HTTP basic auth with (sid, token)
    expect = base64.b64encode(b"AC123:tok").decode()
    assert req.headers["authorization"] == f"Basic {expect}"
    # form-encoded body with whatsapp: prefixes applied
    form = parse_qs(req.content.decode())
    assert form["From"] == ["whatsapp:+14155238886"]
    assert form["To"] == ["whatsapp:+919999999999"]
    assert form["Body"] == ["robot down"]


def test_existing_whatsapp_prefix_not_doubled(rest: FakeRest):
    ch = WhatsAppChannel(client=rest.client(), sid="AC1", token="t",
                         from_="whatsapp:+1", to="whatsapp:+2")
    assert ch.from_ == "whatsapp:+1" and ch.to == "whatsapp:+2"


def test_env_credentials(rest: FakeRest, monkeypatch):
    monkeypatch.setenv("TWILIO_SID", "AC9")
    monkeypatch.setenv("TWILIO_TOKEN", "sec")
    monkeypatch.setenv("TWILIO_FROM", "+1000")
    monkeypatch.setenv("TWILIO_TO", "+2000")
    ch = WhatsAppChannel(client=rest.client())
    ch.send("hi")
    assert rest.twilio_posts[0].url.path == "/2010-04-01/Accounts/AC9/Messages.json"


def test_unset_creds_dry_runs_instead_of_failing(rest: FakeRest, monkeypatch, capsys):
    for var in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_TO"):
        monkeypatch.delenv(var, raising=False)
    ch = WhatsAppChannel(client=rest.client())
    assert not ch.configured
    assert ch.send("hi") is True  # never hard-requires creds
    assert rest.twilio_posts == []
    out = capsys.readouterr().out
    assert "creds unset" in out and "'Body': 'hi'" in out


def test_dry_run_flag_prints_payload_even_with_creds(rest: FakeRest, capsys):
    ch = WhatsAppChannel(client=rest.client(), dry_run=True, **CREDS)
    assert ch.send("hi") is True
    assert rest.twilio_posts == []
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "'From': 'whatsapp:+14155238886'" in out
    assert "'To': 'whatsapp:+919999999999'" in out
    assert "'Body': 'hi'" in out


def test_twilio_http_error_is_swallowed():
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    ch = WhatsAppChannel(client=client, **CREDS)
    assert ch.send("hi") is False
