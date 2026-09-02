"""Predictive-maintenance engine + sink (``maintenance_findings`` table).

``MaintenanceEngine`` is pure (no I/O, no clocks): feed it the recent
``robot_telemetry`` window (list of sample dicts, all robots mixed) plus
an explicit ``now``; it returns :class:`~yantradetect.engine.Action`
objects that a sink turns into PostgREST writes.

Three heuristics, one component each:

* **drive motor** — least-squares linear trend of ``motor_temp`` over
  the window. A slope >= ``temp_slope_c_per_hr`` (default 1.5 °C/hr)
  with a decent fit opens a finding; RUL = hours until ``temp_crit``
  (default 85 °C) at the current slope, expressed in days.
* **battery** — battery drained per *active* minute, compared against
  the fleet median of the same figure. A robot draining
  >= ``battery_ratio`` x the median (default 1.5x, needs >= 3 robots
  with data) opens a finding; RUL shrinks as the ratio grows.
* **drivetrain** — mean active speed in the late half of the window vs
  the early half. A decline >= ``speed_decline`` (default 15%) opens a
  finding; RUL shrinks as the decline grows.

Dedup mirrors ``IncidentEngine``: at most one *open* finding per
``(robot_id, component)``. While one is open the engine stays quiet for
that pair; when the metric is computable again and back under the
threshold, the finding is patched ``Cleared`` (with ``cleared_at``).
Missing/insufficient data never clears a finding — only positive
evidence of normality does.

Determinism: ids are ``MF-<4 digits>`` hashed from
``robot_id:component:created_at`` so crash-and-retry re-emits the
identical row and the upsert (``on_conflict=id``) is idempotent.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

import httpx

from yantracore import normalize, site_id

from .engine import Action, _parse_ts
from .sink import resolve_config

log = logging.getLogger(__name__)

__all__ = ["MaintenanceEngine", "MaintenanceSink", "OpenFinding", "finding_id"]

COMPONENTS = ("drive motor", "battery", "drivetrain")

ACTIONS = {
    "drive motor": "Inspect drive motor cooling and bearings; schedule service",
    "battery": "Run battery health test; plan pack replacement",
    "drivetrain": "Inspect drivetrain (wheels/gearbox) for wear or obstruction",
}


def finding_id(robot_id: str, component: str, created_at: datetime) -> str:
    """Deterministic ``MF-XXXX`` id (idempotent across retries)."""
    seed = f"{robot_id}:{component}:{created_at.isoformat()}"
    n = int.from_bytes(hashlib.sha1(seed.encode()).digest()[:4], "big")
    return f"MF-{2000 + n % 8000}"


def linear_trend(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares fit of ``(x, y)`` points -> ``(slope, r_squared)``.

    ``x`` is hours; slope is therefore units/hr. Returns ``(0.0, 0.0)``
    for degenerate input (fewer than 2 points, or zero x-variance).
    """
    n = len(points)
    if n < 2:
        return 0.0, 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx <= 0:
        return 0.0, 0.0
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    slope = sxy / sxx
    r2 = 0.0 if syy <= 0 else (sxy * sxy) / (sxx * syy)
    return slope, r2


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class OpenFinding:
    """In-memory record of one open maintenance finding."""

    id: str
    robot_id: str
    component: str
    created_at: datetime


@dataclass(frozen=True)
class _Metric:
    """One computed heuristic result for a robot+component."""

    crossed: bool
    finding: str = ""
    rul_days: float = 0.0
    confidence: float = 0.0


class MaintenanceEngine:
    """Feed me telemetry windows; I emit open/clear finding actions."""

    def __init__(
        self,
        temp_slope_c_per_hr: float = 1.5,
        temp_crit: float = 85.0,
        min_samples: int = 6,
        battery_ratio: float = 1.5,
        min_active_minutes: float = 5.0,
        speed_decline: float = 0.15,
    ) -> None:
        self.temp_slope_c_per_hr = temp_slope_c_per_hr
        self.temp_crit = temp_crit
        self.min_samples = min_samples
        self.battery_ratio = battery_ratio
        self.min_active_minutes = min_active_minutes
        self.speed_decline = speed_decline
        # dedup: at most one open finding per (robot_id, component) --------
        self.open_findings: dict[tuple[str, str], OpenFinding] = {}

    # -- restart support ----------------------------------------------------

    def seed(self, open_rows: Iterable[dict[str, Any]]) -> None:
        """Rebuild dedup state from already-open rows
        (``GET /maintenance_findings?state=eq.Open``)."""
        for r in open_rows:
            rid = str(r.get("robot_id") or "")
            comp = str(r.get("component") or "")
            if not rid or comp not in COMPONENTS:
                continue
            created = _parse_ts(r.get("created_at")) or datetime.now(timezone.utc)
            self.open_findings[(rid, comp)] = OpenFinding(
                id=str(r["id"]), robot_id=rid, component=comp, created_at=created,
            )

    # -- main entry ---------------------------------------------------------

    def observe(self, samples: list[dict[str, Any]],
                now: datetime | None = None) -> list[Action]:
        """Process one telemetry window (all robots mixed); return actions."""
        now = now or datetime.now(timezone.utc)
        by_robot = self._group(samples)

        # battery drain needs the fleet median, so compute rates first ------
        drain_rates: dict[str, float] = {}
        for rid, rows in by_robot.items():
            rate = self._drain_rate(rows)
            if rate is not None:
                drain_rates[rid] = rate
        fleet_median = median(drain_rates.values()) if len(drain_rates) >= 3 else None

        actions: list[Action] = []
        for rid, rows in sorted(by_robot.items()):
            metrics = {
                "drive motor": self._motor_metric(rows),
                "battery": self._battery_metric(rid, drain_rates, fleet_median),
                "drivetrain": self._drivetrain_metric(rows),
            }
            for comp, m in metrics.items():
                actions.extend(self._transition(rid, comp, m, now))
        return actions

    # -- transitions --------------------------------------------------------

    def _transition(self, rid: str, comp: str, m: _Metric | None,
                    now: datetime) -> list[Action]:
        key = (rid, comp)
        open_f = self.open_findings.get(key)
        if m is None:  # not computable: never open, never clear
            return []
        if m.crossed:
            if open_f is not None:
                return []  # dedup: one open finding per robot+component
            f = OpenFinding(id=finding_id(rid, comp, now), robot_id=rid,
                            component=comp, created_at=now)
            self.open_findings[key] = f
            row = {
                "id": f.id,
                "robot_id": rid,
                "component": comp,
                "finding": m.finding,
                "rul_days": round(m.rul_days, 1),
                "confidence": round(_clamp(m.confidence, 0.0, 1.0), 2),
                "action": ACTIONS[comp],
                "state": "Open",
                "site_id": site_id(),
                "created_at": now.isoformat(),
            }
            return [Action("open", f.id, row)]
        if open_f is not None:  # computable and normal again -> clear
            del self.open_findings[key]
            return [Action("patch", open_f.id,
                           {"state": "Cleared", "cleared_at": now.isoformat()})]
        return []

    # -- heuristics ---------------------------------------------------------

    def _motor_metric(self, rows: list[dict[str, Any]]) -> _Metric | None:
        pts: list[tuple[float, float]] = []
        for r in rows:
            ts, temp = r.get("_ts"), r.get("motor_temp")
            if ts is None or temp is None:
                continue
            pts.append((ts.timestamp() / 3600.0, float(temp)))
        if len(pts) < self.min_samples:
            return None
        t0 = pts[0][0]
        pts = [(x - t0, y) for x, y in pts]
        if pts[-1][0] <= 0:
            return None
        slope, r2 = linear_trend(pts)
        if slope < self.temp_slope_c_per_hr or r2 < 0.5:
            return _Metric(crossed=False)
        last = pts[-1][1]
        rul_days = _clamp((self.temp_crit - last) / slope / 24.0, 0.5, 60.0)
        conf = 0.35 + 0.5 * r2 + (0.1 if len(pts) >= 10 else 0.0)
        return _Metric(
            crossed=True,
            finding=(f"Motor temp trending +{slope:.1f} °C/hr "
                     f"(now {last:.0f} °C)"),
            rul_days=rul_days,
            confidence=conf,
        )

    def _drain_rate(self, rows: list[dict[str, Any]]) -> float | None:
        """Battery % drained per active minute; None without enough data."""
        drop = 0.0
        minutes = 0.0
        for a, b in zip(rows, rows[1:]):
            if normalize(a.get("status")) != "active":
                continue
            if normalize(b.get("status")) != "active":
                continue
            ta, tb = a.get("_ts"), b.get("_ts")
            ba, bb = a.get("battery"), b.get("battery")
            if None in (ta, tb, ba, bb):
                continue
            dt_min = (tb - ta).total_seconds() / 60.0
            if not 0 < dt_min <= 30:  # skip gaps and out-of-order rows
                continue
            d = float(ba) - float(bb)
            if d < 0:  # charging blip mid-"active"; ignore
                continue
            drop += d
            minutes += dt_min
        if minutes < self.min_active_minutes:
            return None
        return drop / minutes

    def _battery_metric(self, rid: str, rates: dict[str, float],
                        fleet_median: float | None) -> _Metric | None:
        rate = rates.get(rid)
        if rate is None or fleet_median is None or fleet_median <= 0:
            return None
        ratio = rate / fleet_median
        if ratio < self.battery_ratio:
            return _Metric(crossed=False)
        rul_days = _clamp(90.0 / ratio, 3.0, 60.0)
        conf = 0.5 + 0.15 * (ratio - self.battery_ratio)
        return _Metric(
            crossed=True,
            finding=(f"Battery drains {rate:.2f}%/active-min — "
                     f"{ratio:.1f}x fleet median ({fleet_median:.2f})"),
            rul_days=rul_days,
            confidence=conf,
        )

    def _drivetrain_metric(self, rows: list[dict[str, Any]]) -> _Metric | None:
        pts: list[tuple[datetime, float]] = []
        for r in rows:
            ts, spd = r.get("_ts"), r.get("speed")
            if ts is None or spd is None:
                continue
            if normalize(r.get("status")) != "active":
                continue
            pts.append((ts, float(spd)))
        if len(pts) < 2 * 3:  # need >=3 samples in each half
            return None
        mid = pts[0][0] + (pts[-1][0] - pts[0][0]) / 2
        early = [v for t, v in pts if t <= mid]
        late = [v for t, v in pts if t > mid]
        if len(early) < 3 or len(late) < 3:
            return None
        e = sum(early) / len(early)
        l = sum(late) / len(late)
        if e <= 0:
            return None
        decline = 1.0 - l / e
        if decline < self.speed_decline:
            return _Metric(crossed=False)
        rul_days = _clamp(7.0 * (0.5 / decline), 2.0, 45.0)
        conf = 0.4 + decline
        return _Metric(
            crossed=True,
            finding=(f"Active speed down {decline * 100:.0f}% over window "
                     f"({e:.2f} -> {l:.2f} m/s)"),
            rul_days=rul_days,
            confidence=conf,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _group(samples: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Group by robot_id, parse ts into ``_ts``, sort ascending."""
        out: dict[str, list[dict[str, Any]]] = {}
        for s in samples:
            rid = str(s.get("robot_id") or "")
            ts = _parse_ts(s.get("ts"))
            if not rid or ts is None:
                continue
            row = dict(s)
            row["_ts"] = ts
            out.setdefault(rid, []).append(row)
        for rows in out.values():
            rows.sort(key=lambda r: r["_ts"])
        return out


class MaintenanceSink:
    """``maintenance_findings`` writer + telemetry reader — the
    :class:`~yantradetect.sink.PostgRESTSink` pattern for maintenance.

    Opens are POSTed with ``on_conflict=id`` +
    ``Prefer: resolution=merge-duplicates`` (deterministic ids make
    retries idempotent); clears are ``PATCH ?id=eq.<id>``. An injectable
    ``httpx.Client`` keeps every test offline.
    """

    TABLE = "maintenance_findings"

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url, self.key = resolve_config(url, key)
        self.rest = f"{self.base_url}/rest/v1"
        self._client = client or httpx.Client(timeout=timeout_s)

    # -- Sink protocol ------------------------------------------------------

    def apply(self, actions: Iterable[Action]) -> int:
        """Apply actions in order; failures are logged, never fatal
        (next poll re-derives the state). Returns actions attempted."""
        n = 0
        for a in actions:
            n += 1
            try:
                if a.op == "open":
                    resp = self._client.post(
                        f"{self.rest}/{self.TABLE}",
                        params={"on_conflict": "id"},
                        json=[a.row],
                        headers={**self._headers(),
                                 "Prefer": "resolution=merge-duplicates, return=minimal"},
                    )
                else:
                    resp = self._client.patch(
                        f"{self.rest}/{self.TABLE}",
                        params={"id": f"eq.{a.incident_id}"},
                        json=a.row,
                        headers={**self._headers(), "Prefer": "return=minimal"},
                    )
                if resp.status_code >= 400:
                    log.warning("%s %s %s -> %s: %s", self.TABLE, a.op,
                                a.incident_id, resp.status_code, resp.text[:200])
            except httpx.HTTPError as exc:
                log.warning("%s %s %s failed: %s", self.TABLE, a.op,
                            a.incident_id, exc)
        return n

    # -- reads used by the CLI ---------------------------------------------

    def fetch_telemetry(self, window_hours: float = 6.0,
                        now: datetime | None = None) -> list[dict[str, Any]]:
        """Recent telemetry window, oldest first per robot."""
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(hours=window_hours)
        resp = self._client.get(
            f"{self.rest}/robot_telemetry",
            params={
                "select": "robot_id,ts,battery,speed,motor_temp,status",
                "ts": f"gte.{since.isoformat()}",
                "order": "robot_id.asc,ts.asc",
            },
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json() or []

    def fetch_open_findings(self) -> list[dict[str, Any]]:
        """Open rows for engine dedup seeding; [] on any failure."""
        try:
            resp = self._client.get(
                f"{self.rest}/{self.TABLE}",
                params={"state": "eq.Open",
                        "select": "id,robot_id,component,created_at"},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as exc:  # noqa: BLE001 - availability over purity
            log.warning("open-finding seed fetch failed: %s", exc)
            return []

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
