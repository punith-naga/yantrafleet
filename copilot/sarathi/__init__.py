"""sarathi — YantraFleet ops copilot service.

A three-tier, evidence-grounded question-answering service for a robot fleet:

* tier "grounded"  — LLM agent loop with live Supabase tools (litellm).
* tier "llm_only"  — direct LLM answer, no live data, flagged lower-confidence.
* tier "offline"   — deterministic template answers computed from tool data,
                     no LLM involved at all (works with zero API keys).

Numbers in answers always originate from tool results: tier 1 enforces it via
prompt + a grounding check, tier 3 is computed directly from the data.
"""

__version__ = "0.1.0"
