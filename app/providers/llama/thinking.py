"""ThinkingPolicy - what one model can be told about reasoning, discovered not assumed.

Each GGUF's baked chat template decides what a request may control, and the vocabulary
differs per family: some declare enable_thinking plus reasoning_effort with their own
values, some only a toggle (often with thinking_budget), most instruct models declare
nothing - and a strict template raises on an undeclared variable, so inventing a key
would 400 every request. R1-style models emit reasoning unconditionally with no request
knob at all.

So the capability is read from /props: the endpoint's own chat_template text plus
chat_template_caps. The UI and the wire only ever carry the canonical levels
none / low / medium / high; a policy translates them into the model's own spellings and
produces chat_template_kwargs. An operator can pin the truth with a `thinking` block on a
server entry in config/llama_servers.json or config/strata_servers.json (from_override);
discovery still wins for anything the override does not state.

Pure parsing and mapping: no network, no config import.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

# Canonical levels the UI can offer, cheapest first.
MODES = ("none", "low", "medium", "high")

# Anything the wire may carry for a mode (bools, aliases, hand-written requests).
# '' is deliberately absent: an empty value means "no instruction", not "disable".
_ALIASES = {
    "off": "none", "false": "none", "no": "none", "disabled": "none",
    "on": "medium", "auto": "medium", "true": "medium", "enabled": "medium",
    "think": "medium", "default": "medium",
    "xhigh": "high", "highest": "high", "max": "high", "extra": "high",
    "minimal": "low", "brief": "low", "short": "low",
}

# Ranking of known effort spellings, cheapest first. Unknown spellings keep template order.
_EFFORT_RANK = ("none", "low", "medium", "high", "xhigh")

_IN_TUPLE_RE = re.compile(
    r"(?:resolved_)?reasoning_effort\s+not\s+in\s*\(\s*([^)]*?)\)", re.IGNORECASE)
_IN_LIST_RE = re.compile(
    r"(?:resolved_)?reasoning_effort\s+in\s*\(\s*([^)]*?)\)", re.IGNORECASE)
_DEFAULT_RE = re.compile(
    r"reasoning_effort\|default\(\s*'([a-z0-9]+)'\s*\)", re.IGNORECASE)
_COMPARE_RE = re.compile(
    r"(?:resolved_)?reasoning_effort\s*==\s*'([a-z0-9]+)'", re.IGNORECASE)
_RAISE_RE = re.compile(r"raise_exception\((.{0,180}?)\)", re.IGNORECASE)
_QUOTED_RE = re.compile(r"['\"]([a-z0-9]+)['\"]")

# Families that reason even when their template exposes nothing (hint only).
_REASONING_FAMILIES = ("qwen3", "qwq", "deepseek-r1", "deepseek-r1-", "r1-",
                       "phi4-reasoning", "magistral", "openthoughts", "glm-4.5",
                       "gemma-4", "gemma4", "internlm", "kimi")


def normalize_mode(value) -> str:
    """Canonical mode for anything the UI sent ('' when unusable)."""
    if value is None:
        return ""
    raw = str(value).strip().lower()
    if raw in MODES:
        return raw
    return _ALIASES.get(raw, "")


def level_or_bool(value):
    """A thinking value read off the wire: canonical level string, or bool.

    Both shapes arrive: a bool from a plain caller, a level from the picker. Anything
    unrecognized becomes False - no instruction and no display - so an invented level never
    reaches a strict template.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return normalize_mode(value) or False


class ThinkingPolicy:
    """What one model can be told about thinking, and how to say it in its own words."""

    def __init__(self, *, toggle: bool = False, efforts: Sequence[str] = (),
                 default: Optional[str] = None, preserve: bool = False,
                 budget: bool = False, emit_only: bool = False,
                 enforced: bool = True, source: str = "none", hint: str = "",
                 budgets: Optional[Dict[str, int]] = None):
        self.toggle = bool(toggle)
        # The model's OWN effort spellings, cheapest first (e.g. ['low','medium','xhigh']).
        self.efforts: List[str] = [e for e in efforts if e]
        self.default = default or ""
        self.preserve = bool(preserve)
        self.budget = bool(budget)
        # Per-level reasoning token caps (canonical level -> tokens; 0/absent = no cap).
        # None means "use the global default" (config.LLAMA_THINKING_BUDGET).
        self.budgets: Optional[Dict[str, int]] = budgets
        # True when reasoning may be emitted although no request knob exists
        # (R1-style models, or a llama.cpp node started with --reasoning).
        self.emit_only = bool(emit_only)
        # False when the template declares a knob known not to be honored
        # (Gemma 4 + some llama.cpp builds ignore enable_thinking:false).
        self.enforced = bool(enforced)
        # "template" | "caps" | "override" | "merged" | "emit" | "none"
        self.source = source
        self.hint = hint

    # --- capability ------------------------------------------------------

    @property
    def supported(self) -> bool:
        """True when a request can influence thinking at all."""
        return bool(self.toggle or self.efforts)

    @property
    def can_disable(self) -> bool:
        """True when we can genuinely ask this model NOT to think."""
        return bool(self.toggle or "none" in self.efforts)

    @property
    def can_display(self) -> bool:
        """True when reasoning text may reach us (so the UI may offer a display)."""
        return bool(self.supported or self.emit_only)

    def levels(self) -> List[str]:
        """Canonical levels to offer the UI, cheapest first; 'none' only when disabling is real.

        A toggle with no named efforts has exactly one thing to switch on, so 'medium' is offered
        for it; with no knob but reasoning possible (emit_only) the levels only decide display.
        """
        out: List[str] = []
        if self.can_disable:
            out.append("none")
        for mode in ("low", "medium", "high"):
            if self.map(mode) is not None:
                out.append(mode)
        if not out:
            if self.toggle:
                out = ["none", "medium"]
            elif self.emit_only:
                # Nothing to control, but reasoning may still arrive: offer display only.
                out = ["none", "medium"]
        elif self.toggle and "medium" not in out and self.map("medium") is None:
            out.append("medium")
        return out

    def map(self, mode: str) -> Optional[str]:
        """This model's spelling for a canonical level, or None when it has none."""
        mode = normalize_mode(mode)
        if not mode or mode == "none" or not self.efforts:
            return None
        if mode in self.efforts:
            return mode
        if mode == "high" and "xhigh" in self.efforts:
            return "xhigh"          # Qwen3.8: 'high' is not a template value
        ranked = [e for e in _EFFORT_RANK if e in self.efforts and e != "none"]
        ranked += [e for e in self.efforts if e not in ranked and e != "none"]
        if not ranked:
            return None
        if mode == "high":
            return ranked[-1]
        want = {"low": 0, "medium": 1}[mode]
        return min(ranked, key=lambda e: abs(ranked.index(e) - want))

    # --- the request -----------------------------------------------------

    def budget_for(self, mode: str) -> int:
        """Reasoning token cap for a canonical level: 0 when there is none.

        The level now influences thinking by budget as well as by effort word. Resolution:
        a per-request override wins, then this model's `budgets` map (server config), then
        the global default config.LLAMA_THINKING_BUDGET. config is imported lazily so this
        module stays import-clean (docstring: no config import at module level).
        """
        mode = normalize_mode(mode)
        if not mode or mode == "none":
            return 0
        budgets = self.budgets
        if budgets is None:
            try:
                from config import LLAMA_THINKING_BUDGET
                budgets = LLAMA_THINKING_BUDGET or {}
            except Exception:  # pragma: no cover - config unavailable in bare tests
                budgets = {}
        try:
            return max(0, int(budgets.get(mode, 0) or 0))
        except (TypeError, ValueError):
            return 0

    def kwargs(self, mode: str, *, preserve: Optional[bool] = None,
               budget: Optional[int] = None) -> Dict:
        """chat_template_kwargs for one request; {} means send nothing.

        Sending nothing matters: a strict template raises on a variable it does not declare.
        reasoning_effort is omitted when thinking is off - templates read it only while thinking
        is enabled, and a stale value can trip the raise-on-unknown check.

        budget: None takes the per-level cap from budget_for(); an explicit int overrides it.
        Either way it is only emitted when the template declares thinking_budget/reasoning_budget
        (self.budget), so a strict template never sees an undeclared variable.
        """
        mode = normalize_mode(mode)
        if not mode or not self.supported:
            return {}
        kw: Dict = {}
        if mode == "none":
            if self.toggle:
                kw["enable_thinking"] = False
            elif "none" in self.efforts:
                kw["reasoning_effort"] = "none"
            else:
                return {}           # cannot be told to stop: send nothing
        else:
            if self.toggle:
                kw["enable_thinking"] = True
            effort = self.map(mode)
            if effort is not None:
                kw["reasoning_effort"] = effort
            elif not self.toggle:
                return {}
            want = self.budget_for(mode) if budget is None else int(budget)
            if want and self.budget:
                kw["thinking_budget"] = want
        if preserve is not None and self.preserve:
            kw["preserve_thinking"] = bool(preserve)
        return kw

    # --- what the catalog / UI sees --------------------------------------

    def describe(self) -> Dict:
        """Catalog entry for the thinking picker (small, JSON-safe)."""
        return {
            "supported": self.supported,
            "can_disable": self.can_disable,
            "can_display": self.can_display,
            "levels": self.levels(),
            "model_values": list(self.efforts),
            "model_default": self.default or None,
            "preserve": self.preserve,
            "budget": self.budget,
            # Per-level token caps actually in force for this model (0 = uncapped).
            "budgets": {m: self.budget_for(m) for m in ("low", "medium", "high")},
            "enforced": self.enforced,
            "source": self.source,
            "hint": self.hint,
        }

    def __repr__(self) -> str:
        return (f"ThinkingPolicy(source={self.source!r}, toggle={self.toggle}, "
                f"efforts={self.efforts}, preserve={self.preserve}, budget={self.budget}, "
                f"emit_only={self.emit_only})")

    # --- discovery -------------------------------------------------------

    @classmethod
    def none(cls) -> "ThinkingPolicy":
        """Nothing discovered yet (endpoint not probed): send nothing, offer nothing.

        The fallback of the design - without a readable template the request carries no thinking
        keys, so the model keeps its default and the turn still succeeds. The UI reads
        supported: False and shows "Think: n/a".
        """
        return cls(source="none", hint="thinking capability not discovered yet")

    @classmethod
    def demote(cls, base: "ThinkingPolicy", reason: str) -> "ThinkingPolicy":
        """Replace a discovered capability with "nothing controllable" after it failed.

        A template can advertise a knob its build does not honor (no --jinja, or a patched
        template raising on its own allow-listed value). The next request then sends no thinking
        keys and the UI shows "Think: n/a" with the reason; emit_only is kept, so reasoning
        display is not lost.
        """
        base = base or cls.none()
        return cls(toggle=False, efforts=(), default="",
                   preserve=False, budget=False, emit_only=base.emit_only,
                   enforced=False, source="runtime", budgets={},
                   hint=f"{reason} - thinking keys are no longer sent to this model "
                        f"(pin it with a 'thinking' block in the server config if it "
                        f"really supports them)")

    @classmethod
    def from_template(cls, template: str, *, caps: Optional[dict] = None,
                      model_name: str = "") -> "ThinkingPolicy":
        """Read the knobs out of the baked chat template.

        The template text is the only trustworthy source: node names and build strings are
        localized and vary per vendor patch, while the template is what actually decides
        whether a request variable is accepted or raises.
        """
        tpl = template or ""
        caps = caps or {}
        declared: List[str] = []
        default = ""
        hint = ""

        if "reasoning_effort" in tpl:
            for rx in (_IN_TUPLE_RE, _IN_LIST_RE):
                m = rx.search(tpl)
                if m:
                    declared = [v for v in _QUOTED_RE.findall(m.group(1)) if v]
                    break
            m = _DEFAULT_RE.search(tpl)
            if m:
                default = m.group(1)
            if not declared:
                declared = [v for v in _COMPARE_RE.findall(tpl)]
            if not declared:
                found = set(_QUOTED_RE.findall(tpl))
                declared = [e for e in _EFFORT_RANK if e in found]
            m = _RAISE_RE.search(tpl)
            if m and "effort" in m.group(1).lower():
                hint = m.group(1).strip().strip("'\"")[:160]

        toggle = "enable_thinking" in tpl
        preserve = ("preserve_thinking" in tpl
                    or bool(caps.get("supports_preserve_reasoning")))
        budget = ("thinking_budget" in tpl or "reasoning_budget" in tpl)

        efforts = [e for e in _EFFORT_RANK if e in declared]
        efforts += [e for e in declared if e not in efforts]
        if default and default not in efforts:
            efforts.insert(0, default)

        enforced = True
        low_name = (model_name or "").lower()
        if toggle and any(f in low_name for f in ("gemma-4", "gemma4")):
            # Known llama.cpp bug: enable_thinking:false does not stop thinking there.
            enforced = False
            hint = hint or ("this build may ignore enable_thinking:false "
                            "(llama.cpp #20182) - thinking can still appear")

        source = "template" if (toggle or efforts) else "none"
        emit_only = False
        if source == "none":
            if any(f in low_name for f in _REASONING_FAMILIES):
                emit_only = True
                hint = hint or ("this model reasons but its template exposes no request "
                                "knob - on llama.cpp enable separation with --reasoning")
            elif caps.get("supports_reasoning") or caps.get("supports_preserve_reasoning"):
                emit_only = True
                source = "caps"
            elif tpl.strip():
                # A template we could read and that genuinely declares nothing. Distinct
                # from `none()` ("could not discover anything") - the UI says which it is.
                hint = hint or ("this model exposes no thinking control - no thinking "
                                "keys are sent and the model keeps its own default")
        return cls(toggle=toggle, efforts=efforts, default=default, preserve=preserve,
                   budget=budget, emit_only=emit_only, enforced=enforced,
                   source=source, hint=hint)

    @classmethod
    def from_props(cls, props: dict, model_name: str = "") -> "ThinkingPolicy":
        """Build from a /props response ({chat_template, chat_template_caps, ...}).

        No template text means nothing was learned (no /props, a build that does not expose it, a
        node still loading): returns none() - send no keys, offer no levels. Guessing a kwarg
        here is the mistake this class exists to prevent.
        """
        if not isinstance(props, dict):
            return cls.none()
        template = props.get("chat_template")
        if not isinstance(template, str) or not template.strip():
            return cls.none()
        name = model_name or str(props.get("model_alias")
                                 or props.get("model_path") or "")
        return cls.from_template(template,
                                 caps=props.get("chat_template_caps") or {},
                                 model_name=name)

    @classmethod
    def from_override(cls, cfg, base: Optional["ThinkingPolicy"] = None) -> "ThinkingPolicy":
        """Pin one model's capability from a config `thinking` block.

        Shapes: {"efforts": [...], "default", "toggle", "preserve", "budget", "enforced",
        "emit_only", "hint", "budgets": {"low": 512, ...}} or the shorthand list
        ["low","medium","xhigh"]. Anything not stated keeps the discovered value.
        """
        base = base or cls.none()
        if not cfg:
            return base
        if isinstance(cfg, (list, tuple)):
            cfg = {"efforts": list(cfg)}
        if not isinstance(cfg, dict):
            return base
        efforts = cfg.get("efforts") or cfg.get("values") or None
        toggle = cfg.get("toggle", cfg.get("supports_toggle", base.toggle))
        preserve = cfg.get("preserve", cfg.get("supports_preserve", base.preserve))
        budget = cfg.get("budget", cfg.get("supports_budget", base.budget))
        default = cfg.get("default", base.default)
        enforced = cfg.get("enforced", base.enforced)
        hint = cfg.get("hint", base.hint)
        emit_only = cfg.get("emit_only", base.emit_only)
        budgets = cfg.get("budgets", base.budgets)
        if isinstance(budgets, dict):
            budgets = {str(k).strip().lower(): v for k, v in budgets.items()}
        else:
            budgets = None
        eff = [str(e).strip().lower() for e in (efforts or base.efforts) if str(e).strip()]
        ordered = [e for e in _EFFORT_RANK if e in eff]
        ordered += [e for e in eff if e not in ordered]
        return cls(toggle=bool(toggle), efforts=ordered, default=str(default or ""),
                   preserve=bool(preserve), budget=bool(budget), emit_only=bool(emit_only),
                   enforced=bool(enforced), source="override", hint=str(hint or ""),
                   budgets=budgets)

    @classmethod
    def merge(cls, policies: Sequence["ThinkingPolicy"]) -> "ThinkingPolicy":
        """One policy for a model served by several backends: the INTERSECTION.

        The same model name can run on two builds with different baked templates. The
        catalog must offer only what EVERY backend accepts, or a request routed to the
        weaker node fails with HTTP 400 on that turn.
        """
        live = [p for p in policies if p is not None]
        if not live:
            return cls.none()
        if len(live) == 1:
            return live[0]
        common = set(live[0].efforts)
        for p in live[1:]:
            common &= set(p.efforts)
        ordered = [e for e in _EFFORT_RANK if e in common]
        ordered += [e for e in common if e not in ordered]
        same_default = {p.default for p in live}
        # Per-level budgets: the SMALLEST stated cap wins (a backend that caps lower must not
        # be asked for more). A backend with budgets={} states "no cap" and is excluded.
        stated = [p.budgets for p in live if p.budgets]
        merged_budgets: Optional[Dict[str, int]] = None
        if stated:
            merged_budgets = {}
            for m in ("low", "medium", "high"):
                vals = [b.get(m) for b in stated if b.get(m)]
                if vals:
                    merged_budgets[m] = min(int(v) for v in vals)
        return cls(toggle=all(p.toggle for p in live), efforts=ordered,
                   default=live[0].default if len(same_default) == 1 else "",
                   preserve=all(p.preserve for p in live),
                   budget=all(p.budget for p in live),
                   emit_only=any(p.emit_only for p in live),
                   enforced=all(p.enforced for p in live),
                   source="merged", hint=live[0].hint, budgets=merged_budgets)
