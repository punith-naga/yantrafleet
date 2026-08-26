"""Tiers 1 and 2 — LLM-backed answering via litellm.

Tier 1 ("grounded"): an agent loop with the four fleet tools. The system
prompt enforces the anti-hallucination rule (numbers/IDs only from tool
results) and a lightweight ground-check verifies numeric tokens in the
final answer against the tool data, appending a caveat if any fail.

Tier 2 ("llm_only"): a direct completion with no tools; explicitly told to
refuse live numbers. Evidence is empty and the answer is flagged.

litellm is imported lazily so the service (and the offline test suite)
runs without it installed when only tier 3 is needed.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .tools import TOOL_SPECS, Toolbox, ToolResult


class LLMError(RuntimeError):
    """The LLM provider failed (bad key, quota, network, loop overrun)."""


TIER1_SYSTEM = """\
You are Sarathi, the YantraFleet operations copilot. You answer questions
about a robot fleet using ONLY the provided tools.

STRICT GROUNDING RULES:
1. Every number, robot id, alert id, incident id, and timestamp in your
   answer MUST be copied verbatim from a tool result in this conversation.
   Never estimate, extrapolate, or invent values.
2. If no tool returned the requested value, say it is "not available".
3. An empty tool result is not a license to guess — report "no data
   returned" for that query instead.
4. Call tools before answering. Keep the final answer concise (2-4
   sentences), operational, and specific.
"""

TIER2_SYSTEM = """\
You are Sarathi, the YantraFleet operations copilot. The live fleet data
backend is currently UNREACHABLE, so you have NO tools and NO live data.
Answer conceptually only (how to interpret metrics, what to check, general
robotics-ops guidance). You MUST NOT state any specific live number, robot
id, or count — if the question needs live data, say it is unavailable and
suggest what to look at once data returns. Keep it to 2-4 sentences.
"""

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in(text: str) -> set[float]:
    return {float(m) for m in _NUM_RE.findall(text)}


def ground_check(answer: str, tool_log: list[ToolResult]) -> list[float]:
    """Return numeric tokens in ``answer`` that appear in no tool result.

    NeMo-style output rail: serialize every tool result and compare numeric
    tokens as floats (so "8", "8.0", "008" all match).
    """
    corpus = json.dumps([t.data for t in tool_log], default=str)
    available = _numbers_in(corpus)
    return sorted(n for n in _numbers_in(answer) if n not in available)


def tier1_answer(
    question: str,
    toolbox: Toolbox,
    model: str,
    timeout_s: float = 30.0,
    max_turns: int = 6,
) -> tuple[str, list[ToolResult]]:
    """Run the tool-grounded agent loop. Returns (answer, tool_log).

    Raises LLMError on provider failure; propagates TransportError when the
    data backend is down (the service then degrades to tier 2).
    """
    import litellm

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": TIER1_SYSTEM},
        {"role": "user", "content": question},
    ]
    for _ in range(max_turns):
        try:
            resp = litellm.completion(
                model=model,
                messages=messages,
                tools=TOOL_SPECS,
                tool_choice="auto",
                timeout=timeout_s,
            )
        except Exception as exc:  # provider/auth/network — degrade
            raise LLMError(f"litellm completion failed: {exc}") from exc

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            answer = (msg.content or "").strip()
            if not answer:
                raise LLMError("model returned an empty answer")
            # Grounding rail: flag any number not present in tool data.
            bad = ground_check(answer, toolbox.log)
            if bad:
                answer += (
                    " (Note: some figures could not be verified against tool "
                    "data and should be treated with caution.)"
                )
            return answer, list(toolbox.log)

        # Record the assistant turn, then execute each requested tool.
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = toolbox.call(tc.function.name, args)
                payload = json.dumps(
                    {"source_id": result.source_id, "data": result.data},
                    default=str,
                )
            except ValueError as exc:  # unknown tool name from the model
                payload = json.dumps({"error": str(exc)})
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": payload}
            )
    raise LLMError(f"agent loop exceeded {max_turns} turns without an answer")


def tier2_answer(question: str, model: str, timeout_s: float = 30.0) -> str:
    """Direct LLM answer, no tools. Flagged as lower-confidence."""
    import litellm

    try:
        resp = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": TIER2_SYSTEM},
                {"role": "user", "content": question},
            ],
            timeout=timeout_s,
        )
    except Exception as exc:
        raise LLMError(f"litellm completion failed: {exc}") from exc
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise LLMError("model returned an empty answer")
    return f"[lower confidence — live fleet data unavailable] {content}"
