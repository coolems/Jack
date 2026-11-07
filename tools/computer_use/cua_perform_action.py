"""cua_perform_action - run a UIA pattern action on a snapshot element.

The action name must be one the element ADVERTISED in the snapshot's 'actions' field
(invoke / toggle / expand / select / set_value / set_range / scroll / default_action).
The tool never invents an action and never guesses a pattern: an element that does not
support the name is refused with NO_*_PATTERN.

Deliberately absent: WindowPattern.Close and TransformPattern move/resize. A tool call
here cannot close or relocate a window - that is not a safe operation.

Live-verified caveat: Chrome's HTML <select> advertises ValuePattern but IGNORES
set_value - the value never changes. Switch a dropdown by expanding it and invoking or
selecting the option element (or by clicking the combobox, then clicking the option).
Snapshot rows carry each element's current value / toggle_state / scroll_percent, so a
change is proven by re-observing rather than trusting the dispatched result.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_perform_action",
        "description": "Run one advertised UIA action on a snapshot element: invoke, toggle, expand, collapse, select, set_value, set_range, scroll, default_action. Only names in that element's snapshot 'actions' are accepted. dispatched=true, verified=false.",
        "parameters": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string", "description": "snapshotId from cua_get_state."},
                "element_index": {"type": "integer", "description": "Element index in that snapshotId."},
                "action": {"type": "string", "description": "Action name from the element's 'actions' list."},
                "value": {"type": "string", "description": "Value for set_value / set_range / scroll [x,y]."}
            },
            "required": ["snapshot_id", "element_index", "action"]
        }
    }
}

import logging

from ._desktop_win import desktop_available
from ._desktop_state import action_error, app_consent_key, get_snapshot, snapshot_element
from ._desktop_uia import uia_apply, uia_resolve, uia_supported_actions, uia_value
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.PerformAction")

#: Actions whose effect can be read straight back from the same element (read-only read).
VALUE_ACTIONS = ("set_value", "set_range")


def cua_perform_action(snapshot_id: str, element_index: int, action: str, value=None) -> dict:
    """Perform one advertised UIA action on one snapshot element."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    snap = get_snapshot(snapshot_id)
    if snap is None:
        return action_error(f"SNAPSHOT_INVALIDATED: {snapshot_id} is gone or was replaced. "
                            "Re-run cua_get_state and use the NEW snapshotId.")
    if not is_granted(app_consent_key(snap["app_handle"]), current_conversation_id()):
        return action_error("DESKTOP_CONSENT_REQUIRED: access to this app is not granted.")

    element, err = snapshot_element(snapshot_id, element_index)
    if err:
        return action_error(err)
    if element.get("offscreen"):
        return action_error(f"ELEMENT_OFFSCREEN: index {element_index} ({element.get('name')!r}) is not "
                            "visible. Scroll or show the window, then observe again.")

    live = uia_resolve(snap["hwnd"], element["path"])
    if live is None:
        return action_error(f"ELEMENT_CHANGED: index {element_index} no longer resolves in this window "
                            "(the tree changed). Re-run cua_get_state.")

    advertised = uia_supported_actions(live)
    if action not in advertised:
        return action_error(f"ACTION_NOT_ADVERTISED: {action!r} for index {element_index}. "
                            f"This element supports: {advertised or 'nothing'}.")

    ok, why = uia_apply(live, action, value)
    logger.info("[CUA] action %s on index %d -> %s", action, element_index, ok)

    result = {"status": "ok" if ok else "error", "dispatched": bool(ok), "verified": False,
            "action": action, "element": {"index": element_index, "name": element.get("name"),
                                          "control_type": element.get("control_type")},
            "advertised_actions": advertised,
            "error": why,
            "next_step": "Re-observe with cua_get_state - a delivered action is not proof of a change."}

    # Read the value back from the SAME element (read-only), so a silent no-op is visible
    # instead of reported as success. Chrome's HTML <select> advertises ValuePattern and then
    # ignores it, so 'dispatched' alone would misreport what happened.
    if ok and action in VALUE_ACTIONS:
        now = uia_value(live)
        result["value_now"] = now
        result["changed"] = now != element.get("value")
        if not result["changed"]:
            result["caveat"] = ("The app accepted the action but the value did not change - it ignored "
                                "it. For a web dropdown, switch it by expanding the combobox and "
                                "invoking or selecting the option element, or by clicking the "
                                "combobox and then the option.")
    return result
