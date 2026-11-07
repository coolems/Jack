"""cua_select_text - select a range or place a caret inside a UIA text element.

Ported from selectText. Anchors are literal strings (prefix/suffix), resolved through
the Text pattern in the CURRENT thread, so a stale snapshot index is refused instead of
acting on a different control. selection_type picks the shape:

  text          -> the anchored range is selected
  cursor_before -> collapsed caret at the range start
  cursor_after  -> collapsed caret at the range end

No keystrokes are sent - this is a UIA selection, so it cannot type, delete or submit.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_select_text",
        "description": "Select text in a snapshot element by prefix/suffix anchors, or place a caret (selectionType cursor_before/cursor_after). No keystrokes are sent. dispatched=true, verified=false.",
        "parameters": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string", "description": "snapshotId from cua_get_state."},
                "element_index": {"type": "integer", "description": "Element index in that snapshotId."},
                "prefix": {"type": "string", "description": "Anchor that starts the selection."},
                "suffix": {"type": "string", "description": "Anchor that ends the selection."},
                "selection_type": {"type": "string", "enum": ["text", "cursor_before", "cursor_after"],
                                   "description": "Selection shape (default text)."}
            },
            "required": ["snapshot_id", "element_index"]
        }
    }
}

import logging

from ._desktop_win import desktop_available
from ._desktop_state import action_error, app_consent_key, get_snapshot, snapshot_element
from ._desktop_uia import uia_read_text, uia_resolve, uia_select_text
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.SelectText")


def cua_select_text(snapshot_id: str, element_index: int, prefix: str = None,
                    suffix: str = None, selection_type: str = "text") -> dict:
    """Select a text range or place a caret in one snapshot element."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    snap = get_snapshot(snapshot_id)
    if snap is None:
        return action_error(f"SNAPSHOT_INVALIDATED: {snapshot_id} is gone or was replaced. "
                            "Re-run cua_get_state and use the NEW snapshotId.")
    if not is_granted(app_consent_key(snap["app_handle"]), current_conversation_id()):
        return action_error("DESKTOP_CONSENT_REQUIRED: access to this app is not granted.")
    if selection_type not in ("text", "cursor_before", "cursor_after"):
        return action_error(f"INVALID_SELECTION_TYPE: {selection_type!r}. Use text, cursor_before or cursor_after.")

    element, err = snapshot_element(snapshot_id, element_index)
    if err:
        return action_error(err)
    if not element.get("text_pattern"):
        return action_error(f"NO_TEXT_PATTERN: index {element_index} ({element.get('name')!r}) has no Text "
                            "pattern. Use cua_type_text or cua_perform_action set_value instead.")

    live = uia_resolve(snap["hwnd"], element["path"])
    if live is None:
        return action_error(f"ELEMENT_CHANGED: index {element_index} no longer resolves (the tree "
                            "changed). Re-run cua_get_state.")

    ok, why = uia_select_text(live, prefix, suffix, selection_type)
    if not ok:
        return action_error(why)

    logger.info("[CUA] select on index %d type=%s -> %s", element_index, selection_type, ok)
    return {"status": "ok", "dispatched": True, "verified": False,
            "element": {"index": element_index, "name": element.get("name")},
            "selection_type": selection_type, "prefix": prefix, "suffix": suffix,
            "text_now": uia_read_text(live)[:200],
            "next_step": "Re-observe (cua_get_state with include_text) - the selection is not proof of focus."}
