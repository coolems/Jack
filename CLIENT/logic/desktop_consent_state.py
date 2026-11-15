"""desktop_consent_state - CLIENT-side fallback store for Computer Use access (2026-11-07).

WHY THIS EXISTS
The real desktop consent store lives in the SERVER-shipped dep tools/computer_use/_consent.py
(in-memory, session-scoped). install_shared_deps() registers it as sys.modules['tools._consent'],
and logic/tool_executor.py uses THAT object when it is installed - one store, two writers.

But a desktop tool can be refused BEFORE any computer_use module is loaded (the tool failed to
compile, or a sync orchestrator is in use). In that state there is no store at all, so the gate
would fail closed with 'unavailable' and the user would never be asked. This module is the
degraded-path store: the SAME decisions, persisted to <working_root>/.temp/desktop_consent.json
so a restart does not silently re-ask for an app the user already blocked.

Same API surface as the dep (consent_key_for_call / call_granted / grant_call / deny_call /
call_identity / was_denied / register_context / clear_context), so tool_executor.py's gate code
is identical whichever store it gets. Fail-closed everywhere: unknown key, unknown conversation
or an unreadable file => not granted.

Scope rules mirror the dep: grants are per conversation id; a denial blocks the app for the
whole process; the run-wide grant ('allow until task done') applies to the desktop kind only.
"""

import json
import logging
import os
import re
import threading

logger = logging.getLogger("COOLEMS.Logic.DesktopConsent")

#: Key covering ENUMERATION (listing visible window titles). Reads app names, so it is
#: gated too - but it is not per-app access, so it has its own key.
ENUMERATE_KEY = "desktop:enumerate"

#: Anchor the dep uses for the per-call conversation channel. Registered here too so the
#: tools' own fail-closed check sees a channel even in the degraded path.
CONTEXT_ANCHOR = "_coolems_desktop_context"

#: Anchor holding the decisions - MUST be the same literal name as
#: tools/computer_use/_consent.py's STATE_ANCHOR (2026-11-07 fix).
#:
#: WHY: loader.globals_builder.install_shared_deps() execs each dep in its own namespace and
#: then REPLACES sys.modules['tools.<dep>'] with a NEW module object, per tool compile. The
#: consent gate in logic/tool_executor.py runs BEFORE that compile, so module-local dicts in
#: either store would be written into one object and read from another - the user allowed
#: Chrome on the card and cua_get_app still answered DESKTOP_CONSENT_REQUIRED. Both writers
#: therefore record into ONE process-wide holder, and the JSON file below persists it.
STATE_ANCHOR = "_coolems_desktop_consent_state"


def _state():
    """The shared decision holder (decisions/run_grants/denied/lock), created once."""
    import sys
    import types
    holder = sys.modules.get(STATE_ANCHOR)
    if holder is None:
        holder = types.ModuleType(STATE_ANCHOR)
        holder.decisions = {}
        holder.run_grants = set()
        holder.denied = set()
        holder.lock = threading.Lock()
        sys.modules[STATE_ANCHOR] = holder
    return holder


def _norm_identity(app_identity: str) -> str:
    """Canonical form of a grant key - identical rule to the dep's _norm_identity.

    Path-shaped keys are normcase'd (Windows is case-insensitive) so a card that granted
    'C:\\Foo\\Bar.exe' covers the same app when a later call resolves the handle to
    'c:\\foo\\bar.exe'. 'pid:<n>' and 'desktop:enumerate' stay as written.
    """
    text = str(app_identity or "").strip()
    if not text:
        return ""
    if "\\" in text or "/" in text or re.match(r"^[A-Za-z]:", text):
        return os.path.normcase(text)
    return text


_persisted = False


def _state_path() -> str:
    """<working_root>/.temp/desktop_consent.json, or '' when the root is unavailable.

    .temp is this project's scratch folder (never the root), and the file holds only
    decisions - no app data beyond the identity the user was shown.
    """
    try:
        from tools.utils import get_working_root
        root = get_working_root()
    except Exception:
        return ""
    if not root or not os.path.isdir(root):
        return ""
    return os.path.join(root, ".temp", "desktop_consent.json")


def _load_once() -> None:
    """Read the persisted decisions once per process. Best-effort, never raises."""
    global _persisted
    if _persisted:
        return
    _persisted = True
    path = _state_path()
    if not path or not os.path.isfile(path):
        return
    st = _state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            conv = data.get("conversations")
            if isinstance(conv, dict):
                with st.lock:
                    st.decisions.update({str(k): {str(a): str(v) for a, v in dict(c).items()}
                                         for k, c in conv.items()})
            denied = data.get("denied")
            if isinstance(denied, list):
                with st.lock:
                    st.denied.update(str(d) for d in denied)
    except Exception as exc:
        logger.debug("[DESKTOP-CONSENT] persisted state unreadable (%r) - starting clean", exc)


def _save() -> None:
    """Persist the decisions. Best-effort: a write failure never blocks a decision."""
    path = _state_path()
    if not path:
        return
    st = _state()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with st.lock:
            payload = {"conversations": dict(st.decisions), "denied": sorted(st.denied)}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        logger.debug("[DESKTOP-CONSENT] persist failed (non-fatal): %r", exc)


def consent_identity(selector) -> str:
    """Canonical consent key for an app selector - identical rule to the dep.

    Paths are normcase'd (Windows is case-insensitive); a pid is keyed 'pid:<n>' so a
    bare number cannot collide with an app name; anything else is a lowercased name.
    """
    if isinstance(selector, dict):
        path = selector.get("path")
        if path:
            return os.path.normcase(str(path).strip())
        pid = selector.get("pid")
        if pid is not None:
            return f"pid:{int(pid)}"
        name = selector.get("name") or selector.get("title")
        return str(name).strip().lower() if name else ""
    if isinstance(selector, (int, float)):
        return f"pid:{int(selector)}"
    if isinstance(selector, str):
        text = selector.strip()
        if not text:
            return ""
        if text.isdigit():
            return f"pid:{int(text)}"
        if "\\" in text or "/" in text or re.match(r"^[A-Za-z]:", text):
            return os.path.normcase(text)
        return text.lower()
    return ""


def register_context(conversation_id: str, websocket=None) -> None:
    """Attach this call's conversation id to CONTEXT_ANCHOR (same shape as the dep)."""
    import sys
    import types
    mod = sys.modules.get(CONTEXT_ANCHOR)
    if mod is None:
        mod = types.ModuleType(CONTEXT_ANCHOR)
        sys.modules[CONTEXT_ANCHOR] = mod
    if not hasattr(mod, "context_local"):
        import threading as _th
        mod.context_local = _th.local()
    if conversation_id:
        mod.context_local.context = {"conversation_id": str(conversation_id),
                                     "websocket": websocket}
    else:
        mod.context_local.context = None


def clear_context() -> None:
    """Detach the channel after a call."""
    import sys
    mod = sys.modules.get(CONTEXT_ANCHOR)
    local = getattr(mod, "context_local", None) if mod is not None else None
    if local is not None:
        local.context = None


def current_conversation_id() -> str:
    """Conversation id of the running desktop call, '' when there is no channel."""
    import sys
    mod = sys.modules.get(CONTEXT_ANCHOR)
    local = getattr(mod, "context_local", None) if mod is not None else None
    ctx = getattr(local, "context", None) if local is not None else None
    if isinstance(ctx, dict):
        return str(ctx.get("conversation_id") or "")
    return ""


def consent_key_for_call(tool_name: str, args) -> str:
    """The grant key for THIS desktop call (degraded path: selector / enumeration only).

    Handles, windows and snapshots cannot be resolved without the dep's registry, so they
    return '' -> the call fails closed instead of guessing an app.
    """
    if tool_name == "cua_list_apps":
        return ENUMERATE_KEY
    args = args if isinstance(args, dict) else {}
    if args.get("selector"):
        return consent_identity(args.get("selector"))
    return ""


def call_identity(tool_name: str, args) -> str:
    """Human-readable app name/path for the consent card ('' when unidentifiable)."""
    if tool_name == "cua_list_apps":
        return "every running app (window titles)"
    args = args if isinstance(args, dict) else {}
    return str(args.get("selector") or "")


def is_granted(app_identity: str, conversation_id: str) -> bool:
    """True only when this conversation explicitly allowed this app. Default False."""
    if not app_identity or not conversation_id:
        return False
    _load_once()
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        if conversation_id in st.run_grants:
            return True
        return st.decisions.get(conversation_id, {}).get(key) == "granted"


def was_denied(app_identity: str, conversation_id: str = "") -> bool:
    """True when the user refused this app (this process, or this conversation)."""
    if not app_identity:
        return False
    _load_once()
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        if key in st.denied:
            return True
        return st.decisions.get(conversation_id or "", {}).get(key) == "denied"


def grant_app(app_identity: str, conversation_id: str, run_wide: bool = False) -> bool:
    """Record the user's Allow. False when identity/conversation is empty."""
    if not app_identity or not conversation_id:
        logger.warning("[DESKTOP-CONSENT] grant refused (empty identity/conversation)")
        return False
    _load_once()
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        st.decisions.setdefault(conversation_id, {})[key] = "granted"
        st.denied.discard(key)
        if run_wide:
            st.run_grants.add(conversation_id)
    _save()
    return True


def deny_app(app_identity: str, conversation_id: str) -> bool:
    """Record the user's Deny. Fail-closed: this identity stays blocked for the session."""
    if not app_identity:
        return False
    _load_once()
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        st.decisions.setdefault(conversation_id or "", {})[key] = "denied"
        st.denied.add(key)
    _save()
    return True


def call_granted(tool_name: str, args, conversation_id: str) -> bool:
    """True when THIS conversation already allowed the app this call targets."""
    return is_granted(consent_key_for_call(tool_name, args), conversation_id)


def grant_call(tool_name: str, args, conversation_id: str, run_wide: bool = False) -> bool:
    """Record the user's Allow for the exact app this call targets."""
    return grant_app(consent_key_for_call(tool_name, args), conversation_id, run_wide=run_wide)


def deny_call(tool_name: str, args, conversation_id: str) -> bool:
    """Record the user's Deny for the exact app this call targets."""
    return deny_app(consent_key_for_call(tool_name, args), conversation_id)


def revoke_app(app_identity: str, conversation_id: str = "") -> int:
    """Withdraw one app's access from every conversation (mirrors the dep)."""
    key = _norm_identity(app_identity)
    st = _state()
    _load_once()
    with st.lock:
        removed = 0
        for conv in list(st.decisions):
            if st.decisions[conv].pop(key, None) is not None:
                removed += 1
        st.denied.discard(key)
    _save()
    return removed


def revoke_all() -> int:
    """Withdraw everything (per-app decisions, denied marks, run-wide grants)."""
    _load_once()
    st = _state()
    with st.lock:
        count = sum(len(v) for v in st.decisions.values())
        st.decisions.clear()
        st.run_grants.clear()
        st.denied.clear()
    _save()
    logger.info("[DESKTOP-CONSENT] all desktop access revoked (%d entries)", count)
    return count


def consent_summary() -> dict:
    """Compact state for the UI/logs."""
    _load_once()
    st = _state()
    with st.lock:
        return {"granted": sum(1 for v in st.decisions.values() for s in v.values() if s == "granted"),
                "denied": len(st.denied), "run_grants": len(st.run_grants)}
