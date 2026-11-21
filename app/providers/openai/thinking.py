"""OpenAI thinking policy - the hosted API's own vocabulary, from the static model table.

OpenAI exposes reasoning as reasoning_effort (low/medium/high), placed differently per
family: o-series models take a TOP-LEVEL reasoning_effort, gpt-5 nests it under
text.format.reasoning_effort, and non-reasoning models (gpt-4o, gpt-4.1) take no
reasoning key at all.

So the canonical UI levels map onto OpenAI's spellings here:
  none   -> no reasoning_effort key (OpenAI has no off switch; the model keeps its own)
  low    -> low
  medium -> medium
  high   -> high
The UI still sees the canonical levels through ThinkingPolicy.describe(), so the picker
needs no change; only the wire shape differs.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..llama.thinking import ThinkingPolicy, normalize_mode

# OpenAI's effort levels, cheapest first.
OPENAI_EFFORTS = ("low", "medium", "high")

# Canonical level -> OpenAI effort spelling (identity for the shared levels).
_LEVEL_TO_EFFORT = {"low": "low", "medium": "medium", "high": "high"}


class OpenAIThinkingPolicy(ThinkingPolicy):
    """Thinking capability of one OpenAI model, read from its static metadata row.

    placement is where the body key goes: "top" (o-series) or "text" (gpt-5's
    text.format). No token-budget knob exists, so budget stays False.
    """

    def __init__(self, *, placement: str = "top", **kwargs):
        super().__init__(**kwargs)
        self.placement = placement if placement in ("top", "text") else "top"

    def map(self, mode: str) -> Optional[str]:
        """OpenAI's spelling for a canonical level, or None when it has none."""
        mode = self.normalize(mode)
        if not mode or mode == "none":
            return None
        wanted = _LEVEL_TO_EFFORT.get(mode)
        if wanted and wanted in self.efforts:
            return wanted
        if not self.efforts:
            return None
        # A model with an unexpected level list: take the closest one we do have.
        ranked = [e for e in OPENAI_EFFORTS if e in self.efforts]
        if not ranked:
            return None
        if mode == "high":
            return ranked[-1]
        if mode == "low":
            return ranked[0]
        return ranked[len(ranked) // 2] if len(ranked) > 1 else ranked[0]

    @staticmethod
    def normalize(mode: str) -> str:
        return normalize_mode(mode)

    @classmethod
    def from_model(cls, meta: dict, override: Optional[dict] = None) -> "OpenAIThinkingPolicy":
        """Build from one static metadata row ({efforts, default, placement}).

        No efforts means the model says nothing about reasoning: still display-capable
        (reasoning models stream reasoning_content), but no effort levels are sent.
        """
        meta = meta or {}
        supported = [str(e).strip().lower() for e in (meta.get("efforts") or [])
                     if str(e).strip()]
        ordered = [e for e in OPENAI_EFFORTS if e in supported]
        ordered += [e for e in supported if e not in ordered]
        default = str(meta.get("default") or "").strip().lower()
        hint = "" if ordered else "this model reports no effort levels - no " \
                                  "reasoning_effort is sent"
        policy = cls(toggle=False, efforts=ordered, default=default, budget=False,
                     source="openai", hint=hint, placement=meta.get("placement") or "top")
        return cls.from_override(override, policy) if override else policy

    def levels(self) -> List[str]:
        """Canonical levels to offer: every level this model can be told."""
        out = []
        for mode in ("low", "medium", "high"):
            if self.map(mode) is not None:
                out.append(mode)
        return out or ["none"]

    @classmethod
    def merge(cls, policies: Sequence[ThinkingPolicy]) -> "OpenAIThinkingPolicy":
        """One policy for a model served by several endpoints: the INTERSECTION.

        ThinkingPolicy.merge builds a plain ThinkingPolicy, which would lose OpenAI's
        level mapping and placement, so the result is rebuilt here from the shared
        effort intersection.
        """
        live = [p for p in policies if p is not None]
        if not live:
            return cls.from_model({})
        if len(live) == 1:
            return live[0]
        common = set(live[0].efforts)
        for p in live[1:]:
            common &= set(p.efforts)
        ordered = [e for e in OPENAI_EFFORTS if e in common]
        ordered += [e for e in common if e not in ordered]
        return cls(toggle=all(p.toggle for p in live), efforts=ordered,
                   default=live[0].default, budget=False, source="merged",
                   hint=live[0].hint, placement=getattr(live[0], "placement", "top"))


def thinking_body(policy: ThinkingPolicy, mode: str) -> Dict:
    """The OpenAI body keys for one canonical level; {} sends no reasoning keys.

    No level (or 'none') means the model keeps its own default, which is exactly what
    an un-set picker should do. The key placement follows the policy (o-series top
    level, gpt-5 under text.format).
    """
    if not hasattr(policy, "placement"):
        return {}
    mode = policy.normalize(mode)
    if not mode or mode == "none":
        return {}
    effort = policy.map(mode)
    if not effort:
        return {}
    if policy.placement == "text":
        return {"text": {"format": {"type": "text", "reasoning_effort": effort}}}
    return {"reasoning_effort": effort}


def effort_levels(policy: ThinkingPolicy) -> Sequence[str]:
    """The model's own effort spellings (for logs)."""
    return tuple(policy.efforts)
