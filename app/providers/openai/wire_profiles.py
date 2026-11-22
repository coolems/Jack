"""Wire profiles: how each OpenAI model FAMILY must be called.

One table is the single source of truth for the request shape. A new family only needs
a row here - payload.py, thinking.py, models.py, streaming.py and responses.py all read
it, so no call path is duplicated per model.

Fields:
  token_key            output-ceiling key the family accepts
                     ("max_tokens" classic, "max_completion_tokens" reasoning families)
  sampling_params      whether temperature/top_p are accepted (reasoning families 400)
  reasoning_key        the reasoning knob name ("" = family takes no reasoning key)
  reasoning_placement  where that key goes ("top" is the only shape OpenAI accepts;
                       a nested text.format body is rejected as "Unknown parameter: 'text'")
  efforts              reasoning_effort levels, cheapest first
  default_effort       the family's own default effort
  tools_mode           how function tools must be routed (see TOOLS_* constants)

Verified against api.openai.com: gpt-5/gpt-6/o-series reject max_tokens, temperature,
top_p and 'text'; they take max_completion_tokens + top-level reasoning_effort.

Tool routing (tools_mode), also verified live:
  "chat"                tools go in /v1/chat/completions, reasoning_effort allowed
                        (gpt-5, gpt-5.1, gpt-5.2, o3, classic families)
  "chat_no_reasoning"   tools go in /v1/chat/completions but reasoning_effort must be
                        dropped (gpt-5.4, gpt-5.5 400 on "Function tools with
                        reasoning_effort are not supported")
  "responses"           tools must use /v1/responses - these families reject tools in
                        /v1/chat/completions even with reasoning_effort omitted, and do
                        not accept reasoning_effort='none' (gpt-5.6, gpt-6)
"""

from dataclasses import dataclass, replace
from typing import Dict, Tuple

# The two shapes OpenAI serves today.
CLASSIC = "classic"
REASONING = "reasoning"

# tools_mode values (how function tools must be routed for the family).
TOOLS_CHAT = "chat"
TOOLS_CHAT_NO_REASONING = "chat_no_reasoning"
TOOLS_RESPONSES = "responses"


@dataclass(frozen=True)
class WireProfile:
    """The request shape one OpenAI family accepts."""

    name: str
    token_key: str = "max_tokens"
    sampling_params: bool = True
    reasoning_key: str = ""
    reasoning_placement: str = "top"
    tools_mode: str = TOOLS_CHAT
    efforts: Tuple[str, ...] = ()
    default_effort: str = ""


_CLASSIC = WireProfile(CLASSIC)
_REASONING = WireProfile(REASONING, token_key="max_completion_tokens",
                         sampling_params=False, reasoning_key="reasoning_effort",
                         efforts=("low", "medium", "high"), default_effort="medium")
# Newer reasoning families that can take tools in /chat/completions only with the
# reasoning knob omitted (an explicit reasoning_effort + tools is a 400).
_REASONING_NO_TOOLS_REASONING = replace(_REASONING,
                                        tools_mode=TOOLS_CHAT_NO_REASONING)
# Reasoning families that reject function tools in /v1/chat/completions outright and
# must be called through /v1/responses when tools are present.
_REASONING_TOOLS_RESPONSES = replace(_REASONING, tools_mode=TOOLS_RESPONSES)

# Family prefix -> profile. Longest non-digit-prefix wins, same rule as models.metadata_for.
FAMILIES: Dict[str, WireProfile] = {
    "gpt-4o": _CLASSIC,
    "gpt-4o-mini": _CLASSIC,
    "gpt-4o-audio": _CLASSIC,
    "gpt-4.1": _CLASSIC,
    "gpt-5": _REASONING,
    "gpt-5.1": _REASONING,
    "gpt-5.2": _REASONING,
    "gpt-5.4": _REASONING_NO_TOOLS_REASONING,
    "gpt-5.5": _REASONING_NO_TOOLS_REASONING,
    "gpt-5.6": _REASONING_TOOLS_RESPONSES,
    "gpt-6": _REASONING_TOOLS_RESPONSES,
    "o1": _REASONING,
    "o3": _REASONING,
    "o4-mini": _REASONING,
}

# An id with no row: the classic shape (max_tokens + temperature, no reasoning key).
DEFAULT_PROFILE = _CLASSIC


def family_for(model_id: str) -> str:
    """The family prefix a model id belongs to, "" when no row matches.

    A family matches when the id starts with it AND the next char is not a digit, so
    "gpt-5.1-2025-11-13" matches gpt-5.1 (not gpt-5) and "gpt-5-2025-08-07" matches gpt-5.
    """
    mid = (model_id or "").strip().lower()
    if mid in FAMILIES:
        return mid
    best = ""
    for family in FAMILIES:
        if not mid.startswith(family):
            continue
        nxt = mid[len(family):len(family) + 1]
        if nxt and nxt.isdigit():
            continue
        if len(family) > len(best):
            best = family
    return best


def profile_for(model_id: str) -> WireProfile:
    """The wire profile for one model id: exact row, longest family prefix, else default."""
    return FAMILIES.get(family_for(model_id), DEFAULT_PROFILE)


def is_reasoning(model_id: str) -> bool:
    """True for families that need max_completion_tokens and reject sampling params."""
    return profile_for(model_id).name == REASONING
