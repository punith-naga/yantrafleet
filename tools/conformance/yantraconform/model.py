"""Report data model and the scoring/grading rules.

Pure data + arithmetic; importable without paho, without a broker, and
without any of this repo's other packages.

SCORING MODEL (deliberately simple enough to argue with)
--------------------------------------------------------
Every check carries a severity. Severity is a weight:

    critical 10   the fleet manager cannot work at all if this is wrong
    major     5   a real integration will misbehave in production
    minor     2   spec deviation with a workaround
    info      0   reported, never scored

Every check result carries a status, which is a credit multiplier:

    pass  1.0    warn  0.5    fail  0.0    skip  excluded from both sides

    score = 100 * sum(weight * credit) / sum(weight)     [scored checks only]

Then two honesty caps are applied on top of the letter grade, because a
weighted average alone lets a vehicle with one fatal defect still look like a
B: any failed CRITICAL check caps the grade at F, and any failed MAJOR check
caps it at B. Caps are reported explicitly in ``grade_capped_by`` so nobody
has to reverse-engineer why an 89 came out a B rather than an A.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

#: Report schema version. Bump on any breaking shape change; a web layer
#: should refuse to render a document whose major differs from what it knows.
SCHEMA_VERSION = "1.0"
TOOL_NAME = "yantra-conform"
TOOL_VERSION = "0.1.0"

SEVERITIES = ("critical", "major", "minor", "info")
STATUSES = ("pass", "warn", "fail", "skip")

SEVERITY_WEIGHT: dict[str, int] = {
    "critical": 10, "major": 5, "minor": 2, "info": 0,
}
STATUS_CREDIT: dict[str, float] = {"pass": 1.0, "warn": 0.5, "fail": 0.0}

#: (minimum score, grade). Evaluated top-down.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (95.0, "A"), (85.0, "B"), (70.0, "C"), (50.0, "D"), (0.0, "F"),
)
_GRADE_ORDER = ("A", "B", "C", "D", "F")

#: severity of a FAILED check -> the best grade still attainable.
FAILURE_GRADE_CAP: dict[str, str] = {"critical": "F", "major": "B"}


def grade_for(score: float) -> str:
    for floor, grade in GRADE_BANDS:
        if score >= floor:
            return grade
    return "F"


def _worse(a: str, b: str) -> str:
    return a if _GRADE_ORDER.index(a) >= _GRADE_ORDER.index(b) else b


@dataclass
class CheckResult:
    """One individually-scored conformance check."""

    id: str
    title: str
    category: str
    severity: str            # critical | major | minor | info
    spec_ref: str            # e.g. "VDA 5050 2.1 §6.4"
    status: str              # pass | warn | fail | skip
    expected: str            # what the spec requires, in one line
    observed: str            # what this AGV actually did, in one line
    remediation: str = ""    # what an integrator should change
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"bad severity {self.severity!r}")
        if self.status not in STATUSES:
            raise ValueError(f"bad status {self.status!r}")

    @property
    def weight(self) -> int:
        return SEVERITY_WEIGHT[self.severity]

    @property
    def scored(self) -> bool:
        return self.status != "skip" and self.weight > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "severity": self.severity,
            "spec_ref": self.spec_ref,
            "status": self.status,
            "expected": self.expected,
            "observed": self.observed,
            "remediation": self.remediation,
            "detail": self.detail,
        }


def tally(checks: Iterable[CheckResult]) -> dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for c in checks:
        counts[c.status] += 1
    return counts


def score_checks(checks: Iterable[CheckResult]) -> tuple[float, str, list[str]]:
    """Return ``(score, grade, capped_by)`` for a bag of check results."""
    checks = list(checks)
    possible = sum(c.weight for c in checks if c.scored)
    earned = sum(c.weight * STATUS_CREDIT[c.status] for c in checks if c.scored)
    score = round(100.0 * earned / possible, 1) if possible else 0.0
    base = grade_for(score)
    grade = base
    capped_by: list[str] = []
    for c in checks:
        if c.status != "fail" or c.severity not in FAILURE_GRADE_CAP:
            continue
        cap = FAILURE_GRADE_CAP[c.severity]
        if _GRADE_ORDER.index(cap) > _GRADE_ORDER.index(base):
            capped_by.append(c.id)
            grade = _worse(grade, cap)
    return score, grade, sorted(set(capped_by))


@dataclass
class RobotReport:
    """Everything the tester learned about one vehicle."""

    manufacturer: str
    serial: str
    interface: str
    major: str
    checks: list[CheckResult] = field(default_factory=list)
    version_reported: str | None = None
    topics_seen: list[str] = field(default_factory=list)
    message_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def topic_prefix(self) -> str:
        return "/".join((self.interface, self.major, self.manufacturer, self.serial))

    def to_dict(self) -> dict[str, Any]:
        score, grade, capped = score_checks(self.checks)
        return {
            "manufacturer": self.manufacturer,
            "serial": self.serial,
            "interface": self.interface,
            "major_version": self.major,
            "topic_prefix": self.topic_prefix,
            "version_reported": self.version_reported,
            "score": score,
            "grade": grade,
            "grade_capped_by": capped,
            "counts": tally(self.checks),
            "message_counts": self.message_counts,
            "topics_seen": sorted(self.topics_seen),
            "notes": self.notes,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class Report:
    """The whole run: one or more vehicles plus run metadata."""

    broker: str = ""
    generated_at: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    robots: list[RobotReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_checks(self) -> list[CheckResult]:
        return [c for r in self.robots for c in r.checks]

    def to_dict(self) -> dict[str, Any]:
        checks = self.all_checks
        score, grade, capped = score_checks(checks)
        by_category: dict[str, dict[str, int]] = {}
        for c in checks:
            bucket = by_category.setdefault(
                c.category, {s: 0 for s in STATUSES})
            bucket[c.status] += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "spec": "VDA 5050 2.1.0",
            "generated_at": self.generated_at,
            "broker": self.broker,
            "options": self.options,
            "summary": {
                "robots": len(self.robots),
                "score": score,
                "grade": grade,
                "grade_capped_by": capped,
                "counts": tally(checks),
                "by_category": by_category,
                "failed_checks": sorted({c.id for c in checks if c.status == "fail"}),
            },
            "warnings": self.warnings,
            "robots": [r.to_dict() for r in self.robots],
        }
