"""Runtime configuration, resolved from environment variables with safe defaults.

The Supabase anon (publishable) key is client-safe by design — it is the same
key the browser console embeds — so shipping it as a default is acceptable.
Override with SUPABASE_URL / SUPABASE_KEY for a different project.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Client-safe defaults for the YantraFleet demo project.
DEFAULT_SUPABASE_URL = "https://flwyvhsmgrrqpmhcqlzd.supabase.co"
DEFAULT_SUPABASE_KEY = "sb_publishable_7rqvPRggmPDRKNL8Jurcqg_Hf531puf"

# Default models per provider (free-tier friendly).
DEFAULT_GEMINI_MODEL = "gemini/gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one service instance."""

    supabase_url: str
    supabase_key: str
    model: str | None          # litellm model id, or None if no LLM key present
    llm_timeout_s: float = 30.0
    max_agent_turns: int = 6
    tool_row_limit: int = 50
    low_battery_threshold: float = 20.0
    extra: dict = field(default_factory=dict)


def _detect_model() -> str | None:
    """Pick a litellm model id based on which API keys are present.

    SARATHI_MODEL always wins when set. Otherwise prefer Gemini (free tier),
    then OpenAI. Returns None when no key is available — the service will run
    tier-3 (offline) only, which is fully supported.
    """
    explicit = os.environ.get("SARATHI_MODEL")
    if explicit:
        return explicit
    if os.environ.get("GEMINI_API_KEY"):
        return DEFAULT_GEMINI_MODEL
    if os.environ.get("OPENAI_API_KEY"):
        return DEFAULT_OPENAI_MODEL
    return None


def load_settings() -> Settings:
    """Build Settings from the current process environment."""
    return Settings(
        supabase_url=os.environ.get("SUPABASE_URL", DEFAULT_SUPABASE_URL).rstrip("/"),
        supabase_key=os.environ.get("SUPABASE_KEY", DEFAULT_SUPABASE_KEY),
        model=_detect_model(),
    )
