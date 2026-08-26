"""Degradation ladder orchestrator.

    tier 1 "grounded"  — tools + LLM (needs an API key AND the data backend)
    tier 2 "llm_only"  — LLM without live data (data backend down)
    tier 3 "offline"   — computed template answers, no LLM (no key, or LLM down)

The service always returns HTTP 200 with ``tier`` set — clients render the
caveat, never an error page.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings
from .llm import LLMError, tier1_answer, tier2_answer
from .offline import OfflineEngine
from .tools import Toolbox, ToolResult
from .transport import Transport, TransportError

log = logging.getLogger("sarathi")

TIER_GROUNDED = "grounded"
TIER_LLM_ONLY = "llm_only"
TIER_OFFLINE = "offline"


@dataclass
class Answer:
    """Service-level answer, tier-tagged, with citation evidence."""

    answer: str
    evidence: list[dict[str, str]] = field(default_factory=list)
    tier: str = TIER_OFFLINE


def _evidence(tool_log: list[ToolResult]) -> list[dict[str, str]]:
    return [{"label": t.label(), "ref": t.source_id} for t in tool_log]


class CopilotService:
    """One instance per app; holds the transport and the degradation logic."""

    def __init__(self, settings: Settings, transport: Transport) -> None:
        self.settings = settings
        self.transport = transport
        self._toolbox_factory: Callable[[], Toolbox] = lambda: Toolbox(
            transport=self.transport, row_limit=settings.tool_row_limit
        )
        self._offline = OfflineEngine(
            self._toolbox_factory,
            low_battery_threshold=settings.low_battery_threshold,
        )

    # -- public API --------------------------------------------------------

    @property
    def llm_available(self) -> bool:
        return self.settings.model is not None

    def ask(self, question: str) -> Answer:
        """Answer a question, degrading through the tiers as needed."""
        if not self.llm_available:
            return self._tier3(question)

        # Tier 1: tool-grounded agent loop.
        toolbox = self._toolbox_factory()
        try:
            answer, tool_log = tier1_answer(
                question,
                toolbox,
                model=self.settings.model or "",
                timeout_s=self.settings.llm_timeout_s,
                max_turns=self.settings.max_agent_turns,
            )
            return Answer(answer=answer, evidence=_evidence(tool_log), tier=TIER_GROUNDED)
        except TransportError as exc:
            log.warning("tier1 -> tier2 (data backend down): %s", exc)
            return self._tier2(question)
        except LLMError as exc:
            log.warning("tier1 -> tier3 (LLM failed): %s", exc)
            return self._tier3(question)
        except Exception as exc:  # never 500 on an /ask — degrade instead
            log.exception("tier1 unexpected failure: %s", exc)
            return self._tier3(question)

    # -- tiers -------------------------------------------------------------

    def _tier2(self, question: str) -> Answer:
        try:
            text = tier2_answer(
                question,
                model=self.settings.model or "",
                timeout_s=self.settings.llm_timeout_s,
            )
            return Answer(answer=text, evidence=[], tier=TIER_LLM_ONLY)
        except LLMError as exc:
            log.warning("tier2 -> tier3 (LLM failed too): %s", exc)
            return self._tier3(question)

    def _tier3(self, question: str) -> Answer:
        off = self._offline.answer(question)
        return Answer(answer=off.answer, evidence=off.evidence, tier=TIER_OFFLINE)

    # -- misc --------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Cheap health probe: which tiers are currently plausible."""
        backend_ok = True
        try:
            self.transport.get("fleet_meta", [("select", "id"), ("limit", "1")])
        except TransportError:
            backend_ok = False
        return {
            "ok": True,
            "llm_configured": self.llm_available,
            "model": self.settings.model,
            "data_backend_ok": backend_ok,
            "best_tier": (
                TIER_GROUNDED if (self.llm_available and backend_ok)
                else TIER_LLM_ONLY if self.llm_available
                else TIER_OFFLINE
            ),
        }
