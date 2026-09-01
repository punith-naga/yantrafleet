"""Pure incident-detection engine (no I/O, no clocks, no network).

Industry patterns baked in (PagerDuty dedup keys, Prometheus ``for:``
pending windows and clear-hold hysteresis, BigPanda/Rootly re-open
windows):

* **Edge detection** — the engine is fed successive snapshots of the
  ``robots`` table (``list[dict]``) plus an explicit ``now`` timestamp
  and compares each robot against the state it remembered from the
  previous poll. It emits :class:`Action` objects; a sink turns those
  into PostgREST calls (or prints them under ``--dry-run``).
* **Dedup** — at most one open incident per robot, keyed by
  ``robot_id`` (PagerDuty ``dedup_key`` style).
* **Flap guard (pending window)** — a robot must be seen abnormal on
  ``pending_polls`` *consecutive* polls (default 2) before an incident
  opens, so one-poll sensor blips never page anyone.
* **Clear hold** — the condition must stay clear ``clear_polls``
  consecutive polls before the incident resolves (default 1 = resolve
  on the first clear poll; raise it for Prometheus-style hysteresis).
* **Re-open window** — if the same robot goes bad again within
  ``reopen_window_s`` of resolution, the *same* incident re-opens
  (``flap_count`` incremented, original ``opened_at`` kept) instead of
  a new row spamming the console.
* **Stale auto-resolve** — a robot absent from the snapshot for
  ``stale_polls`` consecutive polls has its open incident resolved
  with a "stale/no-data" note.

Determinism: incident ids are ``INC-<4 digits>`` derived by hashing
``robot_id : condition : opened_at``, so a crash-and-retry re-emits the
identical row and the PostgREST upsert (``on_conflict=id``) is
idempotent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from yantracore import NOT_OPERATING, normalize

__all__ = ["Action", "IncidentEngine", "OpenIncident"]

#: severity per condition — a FATAL fault outranks a recoverable e-stop.
SEV: dict[str, str] = {"fault": "crit", "estop": "serious"}


def _tlabel(now: datetime) -> str:
    return now.strftime("%H:%M")


def _minutes(start: datetime, now: datetime) -> int:
    return max(0, int((now - start).total_seconds() // 60))


def incident_id(robot_id: str, condition: str, opened_at: datetime) -> str:
    """Deterministic ``INC-XXXX`` id (idempotent across retries)."""
    seed = f"{robot_id}:{condition}:{opened_at.isoformat()}"
    n = int.from_bytes(hashlib.sha1(seed.encode()).digest()[:4], "big")
    return f"INC-{2000 + n % 8000}"


def _title(robot_id: str, condition: str, fault_msg: str | None) -> str:
    if condition == "estop":
        base = f"Emergency stop on {robot_id}"
    else:
        base = f"{robot_id} fault"
    if fault_msg:
        return f"{base} — {fault_msg}"
    return base if condition == "estop" else f"{base} — robot not operating"


def _impact_open(mins: int) -> str:
    return f"{mins} min so far · robot out of service"


def _impact_resolved(mins: int) -> str:
    return f"{mins} min robot out of service · recovered"


@dataclass(frozen=True)
class Action:
    """One sink instruction. ``open`` carries a full incident row;
    ``patch`` carries a partial update for ``incident_id``."""

    op: Literal["open", "patch"]
    incident_id: str
    row: dict[str, Any]


@dataclass
class OpenIncident:
    """In-memory record of one open (or recently resolved) incident."""

    id: str
    robot_id: str
    condition: str  # fault | estop
    opened_at: datetime
    resolved_at: datetime | None = None
    flap_count: int = 0
    last_dur: int = 0  # last emitted duration (minutes)

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None


class IncidentEngine:
    """Feed me successive robot snapshots; I emit open/resolve actions.

    Pure: all state lives in instance attributes, time is an argument,
    and the return value of :meth:`observe` is the only output.
    """

    def __init__(
        self,
        pending_polls: int = 2,
        clear_polls: int = 1,
        reopen_window_s: float = 300.0,
        stale_polls: int = 6,
        flap_threshold: int = 3,
    ) -> None:
        if pending_polls < 1:
            raise ValueError("pending_polls must be >= 1")
        if clear_polls < 1:
            raise ValueError("clear_polls must be >= 1")
        self.pending_polls = pending_polls
        self.clear_polls = clear_polls
        self.reopen_window_s = reopen_window_s
        self.stale_polls = stale_polls
        self.flap_threshold = flap_threshold
        # dedup: at most one entry per robot ---------------------------------
        self.open_incidents: dict[str, OpenIncident] = {}
        # recently resolved, kept for the re-open window ---------------------
        self._recent: dict[str, OpenIncident] = {}
        self._bad_streak: dict[str, int] = {}    # consecutive abnormal polls
        self._bad_cond: dict[str, str] = {}      # condition seen during streak
        self._clear_streak: dict[str, int] = {}  # consecutive clear polls
        self._missing: dict[str, int] = {}       # consecutive absent polls

    # -- restart support ----------------------------------------------------

    def seed(self, open_rows: Iterable[dict[str, Any]]) -> None:
        """Rebuild in-memory state from already-open incident rows
        (``GET /incidents?state=eq.Open``) after a restart."""
        for r in open_rows:
            rid = str(r.get("src") or "")
            if not rid:
                continue
            opened = _parse_ts(r.get("created_at")) or datetime.now(timezone.utc)
            cond = "fault" if r.get("sev") == "crit" else "estop"
            self.open_incidents[rid] = OpenIncident(
                id=str(r["id"]), robot_id=rid, condition=cond, opened_at=opened,
                last_dur=int(r.get("dur") or 0),
            )

    # -- main entry ---------------------------------------------------------

    def observe(self, rows: list[dict[str, Any]], now: datetime | None = None
                ) -> list[Action]:
        """Process one poll's snapshot; return the actions it implies."""
        now = now or datetime.now(timezone.utc)
        actions: list[Action] = []
        seen: set[str] = set()

        for row in rows:
            rid = str(row.get("id") or "")
            if not rid:
                continue
            seen.add(rid)
            self._missing[rid] = 0
            status = normalize(row.get("status"))
            if status in NOT_OPERATING:
                actions.extend(self._on_bad(rid, status, row, now))
            else:
                actions.extend(self._on_clear(rid, now))

        actions.extend(self._sweep_stale(seen, now))
        self._expire_recent(now)
        return actions

    # -- transitions --------------------------------------------------------

    def _on_bad(self, rid: str, condition: str, row: dict[str, Any],
                now: datetime) -> list[Action]:
        self._clear_streak[rid] = 0
        if self._bad_cond.get(rid) == condition:
            self._bad_streak[rid] = self._bad_streak.get(rid, 0) + 1
        else:  # condition changed mid-streak; restart the pending window
            self._bad_cond[rid] = condition
            self._bad_streak[rid] = 1

        inc = self.open_incidents.get(rid)
        if inc is not None:
            # already open: keep duration/impact fresh once per minute
            mins = _minutes(inc.opened_at, now)
            if mins != inc.last_dur:
                inc.last_dur = mins
                return [Action("patch", inc.id,
                               {"dur": mins, "impact": _impact_open(mins)})]
            return []

        if self._bad_streak[rid] < self.pending_polls:
            return []  # flap guard: not abnormal long enough yet

        # re-open window: same robot bad again shortly after resolution?
        recent = self._recent.get(rid)
        if (recent is not None and recent.resolved_at is not None
                and (now - recent.resolved_at).total_seconds()
                <= self.reopen_window_s):
            recent.resolved_at = None
            recent.condition = condition
            recent.flap_count += 1
            mins = _minutes(recent.opened_at, now)
            recent.last_dur = mins
            self.open_incidents[rid] = recent
            del self._recent[rid]
            patch: dict[str, Any] = {
                "state": "Open", "dur": mins, "impact": _impact_open(mins),
            }
            if recent.flap_count >= self.flap_threshold:
                patch["impact"] += " · flapping"
            return [Action("patch", recent.id, patch)]

        # fresh open
        opened = now
        inc = OpenIncident(
            id=incident_id(rid, condition, opened), robot_id=rid,
            condition=condition, opened_at=opened, last_dur=0,
        )
        self.open_incidents[rid] = inc
        row_out: dict[str, Any] = {
            "id": inc.id,
            "sev": SEV[condition],
            "title": _title(rid, condition, row.get("fault_msg")),
            "src": rid,
            "tlabel": _tlabel(now),
            "state": "Open",
            "impact": _impact_open(0),
            "dur": 0,
            # console orders incidents by created_at.desc and seed() restores
            # opened_at from it after a restart — always send it explicitly
            # rather than relying on a DB column default.
            "created_at": opened.isoformat(),
        }
        return [Action("open", inc.id, row_out)]

    def _on_clear(self, rid: str, now: datetime) -> list[Action]:
        self._bad_streak[rid] = 0
        self._bad_cond.pop(rid, None)
        inc = self.open_incidents.get(rid)
        if inc is None:
            return []
        self._clear_streak[rid] = self._clear_streak.get(rid, 0) + 1
        if self._clear_streak[rid] < self.clear_polls:
            return []  # clear-hold hysteresis
        return [self._resolve(inc, now, _impact_resolved(_minutes(inc.opened_at, now)))]

    def _resolve(self, inc: OpenIncident, now: datetime, impact: str) -> Action:
        mins = _minutes(inc.opened_at, now)
        inc.resolved_at = now
        inc.last_dur = mins
        del self.open_incidents[inc.robot_id]
        self._recent[inc.robot_id] = inc
        self._clear_streak[inc.robot_id] = 0
        return Action("patch", inc.id,
                      {"state": "Resolved", "dur": mins, "impact": impact})

    def _sweep_stale(self, seen: set[str], now: datetime) -> list[Action]:
        """Resolve incidents whose robot vanished from telemetry."""
        actions: list[Action] = []
        for rid in list(self.open_incidents):
            if rid in seen:
                continue
            self._missing[rid] = self._missing.get(rid, 0) + 1
            if self._missing[rid] > self.stale_polls:
                inc = self.open_incidents[rid]
                mins = _minutes(inc.opened_at, now)
                actions.append(self._resolve(
                    inc, now,
                    f"{mins} min robot out of service · auto-resolved (stale/no-data)",
                ))
        return actions

    def _expire_recent(self, now: datetime) -> None:
        for rid in list(self._recent):
            r = self._recent[rid]
            if (r.resolved_at is not None
                    and (now - r.resolved_at).total_seconds()
                    > self.reopen_window_s):
                del self._recent[rid]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
