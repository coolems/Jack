"""Per-application access consent for computer_use (fail-closed).

Ported from the harness's application-consent layer. Their rule, kept in behaviour:
an app is only readable/controllable after the LOCAL USER said yes; a denial stops
repeat prompts for that session; revocation drops every handle so no later call can
act on the app.

Why the store lives HERE (a SERVER-shipped dep) and not only in the CLIENT: the tool
functions must verify a grant themselves, and the CLIENT's consent card (consent kind
'desktop', logic/tool_executor.py) writes into the SAME object. install_shared_deps()
registers this dep as sys.modules['tools._consent'], so the CLIENT reaches it through
that anchor - one store, two writers, no duplicated state. The CLIENT never trusts a
grant it did not issue: is_granted() is the only source of truth and defaults False.

Safety properties:
  * fail-closed: unknown conversation, unknown app, or no store => False
  * grants are session-scoped (per conversation id) and die with the process
  * a run-wide grant ("allow until task done") applies to the desktop kind only
  * revoke_app / revoke_all clear everything, including run-wide grants
  * nothing here opens, launches, hides, closes or kills anything - it records decisions
"""

import logging
import os
import re
import sys
import threading

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Consent")

#: Anchor for the chat's consent channel. Same shape as the python_exec relay anchor
#: (_coolems_pyexec_context) set by RemoteToolOrchestrator._call_pyexec_with_context.
CONTEXT_ANCHOR = "_coolems_desktop_context"

#: Key for the one card that covers ENUMERATION (listing visible window titles / installed
#: apps). Enumeration reads app names, so it is gated too - but it is not per-app access,
#: so it gets its own key instead of borrowing an app's grant.
ENUMERATE_KEY = "desktop:enumerate"

#: Anchor holding the decision state (2026-11-07 fix).
#:
#: WHY THE STATE IS NOT MODULE-LOCAL: install_shared_deps() execs each dep in its own
#: namespace and then REPLACES sys.modules['tools.<dep>'] with the new module object. The
#: CLIENT's consent gate runs BEFORE the tool is compiled, so a module-local store would be
#: written into the object the gate resolved and then thrown away when the dep is installed -
#: the tool would read an empty store and refuse an app the user had just allowed (observed
#: live: card answered 'allow_category_until_done' for chrome, tool returned
#: DESKTOP_CONSENT_REQUIRED). One anchor, whatever module object is registered, is the store.
STATE_ANCHOR = "_coolems_desktop_consent_state"


def _state():
    """The process-wide decision store (decisions/run_grants/denied/lock), created once.

    Lives on sys.modules under STATE_ANCHOR so the gate's writer and the tool's reader are
    always the SAME object even after a dep re-install or a tool re-compile.
    """
    import sys
    holder = sys.modules.get(STATE_ANCHOR)
    if holder is None:
        import types
        holder = types.ModuleType(STATE_ANCHOR)
        holder.decisions = {}   # conversation_id -> {app_identity: 'granted'|'denied'}
        holder.run_grants = set()  # conversation ids with the run-wide desktop grant
        holder.denied = set()      # identities denied in this process (no repeat prompts)
        holder.lock = threading.Lock()
        sys.modules[STATE_ANCHOR] = holder
    return holder


def _norm_identity(app_identity: str) -> str:
    """Canonical form of a grant key before it is stored or looked up.

    Windows paths are case-insensitive, so a card that granted 'C:\\Foo\\Bar.exe' must cover
    the same app when a later call resolves its handle to 'c:\\foo\\bar.exe'. Without this the
    mismatch would fail CLOSED (safe, but it would refuse an app the user just allowed). Only
    path-shaped keys are normcase'd - 'pid:<n>' and 'desktop:enumerate' stay as written, which
    is what _desktop_state.consent_identity already produces for them.
    """
    text = str(app_identity or "").strip()
    if not text:
        return ""
    if "\\" in text or "/" in text or re.match(r"^[A-Za-z]:", text):
        return os.path.normcase(text)
    return text


def current_conversation_id() -> str:
    """Conversation id of the running desktop call, '' when there is no consent channel.

    The orchestrator registers the chat's context on CONTEXT_ANCHOR for desktop tool
    calls (threading.local, one worker thread per call). Without it a tool cannot know
    WHICH user approved, so every app-bound call fails closed.
    """
    mod = sys.modules.get(CONTEXT_ANCHOR)
    local = getattr(mod, "context_local", None)
    ctx = getattr(local, "context", None) if local is not None else None
    if isinstance(ctx, dict):
        return str(ctx.get("conversation_id") or "")
    return ""


def register_context(conversation_id: str, websocket=None) -> None:
    """Attach this call's consent channel to CONTEXT_ANCHOR (orchestrator-side helper).

    Kept here so both the CLIENT gate and the tools use one anchor definition. The
    websocket is stored for the tool's own 'cannot ask' reporting; it is never used to
    send anything from this module.
    """
    import types
    mod = sys.modules.get(CONTEXT_ANCHOR)
    if mod is None:
        mod = types.ModuleType(CONTEXT_ANCHOR)
        mod.context_local = threading.local()
        sys.modules[CONTEXT_ANCHOR] = mod
    if not hasattr(mod, "context_local"):
        mod.context_local = threading.local()
    if conversation_id:
        mod.context_local.context = {"conversation_id": str(conversation_id),
                                     "websocket": websocket}
    else:
        mod.context_local.context = None


def clear_context() -> None:
    """Detach the channel after a call (the orchestrator's finally block)."""
    mod = sys.modules.get(CONTEXT_ANCHOR)
    local = getattr(mod, "context_local", None)
    if local is not None:
        local.context = None


def ask_needed(app_identity: str, conversation_id: str) -> bool:
    """Whether a card must be shown for *app_identity* in this conversation.

    False means no prompt: already granted, already denied this session (their
    'stop repeat prompts' rule), or the run-wide desktop grant is active.
    """
    if not app_identity or not conversation_id:
        return False
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        if conversation_id in st.run_grants:
            return False
        state = st.decisions.get(conversation_id, {}).get(key)
        if state in ("granted", "denied"):
            return False
        return key not in st.denied


def grant_app(app_identity: str, conversation_id: str, run_wide: bool = False) -> bool:
    """Record the user's Allow. Returns False when the identity/conversation is empty.

    run_wide=True mirrors 'allow_category_until_done': later apps in THIS conversation
    are allowed without another card, until revoke_all() or process exit.
    """
    if not app_identity or not conversation_id:
        logger.warning("[CUA-CONSENT] grant refused (empty identity/conversation)")
        return False
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        st.decisions.setdefault(conversation_id, {})[key] = "granted"
        st.denied.discard(key)
        if run_wide:
            st.run_grants.add(conversation_id)
    return True


def deny_app(app_identity: str, conversation_id: str) -> bool:
    """Record the user's Deny. Fail-closed: this identity stays blocked for the session."""
    if not app_identity:
        return False
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        st.decisions.setdefault(conversation_id or "", {})[key] = "denied"
        st.denied.add(key)
    return True


def is_granted(app_identity: str, conversation_id: str) -> bool:
    """True only when this conversation explicitly allowed this app. Default False."""
    if not app_identity or not conversation_id:
        return False
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
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        if key in st.denied:
            return True
        return st.decisions.get(conversation_id or "", {}).get(key) == "denied"


def granted_apps(conversation_id: str) -> list:
    """Identities this conversation may use - for the UI/permission page."""
    st = _state()
    with st.lock:
        return sorted(st.decisions.get(conversation_id, {}))


def decision_for(app_identity: str, conversation_id: str) -> str:
    """'granted' | 'denied' | '' (never asked) for one app in one conversation."""
    key = _norm_identity(app_identity)
    st = _state()
    with st.lock:
        return st.decisions.get(conversation_id, {}).get(key, "")


def revoke_app(app_identity: str, conversation_id: str = "") -> int:
    """Withdraw one app's access. Returns how many conversations lost it.

    Handles are invalidated by the caller (cua_revoke_access -> _desktop_state
    .invalidate_app) so a revoked app cannot be used by a later call.
    """
    key = _norm_identity(app_identity)
    removed = 0
    st = _state()
    with st.lock:
        for conv in list(st.decisions):
            if st.decisions[conv].pop(key, None) is not None:
                removed += 1
        st.denied.discard(key)
    logger.info("[CUA-CONSENT] access revoked for %s (%d conversations)", key, removed)
    return removed


def revoke_all() -> int:
    """Withdraw everything: per-app decisions, denied marks and run-wide grants."""
    st = _state()
    with st.lock:
        count = sum(len(v) for v in st.decisions.values())
        st.decisions.clear()
        st.run_grants.clear()
        st.denied.clear()
    logger.info("[CUA-CONSENT] all desktop access revoked (%d entries)", count)
    return count


def consent_summary() -> dict:
    """Compact state for cua_capabilities: how many grants/denials are live."""
    st = _state()
    with st.lock:
        return {"granted": sum(1 for v in st.decisions.values() for s in v.values() if s == "granted"),
                "denied": len(st.denied),
                "run_grants": len(st.run_grants)}


def app_identity_for_handle(app_handle: str) -> str:
    """Verified identity of a bound app handle ('' when the handle is unknown).

    Public on purpose: the CLIENT's desktop consent card resolves an existing app_handle
    to the SAME identity the user approved when the handle was bound, so the card never
    shows a different app than the one the grant covers. Reads the handle registry
    directly (this module owns the grant keys, so it is the right place to resolve them).
    """
    # Imported lazily: deps are exec'd in separate namespaces, so a module-level import
    # of a sibling dep would reference names that do not exist here.
    state = _state_module()
    if state is None:
        return ""
    record = state.get_app(app_handle)
    if not record:
        return ""
    return record.get("exe_path") or record.get("app_name") or ""


def _state_module():
    """The sibling _desktop_state dep module, or None.

    Two anchors, one per layout: the CLIENT exec's the dep as a bare same-package module and
    registers it as 'tools._desktop_state' (install_shared_deps), while the SERVER imports it
    as 'tools.computer_use._desktop_state'. Both must resolve, or consent_key_for_call would
    return '' on the SERVER and every desktop call would fail closed there.
    """
    import sys
    return (sys.modules.get("tools._desktop_state")
            or sys.modules.get("tools.computer_use._desktop_state"))


def consent_key_for_call(tool_name: str, args) -> str:
    """The grant key THIS desktop call will check - derived exactly like the tool does.

    One definition, two callers: the CLIENT's consent card grants under this key and the
    tool verifies against it, so a grant can never cover a different app than the one the
    user was shown. '' means "no identifiable app" -> the call fails closed.
    """
    args = args if isinstance(args, dict) else {}
    if tool_name == "cua_list_apps":
        return ENUMERATE_KEY  # window titles of every running app - its own key

    state = _state_module()
    if state is None:
        return ""

    snapshot_id = args.get("snapshot_id")
    if snapshot_id:
        return state.snapshot_consent_key(str(snapshot_id))
    window_id = args.get("window_id")
    if window_id:
        return state.window_consent_key(str(window_id))
    app_handle = args.get("app_handle")
    if app_handle:
        return state.app_consent_key(str(app_handle))
    selector = args.get("selector")
    if selector:
        return state.consent_identity(selector)
    return ""


def call_granted(tool_name: str, args, conversation_id: str) -> bool:
    """True when THIS conversation already allowed the app this call targets."""
    return is_granted(consent_key_for_call(tool_name, args), conversation_id)


def grant_call(tool_name: str, args, conversation_id: str, run_wide: bool = False) -> bool:
    """Record the user's Allow for the exact app this call targets."""
    return grant_app(consent_key_for_call(tool_name, args), conversation_id, run_wide=run_wide)


def deny_call(tool_name: str, args, conversation_id: str) -> bool:
    """Record the user's Deny for the exact app this call targets."""
    return deny_app(consent_key_for_call(tool_name, args), conversation_id)


def call_identity(tool_name: str, args) -> str:
    """Human-readable app name/path for the consent card ('' when unidentifiable)."""
    args = args if isinstance(args, dict) else {}
    if tool_name == "cua_list_apps":
        return "every running app (window titles)"
    state = _state_module()
    if state is None:
        return ""
    snapshot_id = args.get("snapshot_id")
    if snapshot_id:
        snap = state.get_snapshot(str(snapshot_id))
        if snap:
            return app_identity_for_handle(snap["app_handle"])
    window_id = args.get("window_id")
    if window_id:
        return state.window_identity(str(window_id))
    app_handle = args.get("app_handle")
    if app_handle:
        return app_identity_for_handle(str(app_handle))
    if args.get("selector"):
        return str(args.get("selector"))
    return ""
