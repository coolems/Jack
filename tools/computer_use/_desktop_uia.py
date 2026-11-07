"""UI Automation snapshot + pattern actions for computer_use (Windows).

The ported core of the harness's accessibility layer: instead of guessing pixels from a
screenshot, an app's control tree is OBSERVED (a snapshot with element indices) and
actions run through UIA patterns. Indices belong to ONE snapshot - _desktop_state
invalidates them when the window is re-observed.

Thread model: tools execute inside asyncio.to_thread workers, so a COM pointer created
in one call's thread must NEVER be reused in another call's thread (undefined
behaviour). A snapshot therefore stores each element's PATH from the window root
(child indices), and uia_resolve() re-walks that path in the CURRENT thread.

Verified against comtypes 1.4.6 + UIAutomationCore on this machine:
  * pattern ids: Invoke 10000, Value 10002, RangeValue 10003, Scroll 10004,
    ExpandCollapse 10005, SelectionItem 10010, Text 10014, Toggle 10015,
    LegacyIAccessible 10018  (Selection 10001 / Window 10009 / Transform 10016 are
    deliberately NOT used - WindowPattern.Close() and Transform move/resize are not
    safe operations)
  * GetCurrentPattern(id) returns POINTER(IUnknown): a missing pattern is a NULL
    pointer (falsy), and the real methods only appear after QueryInterface to the
    typed pattern interface
  * element properties are the Current* names (CurrentIsOffscreen, CurrentIsEnabled,
    CurrentName, CurrentControlType, CurrentBoundingRectangle)
  * GetClickablePoint() returns a (POINT, ok_flag) TUPLE, not an object with .x
  * text content comes from TextPattern.DocumentRange.GetText(n) - there is no
    GetTextRange() on this binding
  * tree walking uses RawViewWalker (FindAll raises COMError on some providers, e.g.
    Chrome's UIA provider, so the walker is the reliable path)

Only UIAutomationCore's COM object is used. Nothing here closes, minimizes, moves or
resizes a window, and no process is touched.
"""

import ctypes
import logging
import threading

import comtypes

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.UIA")

#: UIA control type ids -> readable names (what the AI sees in a snapshot).
CONTROL_TYPE_NAMES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "listitem",
    50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar",
    50015: "slider", 50016: "spinner", 50017: "statusbar", 50018: "tab",
    50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "datagrid", 50029: "dataitem", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar", 50038: "separator",
    50039: "semanticzoom", 50040: "appbar",
}

#: Pattern ids we support (verified against the installed UIAutomationCore binding).
PATTERN_INVOKE = 10000
PATTERN_VALUE = 10002
PATTERN_RANGE_VALUE = 10003
PATTERN_SCROLL = 10004
PATTERN_EXPAND = 10005
PATTERN_SELECTION_ITEM = 10010
PATTERN_TEXT = 10014
PATTERN_TOGGLE = 10015
PATTERN_LEGACY = 10018

#: pattern id -> (typed COM interface, advertised action name). The interface is what
#: QueryInterface needs - a raw pattern pointer has no methods.
PATTERN_INTERFACES = {
    PATTERN_INVOKE: "IUIAutomationInvokePattern",
    PATTERN_VALUE: "IUIAutomationValuePattern",
    PATTERN_RANGE_VALUE: "IUIAutomationRangeValuePattern",
    PATTERN_SCROLL: "IUIAutomationScrollPattern",
    PATTERN_EXPAND: "IUIAutomationExpandCollapsePattern",
    PATTERN_SELECTION_ITEM: "IUIAutomationSelectionItemPattern",
    PATTERN_TEXT: "IUIAutomationTextPattern",
    PATTERN_TOGGLE: "IUIAutomationTogglePattern",
    PATTERN_LEGACY: "IUIAutomationLegacyIAccessiblePattern",
}

#: Action names a snapshot advertises. These are the ONLY names uia_apply accepts, so
#: the model never invents one.
ACTION_BY_PATTERN = {
    PATTERN_INVOKE: "invoke",
    PATTERN_TOGGLE: "toggle",
    PATTERN_EXPAND: "expand",
    PATTERN_SELECTION_ITEM: "select",
    PATTERN_VALUE: "set_value",
    PATTERN_RANGE_VALUE: "set_range",
    PATTERN_SCROLL: "scroll",
    PATTERN_LEGACY: "default_action",
}

#: Caps on one observation. A bigger tree is truncated and reported honestly.
MAX_ELEMENTS = 1200
MAX_DEPTH = 25
MAX_NAME_CHARS = 160
MAX_TEXT_CHARS = 4000

_thread_local = threading.local()


def uia_instance():
    """This thread's (IUIAutomation, RawViewWalker, generated module) triple, or None.

    CoInitialize is per thread. A thread that already initialized COM raises
    RPC_E_CHANGED_MODE, which is harmless - the object below is still usable - so that
    error is tolerated rather than treated as a failure.
    """
    cached = getattr(_thread_local, "bundle", None)
    if cached is not None:
        return cached
    try:
        try:
            comtypes.CoInitialize()
        except Exception:
            pass  # already initialized on this thread
        from comtypes.client import CreateObject, GetModule
        GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        auto = CreateObject(U.CUIAutomation)
    except Exception as e:
        logger.warning("UI Automation unavailable on this thread: %s", e)
        return None
    bundle = (auto, auto.RawViewWalker, U)
    _thread_local.bundle = bundle
    return bundle


def uia_available() -> bool:
    """Whether UIA can be created in THIS thread (capability probe, no side effects)."""
    return uia_instance() is not None


def _pattern(element, pattern_id: int):
    """Typed UIA pattern for *element*, or None when it does not support it.

    GetCurrentPattern returns POINTER(IUnknown); a NULL pointer is falsy. The typed
    interface is obtained with QueryInterface - calling methods on the raw pointer
    raises AttributeError.
    """
    bundle = uia_instance()
    if bundle is None:
        return None
    _auto, _walker, U = bundle
    iface_name = PATTERN_INTERFACES.get(pattern_id)
    if iface_name is None:
        return None
    try:
        raw = element.GetCurrentPattern(pattern_id)
        if not raw:
            return None
        return raw.QueryInterface(getattr(U, iface_name))
    except Exception as e:
        logger.debug("pattern %s unavailable: %s", pattern_id, e)
        return None


def uia_element_from_handle(hwnd: int):
    """Root element of a window handle, or None."""
    bundle = uia_instance()
    if bundle is None or not hwnd:
        return None
    try:
        return bundle[0].ElementFromHandle(int(hwnd))
    except Exception as e:
        logger.debug("ElementFromHandle(%s) failed: %s", hwnd, e)
        return None


def uia_element_at_point(x: int, y: int):
    """Element under absolute screen pixels, or None."""
    bundle = uia_instance()
    if bundle is None:
        return None
    try:
        return bundle[0].ElementFromPoint(ctypes.wintypes.POINT(int(x), int(y)))
    except Exception as e:
        logger.debug("ElementFromPoint(%s,%s) failed: %s", x, y, e)
        return None


def uia_focused_element():
    """The element currently holding keyboard focus, or None."""
    bundle = uia_instance()
    if bundle is None:
        return None
    try:
        return bundle[0].GetFocusedElement()
    except Exception as e:
        logger.debug("GetFocusedElement failed: %s", e)
        return None


def uia_focused_pid():
    """pid of the element holding keyboard focus, or None when UIA cannot read it.

    PUBLIC on purpose: install_shared_deps() merges only non-underscore dep symbols, so a
    private helper in a sibling TOOL module would be invisible to the delivered tool
    (the CLIENT neutralises 'from .cua_type_text import _focused_pid' to `pass`). The
    focus guard therefore lives in this dep, where every input tool can reach it.
    """
    element = uia_focused_element()
    if element is None:
        return None
    try:
        return int(element.CurrentProcessId)
    except Exception:
        return None


def _children(element):
    """Direct children of an element (RawViewWalker), [] when the walk fails."""
    bundle = uia_instance()
    if bundle is None:
        return []
    walker = bundle[1]
    out = []
    try:
        child = walker.GetFirstChildElement(element)
        while child:
            out.append(child)
            child = walker.GetNextSiblingElement(child)
    except Exception as e:
        logger.debug("child walk stopped: %s", e)
    return out


def _rect_of(element) -> dict:
    """Bounding rectangle in absolute screen pixels ({} when unreadable)."""
    try:
        r = element.CurrentBoundingRectangle
        left, top, right, bottom = int(r.left), int(r.top), int(r.right), int(r.bottom)
        return {"left": left, "top": top, "right": right, "bottom": bottom,
                "width": max(0, right - left), "height": max(0, bottom - top)}
    except Exception:
        return {}


def _patterns_of(element) -> list:
    """Pattern ids this element actually supports (verified per id, no guessing)."""
    found = []
    for pid in ACTION_BY_PATTERN:
        try:
            if element.GetCurrentPattern(pid):
                found.append(pid)
        except Exception:
            continue
    try:
        if element.GetCurrentPattern(PATTERN_TEXT):
            found.append(PATTERN_TEXT)
    except Exception:
        pass
    return found


def _clickable_point(element):
    """Point UIA says is clickable, as absolute pixels, or None.

    This binding returns a (POINT, ok_flag) tuple - a failed call yields ok=0.
    """
    try:
        point, ok = element.GetClickablePoint()
        if not ok:
            return None
        return [int(point.x), int(point.y)]
    except Exception:
        return None


def _toggle_state(element):
    """Toggle pattern state: 'on' | 'off' | 'mixed' | None (no Toggle pattern)."""
    pattern = _pattern(element, PATTERN_TOGGLE)
    if pattern is None:
        return None
    try:
        state = int(pattern.CurrentToggleState)
    except Exception:
        return None
    return {1: "on", 2: "off", 0: "mixed"}.get(state)


def uia_scroll_percent(element):
    """Scroll pattern position as [horizontal, vertical] percent, or None.

    PUBLIC: snapshot rows carry it, so a scroll can be VERIFIED from a fresh observation
    instead of trusting the dispatched wheel event. Read-only - it never scrolls.
    """
    pattern = _pattern(element, PATTERN_SCROLL)
    if pattern is None:
        return None
    try:
        return [round(float(pattern.CurrentHorizontalScrollPercent), 2),
                round(float(pattern.CurrentVerticalScrollPercent), 2)]
    except Exception:
        return None


def _describe(element, index: int, depth: int, path: list) -> dict:
    """One snapshot row: what the element is, where it is, what can be done to it.

    Also carries the element's CURRENT value / toggle state when it has those patterns, so a
    caller can VERIFY an action from a fresh observation instead of trusting the dispatched
    result (a delivered click is not proof - see the tool docstrings).
    """
    patterns = _patterns_of(element)
    actions = sorted({ACTION_BY_PATTERN[p] for p in patterns if p in ACTION_BY_PATTERN})
    try:
        name = (element.CurrentName or "")[:MAX_NAME_CHARS]
    except Exception:
        name = ""
    try:
        ctype = CONTROL_TYPE_NAMES.get(int(element.CurrentControlType), "unknown")
    except Exception:
        ctype = "unknown"
    try:
        offscreen = bool(element.CurrentIsOffscreen)
    except Exception:
        offscreen = False
    try:
        enabled = bool(element.CurrentIsEnabled)
    except Exception:
        enabled = True
    try:
        focusable = bool(element.CurrentIsKeyboardFocusable)
    except Exception:
        focusable = False
    row = {"index": int(index), "name": name, "control_type": ctype,
           "rect": _rect_of(element), "depth": int(depth), "path": list(path),
           "actions": actions, "patterns": [p for p in patterns if p in ACTION_BY_PATTERN],
           "text_pattern": PATTERN_TEXT in patterns,
           "offscreen": bool(offscreen), "enabled": bool(enabled),
           "keyboard_focusable": bool(focusable)}
    point = _clickable_point(element)
    if point:
        row["clickable_point"] = point
    if PATTERN_VALUE in patterns or PATTERN_RANGE_VALUE in patterns:
        row["value"] = uia_value(element)
    if PATTERN_TOGGLE in patterns:
        row["toggle_state"] = _toggle_state(element)
    if PATTERN_SCROLL in patterns:
        row["scroll_percent"] = uia_scroll_percent(element)
    return row


def uia_snapshot(hwnd: int, max_elements: int = MAX_ELEMENTS, max_depth: int = MAX_DEPTH):
    """Walk a window's control tree. Returns (elements, truncated, error).

    Budget-bounded (element count + depth) and reported honestly: hitting the budget
    sets truncated=True so a caller never mistakes a partial tree for the whole window.
    Off-screen elements are kept but flagged - they exist, they just cannot be used yet.
    """
    root = uia_element_from_handle(hwnd)
    if root is None:
        return [], False, "UIA_UNAVAILABLE: no control tree for this window handle"

    limit = min(int(max_elements), MAX_ELEMENTS)
    depth_limit = min(int(max_depth), MAX_DEPTH)
    elements: list = []
    truncated = False

    def visit(element, path, depth):
        nonlocal truncated
        if len(elements) >= limit:
            truncated = True
            return
        if depth > depth_limit:
            truncated = True
            return
        elements.append(_describe(element, len(elements), depth, path))
        for i, child in enumerate(_children(element)):
            visit(child, path + [i], depth + 1)

    visit(root, [], 0)
    return elements, truncated, None


def uia_resolve(hwnd: int, path: list):
    """Re-resolve a snapshot element's path in the CURRENT thread, or None.

    Snapshot rows carry the child-index path recorded during the walk, so a later call
    on a different worker thread can obtain a live element instead of reusing a COM
    pointer from another thread.
    """
    element = uia_element_from_handle(hwnd)
    if element is None:
        return None
    for idx in path:
        children = _children(element)
        if idx >= len(children):
            return None
        element = children[idx]
    return element


def uia_element_info(x: int, y: int):
    """Describe the element at absolute screen pixels ({} when there is none).

    Used after an input action: a delivered click is not proof, and this says what UIA
    now considers to be at that pixel.
    """
    element = uia_element_at_point(x, y)
    if element is None:
        return {}
    return _describe(element, 0, 0, [])


def uia_read_text(element) -> str:
    """Text content through the Text pattern ('' when the element has none)."""
    pattern = _pattern(element, PATTERN_TEXT)
    if pattern is None:
        return ""
    try:
        return pattern.DocumentRange.GetText(MAX_TEXT_CHARS) or ""
    except Exception as e:
        logger.debug("text read failed: %s", e)
        return ""


def uia_value(element):
    """Current Value/RangeValue pattern value, or None."""
    pattern = _pattern(element, PATTERN_VALUE)
    if pattern is not None:
        try:
            return pattern.CurrentValue
        except Exception:
            return None
    pattern = _pattern(element, PATTERN_RANGE_VALUE)
    if pattern is not None:
        try:
            return pattern.CurrentValue
        except Exception:
            return None
    return None


def uia_supported_actions(element) -> list:
    """Action names an element advertises - the only names uia_apply accepts."""
    return sorted({ACTION_BY_PATTERN[p] for p in _patterns_of(element)
                   if p in ACTION_BY_PATTERN})


def uia_select_text(element, prefix=None, suffix=None, selection_type: str = "text"):
    """Select (or place a caret) inside an element's Text pattern. Returns (ok, error).

    Verified against the installed UIAutomationCore binding:
      * TextPattern.DocumentRange.Clone() -> IUIAutomationTextRange
      * range.FindText(text, ignore_case, max_length) -> range (falsy when not found)
      * range.MoveEndpointByRange(endpoint, other_range, other_endpoint) - THREE args
      * range.Select() then GetSelection().GetElement(0) - the array is not subscriptable
      * a collapsed range selects to an EMPTY string, which is exactly a caret

    Anchors, in order: *prefix* sets the range start to the prefix's start, then
    *suffix* (searched inside what remains) sets the range end to the suffix's end.
    selection_type then decides the shape:
      text          -> the anchored range is selected
      cursor_before -> collapsed to the range start (caret before)
      cursor_after  -> collapsed to the range end (caret after)
    Without anchors the whole document range is used. Nothing is typed - this is a UIA
    selection, so no keystrokes are ever sent.
    """
    bundle = uia_instance()
    if bundle is None:
        return False, "UIA_UNAVAILABLE: UI Automation is not available on this thread."
    _auto, _walker, U = bundle
    pattern = _pattern(element, PATTERN_TEXT)
    if pattern is None:
        return False, "NO_TEXT_PATTERN: this element has no Text pattern (use cua_type_text)."

    start, end = U.TextPatternRangeEndpoint_Start, U.TextPatternRangeEndpoint_End
    try:
        doc = pattern.DocumentRange
        rng = doc.Clone()

        if prefix:
            found = rng.FindText(str(prefix), True, 0)
            if not found:
                return False, f"PREFIX_NOT_FOUND: {prefix!r} is not in this element's text."
            rng.MoveEndpointByRange(start, found, start)

        if suffix:
            found = rng.FindText(str(suffix), True, 0)
            if not found:
                return False, f"SUFFIX_NOT_FOUND: {suffix!r} is not in this element's text (after the prefix)."
            rng.MoveEndpointByRange(end, found, end)

        if selection_type == "cursor_before":
            rng.MoveEndpointByRange(end, doc, start)
        elif selection_type == "cursor_after":
            rng.MoveEndpointByRange(start, doc, end)

        rng.Select()
        rng.ScrollIntoView(0)
        return True, None
    except Exception as e:
        return False, f"SELECT_FAILED: {e}"


def uia_apply(element, action: str, value=None):
    """Perform one pattern action. Returns (ok, error) - never raises.

    Every branch is guarded: a pattern that refuses raises inside COM, and the caller
    gets a result dict instead of an exception. set_value honours the element's own
    IsReadOnly flag instead of forcing a write.
    """
    try:
        if action == "invoke":
            pattern = _pattern(element, PATTERN_INVOKE)
            if pattern is None:
                return False, "NO_INVOKE_PATTERN: this element does not advertise 'invoke'"
            pattern.Invoke()
            return True, None

        if action == "toggle":
            pattern = _pattern(element, PATTERN_TOGGLE)
            if pattern is None:
                return False, "NO_TOGGLE_PATTERN: this element does not advertise 'toggle'"
            pattern.Toggle()
            return True, None

        if action in ("expand", "collapse"):
            pattern = _pattern(element, PATTERN_EXPAND)
            if pattern is None:
                return False, "NO_EXPAND_PATTERN: this element does not advertise 'expand'"
            pattern.Expand() if action == "expand" else pattern.Collapse()
            return True, None

        if action == "select":
            pattern = _pattern(element, PATTERN_SELECTION_ITEM)
            if pattern is None:
                return False, "NO_SELECT_PATTERN: this element does not advertise 'select'"
            pattern.Select()
            return True, None

        if action == "set_value":
            if value is None:
                return False, "NO_VALUE: set_value needs a value"
            pattern = _pattern(element, PATTERN_VALUE)
            if pattern is None:
                return False, "NO_VALUE_PATTERN: this element does not advertise 'set_value'"
            try:
                if pattern.CurrentIsReadOnly:
                    return False, ("READ_ONLY: this element refuses value writes. Use "
                                   "cua_type_text, or an element whose snapshot lists 'set_value'.")
            except Exception:
                pass
            pattern.SetValue(str(value))
            return True, None

        if action == "set_range":
            if value is None:
                return False, "NO_VALUE: set_range needs a number"
            pattern = _pattern(element, PATTERN_RANGE_VALUE)
            if pattern is None:
                return False, "NO_RANGE_PATTERN: this element does not advertise 'set_range'"
            pattern.SetValue(float(value))
            return True, None

        if action == "scroll":
            pattern = _pattern(element, PATTERN_SCROLL)
            if pattern is None:
                return False, "NO_SCROLL_PATTERN: this element does not advertise 'scroll'"
            hx, hy = (value if isinstance(value, (list, tuple)) else (0, 1))
            pattern.Scroll(int(hx), int(hy))
            return True, None

        if action == "default_action":
            pattern = _pattern(element, PATTERN_LEGACY)
            if pattern is None:
                return False, "NO_LEGACY_PATTERN: this element has no default action"
            pattern.DoDefaultAction()
            return True, None

        return False, (f"UNKNOWN_ACTION: {action}. Use one of the action names listed in "
                       "this element's snapshot 'actions' field.")
    except Exception as e:
        return False, f"PATTERN_FAILED: {action} raised: {e}"
