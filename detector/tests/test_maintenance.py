"""MaintenanceEngine trend math, dedup, sink payloads, and CLI — offline."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from yantradetect.__main__ import run
from yantradetect.maintenance import (
    MaintenanceEngine, MaintenanceSink, finding_id, linear_trend,
)

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def series(robot_id: str, *, minutes_step: int = 10, n: int = 12,
           temp0: float = 55.0, temp_per_hr: float = 0.0,
           batt0: float = 90.0, batt_per_min: float = 0.05,
           speed=lambda i: 1.2, status: str = "active") -> list[dict]:
    """Synthetic telemetry: linear temp ramp, linear battery drain."""
    rows = []
    for i in range(n):
        t = T0 + timedelta(minutes=i * minutes_step)
        hrs = i * minutes_step / 60.0
        rows.append({
            "robot_id": robot_id,
            "ts": t.isoformat(),
            "motor_temp": temp0 + temp_per_hr * hrs,
            "battery": batt0 - batt_per_min * i * minutes_step,
            "speed": speed(i),
            "status": status,
        })
    return rows


NOW = T0 + timedelta(hours=2)


# -- trend math -----------------------------------------------------------

def test_linear_trend_slope_and_r2():
    slope, r2 = linear_trend([(0, 50.0), (1, 53.0), (2, 56.0), (3, 59.0)])
    assert abs(slope - 3.0) < 1e-9
    assert abs(r2 - 1.0) < 1e-9


def test_linear_trend_degenerate():
    assert linear_trend([]) == (0.0, 0.0)
    assert linear_trend([(1, 5.0)]) == (0.0, 0.0)
    assert linear_trend([(1, 5.0), (1, 7.0)]) == (0.0, 0.0)  # zero x-variance


def test_rising_motor_temp_opens_drive_motor_finding():
    eng = MaintenanceEngine()
    acts = eng.observe(series("AMR-01", temp_per_hr=6.0), now=NOW)
    motor = [a for a in acts if a.row.get("component") == "drive motor"]
    assert len(motor) == 1
    a = motor[0]
    assert a.op == "open"
    assert a.row["robot_id"] == "AMR-01"
    assert a.row["state"] == "Open"
    assert "°C/hr" in a.row["finding"]
    assert 0.0 < a.row["confidence"] <= 1.0
    # 55 + 6*2 = 67 °C now; (85-67)/6 = 3 hrs -> clamped to 0.5 days min
    assert 0.5 <= a.row["rul_days"] <= 60.0
    assert a.row["action"]
    assert a.row["created_at"] == NOW.isoformat()
    assert a.incident_id == a.row["id"] == finding_id("AMR-01", "drive motor", NOW)


def test_stable_temp_emits_nothing():
    eng = MaintenanceEngine()
    acts = eng.observe(series("AMR-01", temp_per_hr=0.0), now=NOW)
    assert acts == []


def test_slow_rise_below_threshold_emits_nothing():
    eng = MaintenanceEngine()
    acts = eng.observe(series("AMR-01", temp_per_hr=0.5), now=NOW)
    assert [a for a in acts if a.row.get("component") == "drive motor"] == []


def test_too_few_samples_emits_nothing():
    eng = MaintenanceEngine()
    acts = eng.observe(series("AMR-01", temp_per_hr=10.0, n=4), now=NOW)
    assert acts == []


def test_rul_shrinks_near_critical_temp():
    eng = MaintenanceEngine()
    hot = eng.observe(series("A", temp0=60.0, temp_per_hr=1.6), now=NOW)
    cool = eng.observe(series("B", temp0=40.0, temp_per_hr=1.6), now=NOW)
    assert hot[0].row["rul_days"] < cool[0].row["rul_days"]


# -- battery vs fleet median ---------------------------------------------

def fleet_with_hog(hog_rate: float = 0.20) -> list[dict]:
    rows = []
    for rid in ("AMR-01", "AMR-02", "AMR-03"):
        rows += series(rid, batt_per_min=0.05)
    rows += series("AMR-09", batt_per_min=hog_rate)
    return rows


def test_battery_hog_vs_fleet_median_opens_finding():
    eng = MaintenanceEngine()
    acts = eng.observe(fleet_with_hog(), now=NOW)
    batt = [a for a in acts if a.row.get("component") == "battery"]
    assert len(batt) == 1
    a = batt[0]
    assert a.row["robot_id"] == "AMR-09"
    assert "fleet median" in a.row["finding"]
    assert 0.0 < a.row["confidence"] <= 1.0
    assert 3.0 <= a.row["rul_days"] <= 60.0


def test_uniform_drain_no_battery_finding():
    eng = MaintenanceEngine()
    rows = []
    for rid in ("AMR-01", "AMR-02", "AMR-03", "AMR-04"):
        rows += series(rid, batt_per_min=0.05)
    assert eng.observe(rows, now=NOW) == []


def test_fewer_than_three_robots_skips_battery_check():
    eng = MaintenanceEngine()
    rows = series("AMR-01", batt_per_min=0.05) + series("AMR-09", batt_per_min=0.5)
    acts = eng.observe(rows, now=NOW)
    assert [a for a in acts if a.row.get("component") == "battery"] == []


def test_idle_samples_dont_count_as_drain():
    eng = MaintenanceEngine()
    rows = fleet_with_hog(0.05)  # everyone equal...
    rows += series("AMR-77", batt_per_min=0.9, status="charging")  # not active
    acts = eng.observe(rows, now=NOW)
    assert [a for a in acts if a.row.get("component") == "battery"] == []


# -- speed degradation ----------------------------------------------------

def test_speed_degradation_opens_drivetrain_finding():
    eng = MaintenanceEngine()
    # 1.2 m/s for the first half, 0.8 m/s for the second: ~33% decline
    acts = eng.observe(
        series("AMR-05", speed=lambda i: 1.2 if i < 6 else 0.8), now=NOW)
    dt = [a for a in acts if a.row.get("component") == "drivetrain"]
    assert len(dt) == 1
    assert "down" in dt[0].row["finding"]
    assert 0.0 < dt[0].row["confidence"] <= 1.0
    assert dt[0].row["rul_days"] > 0


def test_steady_speed_no_drivetrain_finding():
    eng = MaintenanceEngine()
    acts = eng.observe(series("AMR-05", speed=lambda i: 1.2), now=NOW)
    assert acts == []


# -- dedup + clearing -----------------------------------------------------

def test_dedup_one_open_finding_per_robot_component():
    eng = MaintenanceEngine()
    rows = series("AMR-01", temp_per_hr=6.0)
    first = eng.observe(rows, now=NOW)
    assert len(first) == 1 and first[0].op == "open"
    # same trend again next poll: nothing new
    again = eng.observe(rows, now=NOW + timedelta(minutes=5))
    assert again == []
    assert len(eng.open_findings) == 1


def test_clears_when_trend_abates():
    eng = MaintenanceEngine()
    opened = eng.observe(series("AMR-01", temp_per_hr=6.0), now=NOW)
    fid = opened[0].incident_id
    later = NOW + timedelta(hours=1)
    acts = eng.observe(series("AMR-01", temp_per_hr=0.0), now=later)
    assert len(acts) == 1
    a = acts[0]
    assert a.op == "patch"
    assert a.incident_id == fid
    assert a.row == {"state": "Cleared", "cleared_at": later.isoformat()}
    assert eng.open_findings == {}


def test_missing_data_does_not_clear():
    eng = MaintenanceEngine()
    eng.observe(series("AMR-01", temp_per_hr=6.0), now=NOW)
    # robot vanished from telemetry: finding must stay open, no actions
    assert eng.observe([], now=NOW + timedelta(hours=1)) == []
    assert len(eng.open_findings) == 1


def test_seed_restores_dedup_after_restart():
    eng = MaintenanceEngine()
    eng.seed([{"id": "MF-4242", "robot_id": "AMR-01",
               "component": "drive motor", "created_at": T0.isoformat()}])
    acts = eng.observe(series("AMR-01", temp_per_hr=6.0), now=NOW)
    assert acts == []  # already open -> deduped
    # and the seeded id is the one that gets cleared
    cleared = eng.observe(series("AMR-01", temp_per_hr=0.0),
                          now=NOW + timedelta(hours=1))
    assert cleared[0].incident_id == "MF-4242"


def test_finding_id_deterministic():
    a = finding_id("AMR-01", "battery", NOW)
    assert a == finding_id("AMR-01", "battery", NOW)
    assert a.startswith("MF-") and len(a) == 7
    assert a != finding_id("AMR-01", "drivetrain", NOW)


# -- sink payloads --------------------------------------------------------

def make_sink(requests: list[httpx.Request]) -> MaintenanceSink:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)
    return MaintenanceSink(
        client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_sink_open_posts_upsert_to_maintenance_findings():
    reqs: list[httpx.Request] = []
    sink = make_sink(reqs)
    eng = MaintenanceEngine()
    acts = eng.observe(series("AMR-01", temp_per_hr=6.0), now=NOW)
    assert sink.apply(acts) == 1
    (r,) = reqs
    assert r.method == "POST"
    assert r.url.path.endswith("/rest/v1/maintenance_findings")
    assert r.url.params["on_conflict"] == "id"
    assert "merge-duplicates" in r.headers["Prefer"]
    assert r.headers["apikey"] == sink.key
    body = json.loads(r.content)
    assert isinstance(body, list) and body[0]["component"] == "drive motor"
    assert body[0]["state"] == "Open"
    sink.close()


def test_sink_clear_patches_by_id():
    reqs: list[httpx.Request] = []
    sink = make_sink(reqs)
    eng = MaintenanceEngine()
    (opened,) = eng.observe(series("AMR-01", temp_per_hr=6.0), now=NOW)
    later = NOW + timedelta(hours=1)
    acts = eng.observe(series("AMR-01", temp_per_hr=0.0), now=later)
    sink.apply(acts)
    r = reqs[-1]
    assert r.method == "PATCH"
    assert r.url.params["id"] == f"eq.{opened.incident_id}"
    assert json.loads(r.content) == {"state": "Cleared",
                                     "cleared_at": later.isoformat()}
    sink.close()


def test_sink_error_is_logged_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    sink = MaintenanceSink(
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    eng = MaintenanceEngine()
    acts = eng.observe(series("AMR-01", temp_per_hr=6.0), now=NOW)
    assert sink.apply(acts) == 1  # attempted, not raised
    sink.close()


# -- CLI ------------------------------------------------------------------

def make_cli_client(telemetry: list[dict], requests: list[httpx.Request],
                    open_findings: list[dict] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/robot_telemetry"):
            return httpx.Response(200, json=telemetry)
        if request.method == "GET" and request.url.path.endswith("/maintenance_findings"):
            return httpx.Response(200, json=open_findings or [])
        return httpx.Response(201)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cli_maintenance_once_posts_findings():
    reqs: list[httpx.Request] = []
    client = make_cli_client(series("AMR-01", temp_per_hr=6.0), reqs)
    assert run(["--maintenance", "--once"], client=client) == 0
    gets = [r for r in reqs if r.method == "GET"]
    # seed fetch (open findings) + telemetry window
    assert any(r.url.path.endswith("/maintenance_findings") for r in gets)
    tele = [r for r in gets if r.url.path.endswith("/robot_telemetry")]
    assert len(tele) == 1
    assert "gte." in tele[0].url.params["ts"]
    posts = [r for r in reqs if r.method == "POST"]
    assert len(posts) == 1
    assert posts[0].url.path.endswith("/maintenance_findings")
    assert json.loads(posts[0].content)[0]["robot_id"] == "AMR-01"


def test_cli_maintenance_seeded_open_finding_dedups():
    reqs: list[httpx.Request] = []
    seed = [{"id": "MF-4242", "robot_id": "AMR-01",
             "component": "drive motor", "created_at": T0.isoformat()}]
    client = make_cli_client(series("AMR-01", temp_per_hr=6.0), reqs,
                             open_findings=seed)
    assert run(["--maintenance", "--once"], client=client) == 0
    assert [r for r in reqs if r.method == "POST"] == []


def test_cli_maintenance_dry_run_writes_nothing(capsys):
    reqs: list[httpx.Request] = []
    client = make_cli_client(series("AMR-01", temp_per_hr=6.0), reqs)
    assert run(["--maintenance", "--once", "--dry-run"], client=client) == 0
    assert [r for r in reqs if r.method in ("POST", "PATCH")] == []
    assert "[dry-run] OPEN" in capsys.readouterr().out


def test_cli_maintenance_stable_series_no_writes():
    reqs: list[httpx.Request] = []
    client = make_cli_client(series("AMR-01", temp_per_hr=0.0), reqs)
    assert run(["--maintenance", "--once"], client=client) == 0
    assert [r for r in reqs if r.method in ("POST", "PATCH")] == []
