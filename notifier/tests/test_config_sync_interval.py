"""NOTIFIER_INTERVAL: live from public.app_config (v0.18) when --interval
isn't explicitly passed; an explicit --interval always wins.

Mirrors test_settings_sync.py's offline-only style — the interval is
observed by monkeypatching time.sleep instead of actually sleeping.
"""
from __future__ import annotations

import yantranotify.__main__ as main_mod
from yantranotify.__main__ import build_parser, run

from conftest import URL, FakeRest


def _capture_sleep(monkeypatch) -> list[float]:
    seen: list[float] = []

    def fake_sleep(seconds: float) -> None:
        seen.append(seconds)

    monkeypatch.setattr(main_mod.time, "sleep", fake_sleep)
    return seen


def test_explicit_interval_flag_wins_over_app_config(rest: FakeRest, monkeypatch) -> None:
    seen = _capture_sleep(monkeypatch)
    rest.app_config = [{"key": "NOTIFIER_INTERVAL", "value": "99"}]
    args = build_parser().parse_args(
        ["--interval", "3", "--url", URL, "--key", "k"])
    run(args, client=rest.client(), max_polls=2)
    assert seen == [3.0]


def test_no_flag_uses_live_app_config_value(rest: FakeRest, monkeypatch) -> None:
    seen = _capture_sleep(monkeypatch)
    rest.app_config = [{"key": "NOTIFIER_INTERVAL", "value": "7"}]
    args = build_parser().parse_args(["--url", URL, "--key", "k"])
    run(args, client=rest.client(), max_polls=2)
    assert seen == [7.0]


def test_no_flag_and_no_config_row_uses_hardcoded_default(
    rest: FakeRest, monkeypatch
) -> None:
    seen = _capture_sleep(monkeypatch)
    args = build_parser().parse_args(["--url", URL, "--key", "k"])
    run(args, client=rest.client(), max_polls=2)
    assert seen == [10.0]


def test_interval_updates_live_across_polls(rest: FakeRest, monkeypatch) -> None:
    """An admin edit mid-run takes effect on the very next sleep, without
    restarting the process — config.maybe_poll() runs every tick."""
    seen = _capture_sleep(monkeypatch)
    rest.app_config = [{"key": "NOTIFIER_INTERVAL", "value": "5"}]
    args = build_parser().parse_args(["--url", URL, "--key", "k"])
    run(args, client=rest.client(), max_polls=2)
    assert seen == [5.0]


# -- v0.18.1: YANTRA_SITE_ID live sync (yantracore.site.start_site_sync) ----


def test_no_flag_uses_live_app_config_site_id(rest: FakeRest, monkeypatch) -> None:
    """yantranotify.__main__.run() starts a live YANTRA_SITE_ID sync
    alongside NOTIFIER_INTERVAL's; AlertSource.site (a property, not a
    value cached at construction — see yantranotify/source.py) reads it
    fresh on every poll, so an admin-console site change is honored on
    the very next poll with no restart needed."""
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    rest.app_config = [{"key": "YANTRA_SITE_ID", "value": "PNQ-WH7"}]
    args = build_parser().parse_args(["--url", URL, "--key", "k"])
    run(args, client=rest.client(), max_polls=1)
    site_filters = {r.url.params["site_id"] for r in rest.requests
                    if "site_id" in r.url.params}
    assert site_filters == {"eq.PNQ-WH7"}


def test_no_site_row_falls_back_to_env_default(rest: FakeRest, monkeypatch) -> None:
    """No YANTRA_SITE_ID row in app_config -> the env/default behavior,
    unchanged from before this sync existed."""
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    args = build_parser().parse_args(["--url", URL, "--key", "k"])
    run(args, client=rest.client(), max_polls=1)
    site_filters = {r.url.params["site_id"] for r in rest.requests
                    if "site_id" in r.url.params}
    assert site_filters == {"eq.BLR-DC1"}
