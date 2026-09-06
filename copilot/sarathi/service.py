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

from .config import Settings, resolve_model
from .grounding import (
    GROUNDING_COMPUTED,
    verify_grounding,
)
from .llm import CompletionFn, LLMError, tier1_answer, tier2_answer
from .offline import OfflineEngine
from .settings_sync import SettingsSync
from .tools import Toolbox, ToolResult
from .transport import Transport, TransportError

log = logging.getLogger("sarathi")

TIER_GROUNDED = "grounded"
TIER_LLM_ONLY = "llm_only"
TIER_OFFLINE = "offline"


@dataclass
class Answer:
    """Service-level answer, tier-tagged, with citation evidence.

    ``grounding`` is the output-rail verdict: ``verified`` (every figure
    backed by tool data), ``unverified`` (at least one unsupported figure —
    listed in ``meta["unsupported_numbers"]``), or ``computed`` (tier 3:
    rendered from tool data by construction).
    """

    answer: str
    evidence: list[dict[str, str]] = field(default_factory=list)
    tier: str = TIER_OFFLINE
    grounding: str = GROUNDING_COMPUTED
    meta: dict[str, Any] = field(default_factory=dict)


def _evidence(tool_log: list[ToolResult]) -> list[dict[str, str]]:
    return [{"label": t.label(), "ref": t.source_id} for t in tool_log]


class CopilotService:
    """One instance per app; holds the transport and the degradation logic."""

    def __init__(
        self,
        settings: Settings,
        transport: Transport,
        completion_fn: CompletionFn | None = None,
        settings_sync: SettingsSync | None = None,
        config_sync: Any = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        # LLM seam: None -> real litellm.completion; tests inject a fake.
        self.completion_fn = completion_fn
        # Live settings seam: None means "no admin-settings-panel wiring" —
        # _current_model() then falls back to the model resolved once at
        # process start (self.settings.model), exactly today's behaviour.
        self.settings_sync = settings_sync
        # v0.18: live, non-secret runtime tunables (public.app_config) —
        # a yantracore.runtime_config.TablePoller, or None. Same
        # "None means no wiring, fall back to the value frozen at process
        # start" shape as settings_sync above.
        self.config_sync = config_sync
        self._toolbox_factory: Callable[[], Toolbox] = lambda: Toolbox(
            transport=self.transport, row_limit=settings.tool_row_limit
        )
        self._offline = OfflineEngine(
            self._toolbox_factory,
            low_battery_threshold=settings.low_battery_threshold,
            low_battery_threshold_fn=self._current_low_battery_threshold,
        )

    def _current_low_battery_threshold(self) -> float:
        """Live low-battery threshold: table override (via config_sync)
        else the value resolved once at process start
        (self.settings.low_battery_threshold). Mirrors _current_model()'s
        precedence for GEMINI_API_KEY below."""
        if self.config_sync is not None:
            raw = self.config_sync.get("SARATHI_LOW_BATTERY_THRESHOLD")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        return self.settings.low_battery_threshold

    # -- public API --------------------------------------------------------

    def _current_model(self) -> str | None:
        """Live model resolution: prefers a live signal read through
        settings_sync (table override, else the current env var) so a
        rotated GEMINI_API_KEY takes effect on the very next call with no
        service/app recreation needed. Falls back to ``self.settings.model``
        — the value resolved once at process start — when settings_sync
        has neither an override nor a matching env var right now (e.g. no
        settings_sync at all, or a fresh deployment that hasn't set
        GEMINI_API_KEY/SARATHI_MODEL in the live environment). This keeps
        callers that construct a Settings with an explicit ``model=`` (the
        whole existing test suite) working unchanged, while still letting
        a live override turn tier-1 on/off without a restart.
        """
        if self.settings_sync is not None:
            live = resolve_model(self.settings_sync.get)
            if live is not None:
                return live
        return self.settings.model

    @property
    def llm_available(self) -> bool:
        return self._current_model() is not None

    def ask(self, question: str) -> Answer:
        """Answer a question, degrading through the tiers as needed."""
        model = self._current_model()
        if model is None:
            return self._tier3(question)

        # Tier 1: tool-grounded agent loop.
        toolbox = self._toolbox_factory()
        try:
            answer, tool_log = tier1_answer(
                question,
                toolbox,
                model=model,
                timeout_s=self.settings.llm_timeout_s,
                max_turns=self.settings.max_agent_turns,
                completion_fn=self.completion_fn,
            )
            grounding, offending = verify_grounding(answer, tool_log)
            return Answer(
                answer=answer,
                evidence=_evidence(tool_log),
                tier=TIER_GROUNDED,
                grounding=grounding,
                meta={"unsupported_numbers": offending} if offending else {},
            )
        except TransportError as exc:
            log.warning("tier1 -> tier2 (data backend down): %s", exc)
            return self._tier2(question, model)
        except LLMError as exc:
            log.warning("tier1 -> tier3 (LLM failed): %s", exc)
            return self._tier3(question)
        except Exception as exc:  # never 500 on an /ask — degrade instead
            log.exception("tier1 unexpected failure: %s", exc)
            return self._tier3(question)

    # -- tiers -------------------------------------------------------------

    def _tier2(self, question: str, model: str) -> Answer:
        try:
            text = tier2_answer(
                question,
                model=model,
                timeout_s=self.settings.llm_timeout_s,
                completion_fn=self.completion_fn,
            )
            # Tier 2 has no tool data at all, so *any* figure the model
            # states is unsupported — verify against an empty tool log.
            grounding, offending = verify_grounding(text, [])
            return Answer(
                answer=text,
                evidence=[],
                tier=TIER_LLM_ONLY,
                grounding=grounding,
                meta={"unsupported_numbers": offending} if offending else {},
            )
        except LLMError as exc:
            log.warning("tier2 -> tier3 (LLM failed too): %s", exc)
            return self._tier3(question)

    def _tier3(self, question: str) -> Answer:
        off = self._offline.answer(question)
        return Answer(
            answer=off.answer,
            evidence=off.evidence,
            tier=TIER_OFFLINE,
            grounding=GROUNDING_COMPUTED,
        )

    # -- misc --------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Cheap health probe: which tiers are currently plausible.

        ``transport_ok`` comes from a minimal fleet_meta query (with a short
        timeout when the transport supports one); the probe never raises —
        any failure just reports False.
        """
        transport_ok = True
        try:
            probe = getattr(self.transport, "probe", None)
            if callable(probe):
                probe()  # short-timeout fleet_meta query
            else:
                self.transport.get(
                    "fleet_meta", [("select", "id"), ("limit", "1")]
                )
        except Exception:  # unreachable/misbehaving backend must not crash
            transport_ok = False

        tiers_available = [TIER_OFFLINE]
        if self.llm_available:
            tiers_available.insert(0, TIER_LLM_ONLY)
            if transport_ok:
                tiers_available.insert(0, TIER_GROUNDED)
        return {
            "ok": True,
            "llm_configured": self.llm_available,
            "model": self._current_model() or "none",
            "tiers_available": tiers_available,
            "transport_ok": transport_ok,
            # Back-compat aliases (pre-v0.5 health shape).
            "data_backend_ok": transport_ok,
            "best_tier": tiers_available[0],
        }
