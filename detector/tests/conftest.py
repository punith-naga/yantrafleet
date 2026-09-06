"""Shared fixtures — nothing here touches the network."""
from __future__ import annotations

import pytest

from yantracore.site import stop_site_sync


@pytest.fixture(autouse=True)
def _clean_site_sync():
    """yantradetect.__main__.run() starts a live YANTRA_SITE_ID sync
    (yantracore.site.start_site_sync) on every call; without this, the
    first test to run it would leave its SiteSync active (and its fake
    client polling in a background thread) for every later test in this
    session — see core/yantracore/site.py's module docstring."""
    stop_site_sync()
    yield
    stop_site_sync()
