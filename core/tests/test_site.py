"""Site identity (yantracore.site) — env-driven, defaulted, exported."""
import yantracore
from yantracore import DEFAULT_SITE_ID, SITE_ID, site_id


def test_default_site_id(monkeypatch):
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    assert site_id() == "BLR-DC1" == DEFAULT_SITE_ID


def test_env_override(monkeypatch):
    monkeypatch.setenv("YANTRA_SITE_ID", "PNQ-DC2")
    assert site_id() == "PNQ-DC2"


def test_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("YANTRA_SITE_ID", "   ")
    assert site_id() == DEFAULT_SITE_ID


def test_module_snapshot_and_exports():
    assert isinstance(SITE_ID, str) and SITE_ID
    for name in ("DEFAULT_SITE_ID", "SITE_ID", "site_id"):
        assert name in yantracore.__all__
