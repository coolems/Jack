"""Handle, snapshot and coordinate state for computer_use.

Self-contained on purpose: this module is delivered to the CLIENT as a same-package
shared_dep, and loader.globals_builder.install_shared_deps() execs each dep in its OWN
globals namespace -- a dep that imported another dep would reference symbols that are not
there. So nothing here imports a sibling helper module; the TOOL files combine them.

Three registries, all process-scoped and thread-safe (tools run in asyncio.to_thread
workers, so concurrent chats must not corrupt each other's state):

  apps     app_handle  -> {pid, exe_path, app_name, ...}
  windows  window_id   -> {hwnd, app_handle, rect}      rect = rect seen at registration
  snapshots snapshot_id -> {app_handle, window_id, elements, rect, ...}

Coordinate rules ported from the harness (the reason pixel clicking was unreliable):
  * absolute pixels are only valid against the capture/snapshot that produced them;
    if the window moved or resized since then the call is rejected with WINDOW_CHANGED
  * relative coordinates are thousandths of the window (0..999), scaled and clamped
  * nothing is accepted without a recorded observation -> SCREENSHOT_REQUIRED
"""

import logging
import os
import re
import threading
import time
import uuid

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.State")

#: Hard ceilings on stored state. A snapshot bigger than this is truncated and reported.
MAX_SNAPSHOT_ELEMENTS = 1200
MAX_SNAPSHOT_DEPTH = 25
MAX_TRACKED_HANDLES = 64

#: Sentinel rect Windows reports for minimized windows.
MINIMIZED_LEFT = -32000

#: Anchor holding the registries (2026-11-07 fix).
#:
#: WHY THE REGISTRIES ARE NOT MODULE-LOCAL: loader.globals_builder.install_shared_deps()
#: execs each dep in its own namespace and then REPLACES sys.modules['tools.<dep>'] with a
#: NEW module object, once per tool compile. With module-local dicts, cua_get_app would
#: register an app handle in the object it was compiled with and cua_get_state - compiled
#: later, from a fresh dep object - would answer APP_REQUIRED for a handle that was valid a
#: second ago. Handles, windows and snapshots must therefore live on ONE process-wide
#: anchor, exactly like the consent decisions in _consent.py.
#: Named REGISTRY_ANCHOR (not STATE_ANCHOR) so the merged tool globals never collide with
#: _consent.STATE_ANCHOR - install_shared_deps merges every PUBLIC dep symbol into every
#: tool's exec globals, and a shared public name would let one dep overwrite the other's.
REGISTRY_ANCHOR = "_coolems_desktop_registry"


def _state():
    """The process-wide handle/snapshot registries (created once, shared by every dep copy)."""
    import sys
    import types
    holder = sys.modules.get(REGISTRY_ANCHOR)
    if holder is None:
        holder = types.ModuleType(REGISTRY_ANCHOR)
        holder.apps = {}       # app_handle -> record
        holder.windows = {}    # window_id -> record
        holder.snapshots = {}  # snapshot_id -> record
        holder.lock = threading.Lock()
        sys.modules[REGISTRY_ANCHOR] = holder
    return holder


def new_handle_token() -> str:
    """Opaque handle id. Never derived from a pid/hwnd so it cannot be guessed."""
    return uuid.uuid4().hex[:10]


def register_app(app_handle: str, pid: int, exe_path: str, app_name: str,
                 consent_key: str = "") -> dict:
    """Store an app handle. Returns the stored record.

    *consent_key* is the identity the LOCAL USER approved on the desktop consent card
    (the selector they were shown). Every later app-bound call checks the grant against
    THAT key, so a handle can never outgrow the consent that created it. exe_path is
    still stored as verified identity for display and for path/pid selectors.
    """
    st = _state()
    with st.lock:
        if len(st.apps) >= MAX_TRACKED_HANDLES and app_handle not in st.apps:
            _evict_oldest(st.apps)
        record = {"pid": int(pid), "exe_path": str(exe_path or ""),
                  "app_name": str(app_name or ""),
                  "consent_key": str(consent_key or ""), "created_at": time.time()}
        st.apps[app_handle] = record
        return record


def get_app(app_handle: str):
    """Return the app record for *app_handle*, or None."""
    st = _state()
    with st.lock:
        return st.apps.get(app_handle)


def app_identity(app_handle: str) -> str:
    """Stable consent-grant key for an app: its executable path, else its name."""
    record = get_app(app_handle)
    if not record:
        return ""
    return record["exe_path"] or record["app_name"]


def consent_identity(selector) -> str:
    """Canonical consent key for an app selector (name / path / pid / bundle id).

    Both sides - the CLIENT's desktop consent card and the tool that binds the handle -
    derive the SAME key from the SAME selector, so a grant can only cover the app the
    user was actually shown. Paths are normcase'd (Windows is case-insensitive); a pid
    is keyed as 'pid:<n>' because a bare number could collide with a name.
    """
    if isinstance(selector, dict):
        path = selector.get("path")
        if path:
            return os.path.normcase(str(path).strip())
        pid = selector.get("pid")
        if pid is not None:
            return f"pid:{int(pid)}"
        bundle = selector.get("bundle_id") or selector.get("bundleId")
        if bundle:
            return str(bundle).strip().lower()
        name = selector.get("name") or selector.get("title")
        if name:
            return str(name).strip().lower()
        return ""
    if isinstance(selector, (int, float)):
        return f"pid:{int(selector)}"
    if isinstance(selector, str):
        text = selector.strip()
        if not text:
            return ""
        if text.isdigit():
            return f"pid:{int(text)}"
        # A path-looking selector is keyed as a path; anything else as a name.
        if "\\" in text or "/" in text or re.match(r"^[A-Za-z]:", text):
            return os.path.normcase(text)
        return text.lower()
    return ""


def app_consent_key(app_handle: str) -> str:
    """The consent key an app handle was granted under ('' when unknown)."""
    record = get_app(app_handle)
    if not record:
        return ""
    return record.get("consent_key") or record["exe_path"] or record["app_name"]


def window_consent_key(window_id: str) -> str:
    """Consent key of the app owning a window ('' when the window is unknown)."""
    window = get_window(window_id)
    if not window:
        return ""
    return app_consent_key(window["app_handle"])


def windows_for_app(app_handle: str) -> list:
    """(window_id, record) pairs currently registered for one app.

    Public on purpose: tool files may only use non-underscore dep symbols (dep exec
    globals merge public names only), so registry reads go through this helper instead
    of touching the private dict.
    """
    st = _state()
    with st.lock:
        return [(wid, dict(rec)) for wid, rec in st.windows.items()
                if rec.get("app_handle") == app_handle]


def snapshot_consent_key(snapshot_id: str) -> str:
    """Consent key of the app a snapshot was taken from ('' when unknown)."""
    snap = get_snapshot(snapshot_id)
    if not snap:
        return ""
    return app_consent_key(snap["app_handle"])


def register_window(window_id: str, hwnd: int, app_handle: str, rect: dict,
                    title: str = "") -> dict:
    """Store a window handle together with the rect observed at registration.

    *title* is kept so cua_list_windows can report the same window again without a
    second enumeration, and so a window that lost its readable rect still identifies
    itself instead of returning an empty row.
    """
    st = _state()
    with st.lock:
        if len(st.windows) >= MAX_TRACKED_HANDLES and window_id not in st.windows:
            _evict_oldest(st.windows)
        record = {"hwnd": int(hwnd), "app_handle": app_handle,
                  "rect": dict(rect or {}), "title": str(title or ""),
                  "created_at": time.time()}
        st.windows[window_id] = record
        return record


def get_window(window_id: str):
    """Return the window record for *window_id*, or None."""
    st = _state()
    with st.lock:
        return st.windows.get(window_id)


def register_snapshot(snapshot_id: str, app_handle: str, window_id: str,
                      elements: list, rect: dict, truncated: bool, hwnd: int = 0) -> dict:
    """Store an observation. Any earlier snapshot of the SAME scope is invalidated.

    Element indices are only meaningful for the snapshot they came from, so keeping two
    live snapshots of one window would let a stale index address a different tree.

    *hwnd* is kept so a later call can re-resolve an element's recorded path in its own
    thread (COM pointers are thread-local and must never cross threads).
    """
    st = _state()
    with st.lock:
        if len(st.snapshots) >= MAX_TRACKED_HANDLES and snapshot_id not in st.snapshots:
            _evict_oldest(st.snapshots)
        for sid, snap in list(st.snapshots.items()):
            if sid != snapshot_id and snap["app_handle"] == app_handle and snap["window_id"] == window_id:
                del st.snapshots[sid]
        st.snapshots[snapshot_id] = {"app_handle": app_handle, "window_id": window_id,
                                     "elements": elements, "rect": dict(rect or {}),
                                     "truncated": bool(truncated), "hwnd": int(hwnd or 0),
                                     "created_at": time.time()}
        return st.snapshots[snapshot_id]


def get_snapshot(snapshot_id: str):
    """Return a live snapshot, or None (unknown or superseded by a newer observation)."""
    st = _state()
    with st.lock:
        return st.snapshots.get(snapshot_id)


def snapshot_element(snapshot_id: str, element_index: int):
    """Resolve an element index against a live snapshot.

    Returns (element, error). error is a string when the snapshot is gone or the index
    is out of range - never an exception, so tools report it as a result.
    """
    snap = get_snapshot(snapshot_id)
    if snap is None:
        return None, (f"SNAPSHOT_INVALIDATED: snapshot {snapshot_id} is gone or was replaced "
                      "by a newer observation of that window. Re-run cua_get_state and use the "
                      "indices from the NEW snapshotId.")
    if not isinstance(element_index, int) or element_index < 0 or element_index >= len(snap["elements"]):
        return None, (f"INDEX_OUT_OF_RANGE: {element_index} is not in snapshot {snapshot_id} "
                      f"(0-{len(snap['elements']) - 1}). Use indices from THIS snapshotId only.")
    return snap["elements"][element_index], None


def action_error(error: str) -> dict:
    """One refusal shape for every action tool: nothing was dispatched, nothing verified.

    PUBLIC on purpose (dep symbols merge into the tool's exec globals). An action tool must
    never return a bare {'status': 'error'} - a caller reading a partial result could mistake
    a refusal for 'something was sent'. Every refusal therefore carries both keys as False.
    """
    return {"status": "error", "error": error, "dispatched": False, "verified": False}


def target_consent_key(snapshot_id: str = None, window_id: str = None) -> tuple:
    """Consent key of an action target, plus the refusal when it cannot be identified.

    Returns (key, error). One definition for every input tool (click/hover/drag/scroll), so
    a stale snapshotId and an unregistered windowId get their ACTUAL reason instead of a
    generic 'not granted':

      * snapshot_id given but gone/superseded  -> SNAPSHOT_INVALIDATED
      * window_id given but not registered     -> WINDOW_REQUIRED
      * neither given                          -> NO_TARGET

    Fail-closed either way: an unidentifiable target has no grant key, so no action can run.
    Resolving the identity is a registry read, not an action - the grant is still verified
    against that key before anything is dispatched.
    """
    if snapshot_id is not None:
        key = snapshot_consent_key(str(snapshot_id))
        if not key:
            return "", (f"SNAPSHOT_INVALIDATED: snapshot {snapshot_id} is gone or was replaced by a "
                        "newer observation of that window. Re-run cua_get_state and use the NEW "
                        "snapshotId.")
        return key, None
    if window_id is not None:
        key = window_consent_key(str(window_id))
        if not key:
            return "", (f"WINDOW_REQUIRED: window {window_id} is not registered. "
                        "Call cua_get_app or cua_list_windows first.")
        return key, None
    return "", "NO_TARGET: give snapshotId + elementIndex, or windowId with x,y / relative."


def invalidate_app(app_handle: str) -> int:
    """Drop every handle, window and snapshot belonging to one app.

    Used by access revocation: after a user revokes an app, no handle may survive, so a
    later call cannot act on it. Returns how many entries were removed.
    """
    st = _state()
    with st.lock:
        removed = 0
        st.apps.pop(app_handle, None)
        for wid, rec in list(st.windows.items()):
            if rec["app_handle"] == app_handle:
                del st.windows[wid]
                removed += 1
        for sid, snap in list(st.snapshots.items()):
            if snap["app_handle"] == app_handle:
                del st.snapshots[sid]
                removed += 1
        return removed


def reset_all_state() -> None:
    """Clear every registry (used by tests and by a full access reset)."""
    st = _state()
    with st.lock:
        st.apps.clear()
        st.windows.clear()
        st.snapshots.clear()


def _evict_oldest(store: dict) -> None:
    """Drop the oldest entry from *store*. Caller already holds the lock."""
    if not store:
        return
    oldest = min(store, key=lambda k: store[k].get("created_at", 0))
    del store[oldest]


def rect_is_minimized(rect: dict) -> bool:
    """True when *rect* is the off-screen minimized sentinel or has zero size."""
    if not rect:
        return True
    if rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
        return True
    return rect.get("left", 0) <= MINIMIZED_LEFT


def validate_coordinate_target(window_id: str, current_rect: dict, x=None, y=None,
                               relative=None) -> tuple:
    """Turn a caller's coordinate claim into absolute screen pixels.

    Exactly one of (x, y) or relative is accepted. Returns (absolute_xy, error):
      * absolute pixels are measured from the top-left of the window, and are only
        honoured when the window rect is unchanged since the observation that produced
        them (otherwise WINDOW_CHANGED)
      * relative values are thousandths of the window width/height (0..999), scaled and
        clamped inside the window
      * no recorded window, or no live rect, is SCREENSHOT_REQUIRED / WINDOW_CHANGED
    """
    window = get_window(window_id)
    if window is None:
        return None, (f"WINDOW_REQUIRED: window {window_id} is not registered. "
                      "Call cua_list_windows first and use its windowId.")
    if current_rect is None:
        return None, (f"WINDOW_CHANGED: window {window_id} no longer has a readable rectangle "
                      "(closed or destroyed). Re-run cua_list_windows.")
    if rect_is_minimized(current_rect):
        return None, (f"WINDOW_MINIMIZED: window {window_id} is minimized, so its pixels are "
                      "not addressable. Use cua_show_window, or target an element index instead.")

    stored = window["rect"]
    has_relative = relative is not None
    has_absolute = x is not None and y is not None
    if has_relative and has_absolute:
        return None, "AMBIGUOUS_TARGET: give either x,y or relative, not both."
    if not has_relative and not has_absolute:
        return None, "NO_COORDINATES: supply x,y (window pixels) or relative [0..999]."

    if has_absolute:
        if not isinstance(x, int) or not isinstance(y, int):
            return None, "INVALID_COORDINATES: x and y must be integers."
        if stored and (stored.get("left") != current_rect["left"]
                       or stored.get("top") != current_rect["top"]
                       or stored.get("width") != current_rect["width"]
                       or stored.get("height") != current_rect["height"]):
            return None, (f"WINDOW_CHANGED: window {window_id} moved or resized since the "
                          f"screenshot that produced these coordinates "
                          f"(was {stored.get('left')},{stored.get('top')} "
                          f"{stored.get('width')}x{stored.get('height')}, now "
                          f"{current_rect['left']},{current_rect['top']} "
                          f"{current_rect['width']}x{current_rect['height']}). "
                          "Take a fresh cua_screenshot and recompute.")
        if x < 0 or y < 0 or x >= current_rect["width"] or y >= current_rect["height"]:
            return None, (f"COORDINATES_OUT_OF_WINDOW: ({x},{y}) is outside window {window_id} "
                          f"(0-{current_rect['width'] - 1}, 0-{current_rect['height'] - 1}).")
        return (current_rect["left"] + x, current_rect["top"] + y), None

    # relative mode: thousandths of the window
    if not isinstance(relative, (list, tuple)) or len(relative) != 2:
        return None, "INVALID_RELATIVE: relative must be [rx, ry] with integers 0..999."
    rx, ry = relative
    if not isinstance(rx, int) or not isinstance(ry, int):
        return None, "INVALID_RELATIVE: relative values must be integers."
    if not (0 <= rx <= 999 and 0 <= ry <= 999):
        return None, f"RELATIVE_OUT_OF_RANGE: {rx},{ry} - each value must be 0..999 (thousandths)."
    ax = current_rect["left"] + int(rx * current_rect["width"] / 1000)
    ay = current_rect["top"] + int(ry * current_rect["height"] / 1000)
    # Clamp inside the window so a 999 request never lands on the next window's pixel.
    ax = min(ax, current_rect["right"] - 1)
    ay = min(ay, current_rect["bottom"] - 1)
    return (ax, ay), None


def element_center(rect: dict):
    """Center of an element rect as absolute screen pixels, or None for an empty rect."""
    if not rect or rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
        return None
    return (rect["left"] + rect["width"] // 2, rect["top"] + rect["height"] // 2)


def resolve_element_target(snapshot_id: str, element_index: int):
    """Resolve an element target to absolute screen pixels. Returns (point, error).

    Uses UIA's own clickable point when the snapshot recorded one, otherwise the
    element's centre. An off-screen element is refused rather than clicked at a stale
    rectangle, and an element with neither is pointed at cua_perform_action instead.
    """
    element, err = snapshot_element(snapshot_id, element_index)
    if err:
        return None, err
    if element.get("offscreen"):
        return None, (f"ELEMENT_OFFSCREEN: index {element_index} ({element.get('name')!r}) is not "
                      "visible yet. Scroll or show the window, then observe again.")
    point = element.get("clickable_point")
    if point:
        return (int(point[0]), int(point[1])), None
    centre = element_center(element.get("rect") or {})
    if centre is None:
        return None, (f"ELEMENT_UNADDRESSABLE: index {element_index} has no clickable point or "
                      "usable rectangle. Use cua_perform_action with one of its listed actions.")
    return centre, None


def resolve_window_target(window_id: str, current_rect: dict, x=None, y=None, relative=None):
    """Resolve a coordinate target to absolute screen pixels. Returns (point, error).

    Thin wrapper over validate_coordinate_target so every input tool applies the same
    rules (WINDOW_REQUIRED / WINDOW_CHANGED / WINDOW_MINIMIZED / out-of-range).
    """
    return validate_coordinate_target(window_id, current_rect, x=x, y=y, relative=relative)


def window_identity(window_id: str) -> str:
    """Identity of the app owning a window ('' when the window is unknown).

    Public for the CLIENT's desktop consent card: it resolves a windowId to the app the
    user approved, so the card names the real target instead of an opaque handle.
    """
    window = get_window(window_id)
    if window is None:
        return ""
    record = get_app(window["app_handle"])
    if not record:
        return ""
    return record.get("exe_path") or record.get("app_name") or ""


def window_identity_summary() -> dict:
    """Counts of live handles - used by cua_capabilities so the AI can see what is open."""
    st = _state()
    with st.lock:
        return {"apps": len(st.apps), "windows": len(st.windows), "snapshots": len(st.snapshots)}
