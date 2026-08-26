"""Shared fixtures. All tests are OFFLINE: HTTP is httpx.MockTransport,
MQTT is a recording fake — supabase.co / brokers are never contacted."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # sim/ on path

from yantrasim.sim import FleetSim  # noqa: E402

FIXED_NOW = datetime(2026, 8, 26, 10, 15, 32, 512000, tzinfo=timezone.utc)


@pytest.fixture()
def sim() -> FleetSim:
    return FleetSim(seed=7)


@pytest.fixture()
def one_tick(sim: FleetSim):
    return sim.tick(dt_s=10.0, now=FIXED_NOW)
