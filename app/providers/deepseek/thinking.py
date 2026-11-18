"""DeepSeek thinking policy - the hosted API's own vocabulary, from /models metadata.

DeepSeek exposes reasoning as two body fields, not chat_template_kwargs:
  thinking: {"type": "enabled" | "disabled"}
  reasoning_effort: one of the model's effort levels from /models
  (deepseek-flash / deepseek-v4-pro report ["low","high","max"], default "high")

So the canonical UI levels map onto DeepSeek's spellings here:
  none   -> thinking:disabled (no effort key)
  low    -> low
  medium -> high
  high   -> max
The UI still sees the canonical levels through ThinkingPolicy.describe(), so the picker
needs no change; only the wire shape differs.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..llama.thinking import ThinkingPolicy, normalize_mode

# DeepSeek's effort levels, cheapest first.
DEEPSEEK_EFFORTS = ("low", "high", "max")

# Canonical level -> DeepSeek effort spelling.
_LEVEL_TO_EFFORT = {"low": "low", "medium": "high", "high": "max"}


class DeepSeekThinkingPolicy(ThinkingPolicy):
    """Thinking capability of one DeepSeek model, read from its /models entry.

    toggle=True because thinking.type is a real switch; efforts come from the model's
    effort.supported_levels. No token-budget knob exists, so budget stays False.
    """

    def map(self, mode: str) -> Optional[str]:
        """DeepSeek's spelling for a canonical level, or None when it has none."""
        mode = self.normalize(mode)
        if not mode or mode == "none":
            return None
        wanted = _LEVEL_TO_EFFORT.get(mode)
        if wanted and wanted in self.efforts:
            return wanted
        if not self.efforts:
            return None
        # A model with an unexpected level list: take the closest one we do have.
        ranked = [e for e in DEEPSEEK_EFFORTS if e in self.efforts]
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
    def from_model(cls, meta: dict, override: Optional[dict] = None) -> "DeepSeekThinkingPolicy":
        """Build from one /models entry ({effort: {supported_levels, default_level}}).

        No effort block means the model says nothing about reasoning: still toggle-capable
        (thinking.type is documented on the API), but no effort levels are sent.
        """
        effort = (meta or {}).get("effort") or {}
        supported = [str(e).strip().lower() for e in (effort.get("supported_levels") or [])
                     if str(e).strip()]
        ordered = [e for e in DEEPSEEK_EFFORTS if e in supported]
        ordered += [e for e in supported if e not in ordered]
        default = str(effort.get("default_level") or "").strip().lower()
        hint = "" if ordered else ("this model reports no effort levels - only "
                                   "thinking.type is sent")
        policy = cls(toggle=True, efforts=ordered, default=default, budget=False,
                     source="deepseek", hint=hint)
        return cls.from_override(override, policy) if override else policy

    def levels(self) -> List[str]:
        """Canonical levels to offer: none + every level this model can be told."""
        out = ["none"] if self.can_disable else []
        for mode in ("low", "medium", "high"):
            if self.map(mode) is not None:
                out.append(mode)
        return out or ["none", "medium"]

    @classmethod
    def merge(cls, policies: Sequence[ThinkingPolicy]) -> "DeepSeekThinkingPolicy":
        """One policy for a model served by several endpoints: the INTERSECTION.

        ThinkingPolicy.merge builds a plain ThinkingPolicy, which would lose DeepSeek's
        level mapping, so the result is rebuilt here from the shared effort intersection.
        """
        live = [p for p in policies if p is not None]
        if not live:
            return cls.from_model({})
        if len(live) == 1:
            return live[0]
        common = set(live[0].efforts)
        for p in live[1:]:
            common &= set(p.efforts)
        ordered = [e for e in DEEPSEEK_EFFORTS if e in common]
        ordered += [e for e in common if e not in ordered]
        return cls(toggle=all(p.toggle for p in live), efforts=ordered,
                   default=live[0].default, budget=False, source="merged",
                   hint=live[0].hint)


def thinking_body(policy: ThinkingPolicy, mode: str) -> Dict:
    """The DeepSeek body keys for one canonical level; {} sends no thinking keys.

    No level means the model keeps its own default (V4 models default to thinking on with
    effort 'high'), which is exactly what an un-set picker should do.
    """
    mode = getattr(policy, "normalize", lambda m: m)(mode) if hasattr(policy, "normalize") else mode
    if not mode:
        return {}
    if mode == "none":
        return {"thinking": {"type": "disabled"}} if policy.toggle else {}
    body: Dict = {"thinking": {"type": "enabled"}} if policy.toggle else {}
    effort = policy.map(mode)
    if effort:
        body["reasoning_effort"] = effort
    return body


def effort_levels(policy: ThinkingPolicy) -> Sequence[str]:
    """The model's own effort spellings (for logs)."""
    return tuple(policy.efforts)
