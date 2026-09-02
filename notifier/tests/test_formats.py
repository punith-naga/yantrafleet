"""Payload templates are pure functions: payload_for / digest_payload_for."""
from __future__ import annotations

import json

import pytest

from yantranotify.channels import WebhookChannel
from yantranotify.formats import (SEVERITY_COLOR, digest_payload_for,
                                  group_by_severity, payload_for,
                                  render_digest_grouped, sample_alert,
                                  sample_incident)
from yantranotify.source import Event

from conftest import FakeRest

HOOK = "https://hooks.example.test/services/T000/B000/XXX"


def _alert() -> Event:
    return Event("alert", "A-001", "crit", "battery critical · src=AMR-01",
                 msg="battery critical", src="AMR-01", site="BLR-DC1",
                 tlabel="14:07")


def _incident() -> Event:
    return Event("incident", "INC-0009", "serious",
                 "AMR-05 fault — overtemp · robot out of service",
                 msg="AMR-05 fault — overtemp", src="AMR-05", site="BLR-DC1",
                 tlabel="14:10", impact="robot out of service")


# -- generic json (default) -------------------------------------------------

def test_json_format_is_default_and_generic():
    e = _alert()
    assert payload_for(e) == {"text": e.render()}
    assert payload_for(e, "json") == {"text": e.render()}


def test_unknown_format_raises():
    with pytest.raises(ValueError, match="unknown payload format"):
        payload_for(_alert(), "msteams")


# -- slack block kit --------------------------------------------------------

def test_slack_alert_block_kit_shape():
    p = payload_for(_alert(), "slack")
    assert p["text"]  # fallback notification text for Slack toasts
    blocks = p["blocks"]
    assert blocks[0]["type"] == "header"
    header = blocks[0]["text"]
    assert header["type"] == "plain_text" and header["emoji"] is True
    assert ":red_circle:" in header["text"]       # severity emoji
    assert "CRIT" in header["text"] and "Alert" in header["text"]
    # fields section: robot / site / time
    fields_block = next(b for b in blocks
                        if b["type"] == "section" and "fields" in b)
    texts = [f["text"] for f in fields_block["fields"]]
    assert any(t.startswith("*Robot*") and "AMR-01" in t for t in texts)
    assert any(t.startswith("*Site*") and "BLR-DC1" in t for t in texts)
    assert any(t.startswith("*Time*") and "14:07" in t for t in texts)
    # context line, and strictly action-less
    assert blocks[-1]["type"] == "context"
    assert "unacknowledged" in blocks[-1]["elements"][0]["text"]
    assert all(b["type"] != "actions" for b in blocks)


def test_slack_incident_layout_is_distinct_from_alert():
    p = payload_for(_incident(), "slack")
    blocks = p["blocks"]
    assert "Incident" in blocks[0]["text"]["text"]
    assert ":large_orange_circle:" in blocks[0]["text"]["text"]
    fields_block = next(b for b in blocks
                        if b["type"] == "section" and "fields" in b)
    texts = [f["text"] for f in fields_block["fields"]]
    assert any(t.startswith("*Impact*") and "out of service" in t
               for t in texts)
    assert "state Open" in blocks[-1]["elements"][0]["text"]
    # alerts never carry an Impact field
    alert_fields = next(b for b in payload_for(_alert(), "slack")["blocks"]
                        if b["type"] == "section" and "fields" in b)
    assert not any(f["text"].startswith("*Impact*")
                   for f in alert_fields["fields"])


def test_slack_payload_survives_missing_optional_fields():
    bare = Event("alert", "A-9", "crit", "boom")  # no src/site/time
    p = payload_for(bare, "slack")
    assert p["blocks"][0]["type"] == "header"
    assert all("fields" not in b for b in p["blocks"])  # no empty section


# -- discord embeds ---------------------------------------------------------

def test_discord_embed_color_per_severity():
    crit = payload_for(_alert(), "discord")["embeds"][0]
    serious = payload_for(_incident(), "discord")["embeds"][0]
    assert crit["color"] == SEVERITY_COLOR["crit"]
    assert serious["color"] == SEVERITY_COLOR["serious"]
    assert crit["color"] != serious["color"]
    assert "CRIT" in crit["title"] and "A-001" in crit["title"]
    names = [f["name"] for f in serious["fields"]]
    assert "Impact" in names and "Robot" in names and "Site" in names


# -- digest polish ----------------------------------------------------------

def _storm() -> list[Event]:
    crits = [Event("alert", f"A-{i}", "crit", f"boom {i}") for i in range(5)]
    serious = [Event("incident", f"I-{i}", "serious", f"fault {i}")
               for i in range(2)]
    return crits + serious


def test_group_by_severity_worst_first():
    groups = group_by_severity(_storm())
    assert [sev for sev, _ in groups] == ["crit", "serious"]
    assert len(groups[0][1]) == 5 and len(groups[1][1]) == 2


def test_render_digest_grouped_counts_and_top3():
    text = render_digest_grouped(_storm())
    assert "7 new notifications" in text
    assert "(5 alerts, 2 incidents)" in text
    assert "CRIT (5):" in text and "SERIOUS (2):" in text
    # top 3 crit items listed, remainder summarised
    assert "A-0" in text and "A-2" in text
    assert "A-3" not in text and "A-4" not in text
    assert "and 2 more crit" in text


def test_digest_payload_slack_groups_with_top3_and_more():
    p = digest_payload_for(_storm(), "slack")
    blocks = p["blocks"]
    assert blocks[0]["type"] == "header"
    assert "7 new" in blocks[0]["text"]["text"]
    sections = [b["text"]["text"] for b in blocks
                if b["type"] == "section"]
    crit_section = next(s for s in sections if "CRIT — 5" in s)
    assert crit_section.count("•") == 3          # top 3 only
    assert "and 2 more crit" in crit_section
    assert any("SERIOUS — 2" in s for s in sections)


def test_digest_payload_discord_and_json():
    d = digest_payload_for(_storm(), "discord")["embeds"][0]
    assert d["color"] == SEVERITY_COLOR["crit"]  # worst severity colours it
    assert [f["name"] for f in d["fields"]] == ["CRIT (5)", "SERIOUS (2)"]
    assert d["fields"][0]["value"].count("\n") == 3  # 3 items + "more" line
    j = digest_payload_for(_storm(), "json")
    assert set(j) == {"text"} and "CRIT (5):" in j["text"]


# -- webhook channel posts the formatted payloads ---------------------------

def test_webhook_channel_posts_slack_payload(rest: FakeRest):
    ch = WebhookChannel(url=HOOK, client=rest.client(), fmt="slack")
    assert ch.send_event(_alert()) is True
    body = json.loads(rest.webhook_posts[0].content.decode())
    assert body == payload_for(_alert(), "slack")
    assert ch.send_digest(_storm()) is True
    body = json.loads(rest.webhook_posts[1].content.decode())
    assert body == digest_payload_for(_storm(), "slack")


def test_webhook_format_from_env(rest: FakeRest, monkeypatch):
    monkeypatch.setenv("WEBHOOK_FORMAT", "discord")
    ch = WebhookChannel(url=HOOK, client=rest.client())
    assert ch.fmt == "discord"
    ch.send_event(_alert())
    body = json.loads(rest.webhook_posts[0].content.decode())
    assert "embeds" in body


def test_webhook_default_format_keeps_generic_contract(rest: FakeRest,
                                                       monkeypatch):
    monkeypatch.delenv("WEBHOOK_FORMAT", raising=False)
    ch = WebhookChannel(url=HOOK, client=rest.client())
    assert ch.fmt == "json"
    ch.send_event(_alert())
    body = json.loads(rest.webhook_posts[0].content.decode())
    assert body == {"text": _alert().render()}


def test_sample_events_render_in_every_format():
    for fmt in ("json", "slack", "discord"):
        for ev in (sample_alert(), sample_incident()):
            assert isinstance(payload_for(ev, fmt), dict)
