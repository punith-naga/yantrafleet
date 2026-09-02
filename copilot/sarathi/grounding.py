"""Grounding guard — verify that numbers in an answer come from tool data.

Post-hoc output rail for the LLM tiers (1 and 2): extract the numeric
tokens from the final answer text (skipping timestamps, clock times,
robot/alert/incident-style ids, and evidence source refs, none of which
are "figures" the model could hallucinate meaningfully) and require each
one to appear somewhere in the tool-result payloads that back the answer.

Tolerances:
* int/float formatting — "8", "8.0" and 8 all match (compared as floats);
* rounding to 1 decimal — a payload value of 54.16 supports an answer
  token of "54.2" (models legitimately round when quoting).

Tier-3 answers are rendered from tool data by construction, so they are
labelled ``computed`` rather than run through this check.
"""
from __future__ import annotations

import json
import re

from .tools import ToolResult

GROUNDING_VERIFIED = "verified"      # every number backed by tool data
GROUNDING_UNVERIFIED = "unverified"  # at least one unsupported number
GROUNDING_COMPUTED = "computed"      # tier 3: grounded by construction

# Tokens that are *not* figures and must be ignored on both sides:
_SKIP_RES = [
    # ISO-8601 timestamps: 2026-08-26T10:14:00Z / with offset / date-only.
    re.compile(
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
        r"(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    ),
    # Evidence source refs: tool:8-hex-digest:... (the digest + trailing ts).
    re.compile(r"\b[A-Za-z_]\w*:[0-9a-f]{8}:\S*"),
    # Clock times: 10:31, 09:58:12.
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    # Entity ids: R-004, AL-1, INC-2, M-1, c-100, bot_7 ...
    re.compile(r"\b[A-Za-z][A-Za-z0-9]{0,11}[-_]\d{1,6}\b"),
]

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _strip_non_figures(text: str) -> str:
    for rx in _SKIP_RES:
        text = rx.sub(" ", text)
    return text


def extract_numbers(text: str) -> list[str]:
    """Numeric tokens in ``text`` that count as figures (order preserved).

    Timestamps, clock times, entity ids and evidence refs are skipped.
    """
    return _NUM_RE.findall(_strip_non_figures(text))


def payload_numbers(tool_log: list[ToolResult]) -> set[float]:
    """All numeric values present in the tool-result payloads, as floats."""
    corpus = json.dumps([t.data for t in tool_log], default=str)
    return {float(tok) for tok in extract_numbers(corpus)}


def _supported(n: float, values: set[float]) -> bool:
    if n in values:
        return True
    # Rounding tolerance: the answer may quote a value rounded to 1 decimal.
    return any(round(v, 1) == round(n, 1) for v in values)


def verify_grounding(
    answer: str, tool_log: list[ToolResult]
) -> tuple[str, list[str]]:
    """Check every figure in ``answer`` against the tool payloads.

    Returns ``(status, offending)`` where status is ``verified`` or
    ``unverified`` and ``offending`` lists the unsupported tokens as they
    appear in the answer (deduplicated, order preserved).
    """
    values = payload_numbers(tool_log)
    offending: list[str] = []
    for tok in extract_numbers(answer):
        if not _supported(float(tok), values) and tok not in offending:
            offending.append(tok)
    return (GROUNDING_UNVERIFIED if offending else GROUNDING_VERIFIED), offending
