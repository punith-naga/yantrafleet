"""Shared offline fixtures: mocked fleet data + a StaticTransport.

No network anywhere — supabase.co is unreachable from CI, and the tests
must stay hermetic. Golden expectations in eval/golden.json are written
against exactly this fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when pytest runs from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sarathi.transport import StaticTransport  # noqa: E402

FLEET_FIXTURE: dict[str, list[dict]] = {
    "robots": [
        {"id": "R-001", "vendor": "yantra", "status": "active", "battery": 78,
         "pos": [12.0, 4.5], "speed": 1.2, "task_kind": "pick", "health": 96,
         "motor_temp": 44, "tasks_done": 12, "fault_msg": None,
         "updated_at": "2026-08-26T10:14:00Z"},
        {"id": "R-002", "vendor": "yantra", "status": "charging", "battery": 21,
         "pos": [0.5, 1.0], "speed": 0.0, "task_kind": None, "health": 91,
         "motor_temp": 31, "tasks_done": 8, "fault_msg": None,
         "updated_at": "2026-08-26T10:14:02Z"},
        {"id": "R-003", "vendor": "acme", "status": "active", "battery": 55,
         "pos": [7.2, 9.9], "speed": 0.9, "task_kind": "haul", "health": 92,
         "motor_temp": 47, "tasks_done": 9, "fault_msg": None,
         "updated_at": "2026-08-26T10:14:05Z"},
        {"id": "R-004", "vendor": "acme", "status": "fault", "battery": 8,
         "pos": [3.3, 2.1], "speed": 0.0, "task_kind": None, "health": 40,
         "motor_temp": 88, "tasks_done": 4, "fault_msg": "motor overtemp",
         "updated_at": "2026-08-26T10:13:40Z"},
        {"id": "R-005", "vendor": "yantra", "status": "idle", "battery": 91,
         "pos": [15.0, 0.0], "speed": 0.0, "task_kind": None, "health": 99,
         "motor_temp": 29, "tasks_done": 17, "fault_msg": None,
         "updated_at": "2026-08-26T10:14:07Z"},
        {"id": "R-006", "vendor": "acme", "status": "charging", "battery": 14,
         "pos": [0.5, 2.0], "speed": 0.0, "task_kind": None, "health": 88,
         "motor_temp": 33, "tasks_done": 6, "fault_msg": None,
         "updated_at": "2026-08-26T10:14:01Z"},
    ],
    "alerts": [
        {"id": "AL-1", "sev": "critical", "msg": "R-004 motor overtemp",
         "src": "sim", "tlabel": "10:02", "ack": False,
         "created_at": "2026-08-26T10:02:00Z"},
        {"id": "AL-2", "sev": "warn", "msg": "R-006 battery low",
         "src": "sim", "tlabel": "10:05", "ack": False,
         "created_at": "2026-08-26T10:05:00Z"},
        {"id": "AL-3", "sev": "info", "msg": "mission M-2 completed",
         "src": "planner", "tlabel": "09:58", "ack": True,
         "created_at": "2026-08-26T09:58:00Z"},
    ],
    "incidents": [
        {"id": "INC-1", "sev": "high", "title": "Motor overtemp on R-004",
         "src": "sim", "tlabel": "10:03", "state": "open",
         "impact": "1 robot out of service", "rca": None, "fix": None,
         "dur": None},
        {"id": "INC-2", "sev": "low", "title": "Brief localization drift",
         "src": "sim", "tlabel": "09:40", "state": "resolved",
         "impact": "none", "rca": "map update", "fix": "relocalized",
         "dur": 6},
    ],
    "missions": [
        {"id": "M-1", "name": "Wave A", "robots": ["R-001", "R-003"],
         "state": "running", "prog": 62, "eta": "10:31"},
    ],
    "fleet_meta": [
        {"id": 1, "writer_id": "sim-1", "sim_min": 342, "throughput": 41.5,
         "updated_at": "2026-08-26T10:14:07Z"},
    ],
}


@pytest.fixture()
def transport() -> StaticTransport:
    return StaticTransport(FLEET_FIXTURE)


@pytest.fixture(autouse=True)
def no_llm_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests are offline: strip any LLM keys so tier selection is 'offline'."""
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "SARATHI_MODEL"):
        monkeypatch.delenv(var, raising=False)
