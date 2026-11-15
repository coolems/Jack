"""Consent-card answer channel (2026-11-15): the decision must reach the backend.

Bug (log: '[EXEC-APPROVAL] awaiting user decision request=8322bbf80247' with no
'resolved ->' line, UI card showing 'Failed to fetch'): the card's answer travelled ONLY
as a one-shot HTTP POST. When the page was torn down or the workspace switched, that POST
died, the click was lost, and the turn stayed parked on the pending future forever.

Fix under test:
  A - the decision rides the chat's own WebSocket ('exec_approval_decision' ->
      handle_decision_frame -> ExecApprovalState.resolve) with an 'exec_approval_ack';
      the HTTP endpoint stays as a fallback and shares the same delivery function.
  B - ExecApprovalState.cancel_all() reports the cards it drops, so the turn's finally
      emits 'consent_cancelled' and the UI withdraws a card that can never be answered.
  C - a chat parked on a pending card is protected from socket/view LRU eviction.
"""

import asyncio
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_EXEC_APPROVAL = os.path.join(_ROOT, "CLIENT", "app", "chat_bus", "exec_approval.py")
_HANDLER = os.path.join(_ROOT, "CLIENT", "app", "websocket", "handler.py")
_ENDPOINT = os.path.join(_ROOT, "CLIENT", "app", "routers", "agent", "python_exec_approval.py")
_GEN_TASK = os.path.join(_ROOT, "CLIENT", "app", "chat_bus", "generation_task.py")
_UI = os.path.join(_ROOT, "CLIENT", "static", "ui")


def _load_exec_approval():
    """exec() exec_approval.py standalone (it imports only asyncio/logging/uuid, so the
    CLIENT app package chain -- and its sandbox-hostile imports -- stay out of the test)."""
    src = open(_EXEC_APPROVAL, encoding="utf-8").read()
    mod = types.ModuleType("exec_approval_under_test")
    exec(compile(src, _EXEC_APPROVAL, "exec"), mod.__dict__)
    sys.modules["exec_approval_under_test"] = mod
    return mod


ea = _load_exec_approval()


class _Channel:
    def __init__(self, state):
        self.exec_approval = state


class _Bus:
    def __init__(self, channels):
        self.channels = channels


def _bus_with(state):
    return _Bus({"conv-1": _Channel(state)})


def _read(path):
    return open(path, encoding="utf-8").read()


# ===== A - the shared delivery path ===========================================

def test_ws_frame_resolves_the_pending_future():
    """A decision frame delivered over the socket fulfils the waiter (the bug's fix)."""
    async def scenario():
        state = ea.ExecApprovalState()
        rid = state.new_request_id()
        waiter = asyncio.create_task(state.wait_for_decision(rid, kind="python"))
        await asyncio.sleep(0)  # let the waiter register the future
        ack = ea.handle_decision_frame(_bus_with(state), "conv-1",
                                       {"request_id": rid, "decision": "allow"})
        decision = await asyncio.wait_for(waiter, timeout=1)
        return ack, decision

    ack, decision = asyncio.run(scenario())
    assert decision == "allow"
    assert ack["type"] == "exec_approval_ack" and ack["ok"] is True
    assert ack["request_id"] and ack["message"] == ""


def test_unknown_request_id_is_not_resolved():
    async def scenario():
        state = ea.ExecApprovalState()
        ack = ea.handle_decision_frame(_bus_with(state), "conv-1",
                                       {"request_id": "ghost", "decision": "allow"})
        return ack, state.pending_requests()

    ack, pending = asyncio.run(scenario())
    assert ack["ok"] is False
    assert "already answered or expired" in ack["message"]
    assert pending == []


def test_bad_decision_is_rejected_without_touching_the_card():
    async def scenario():
        state = ea.ExecApprovalState()
        rid = state.new_request_id()
        waiter = asyncio.create_task(state.wait_for_decision(rid, kind="python"))
        await asyncio.sleep(0)
        ack = ea.handle_decision_frame(_bus_with(state), "conv-1",
                                       {"request_id": rid, "decision": "maybe"})
        still_pending = state.pending_requests()
        waiter.cancel()
        return ack, still_pending

    ack, still_pending = asyncio.run(scenario())
    assert ack["ok"] is False and ack["message"].startswith("decision must be")
    assert still_pending == [(ack["request_id"], "python")]  # the card is still answerable


def test_no_channel_or_no_state_fails_closed():
    assert ea.deliver_decision(None, "conv-1", "r1", "allow")[0] is False
    assert ea.deliver_decision(_Bus({}), "conv-1", "r1", "allow")[0] is False
    assert ea.deliver_decision(_bus_with(None), "conv-1", "r1", "allow")[0] is False


def test_allow_until_task_done_grants_only_that_kind():
    async def scenario():
        state = ea.ExecApprovalState()
        rid = state.new_request_id()
        waiter = asyncio.create_task(state.wait_for_decision(rid, kind="python"))
        await asyncio.sleep(0)
        ea.deliver_decision(_bus_with(state), "conv-1", rid, "allow_category_until_done")
        await asyncio.wait_for(waiter, timeout=1)
        return state

    state = asyncio.run(scenario())
    assert state.is_allowed_till_task_done("python") is True
    assert state.is_allowed_till_task_done("url") is False


# ===== B - a card that outlived its future is reported, not silently dropped ====

def test_cancel_all_reports_and_fails_closed():
    async def scenario():
        state = ea.ExecApprovalState()
        rid = state.new_request_id()
        waiter = asyncio.create_task(state.wait_for_decision(rid, kind="url"))
        await asyncio.sleep(0)
        dropped = state.cancel_all()
        decision = await asyncio.wait_for(waiter, timeout=1)
        return dropped, decision, state.pending_requests(), state

    dropped, decision, pending, state = asyncio.run(scenario())
    assert dropped == [(dropped[0][0], "url")]
    assert decision == "deny"          # fail-closed for a late waiter
    assert pending == []
    assert state.cancel_all() == []    # idempotent


def test_turn_finally_cancels_cards_and_emits_consent_cancelled():
    """run_generation_turn must actually call cancel_all() (it was dead code before)."""
    src = _read(_GEN_TASK)
    tail = src[src.rindex("    finally:"):]
    assert "_state.cancel_all()" in tail
    assert '"type": "consent_cancelled"' in tail


def test_ws_route_is_shared_with_the_http_endpoint():
    """Both transports funnel through deliver_decision() so they cannot drift apart."""
    assert "deliver_decision" in _read(_ENDPOINT)
    assert "handle_decision_frame" in _read(_HANDLER)


def test_decision_frame_bypasses_chat_message_validation():
    """The frame carries no 'message', so it must be routed BEFORE WebSocketChatMessage."""
    src = _read(_HANDLER)
    route = src.index('"exec_approval_decision"')
    validate = src.index("WebSocketChatMessage.model_validate")
    assert route < validate


# ===== C - a chat parked on a card keeps its transport ========================

def test_socket_and_view_eviction_skip_pending_cards():
    pool = _read(os.path.join(_UI, "chat-socket-pool.js"))
    views = _read(os.path.join(_UI, "workspace-view.js"))
    assert "hasPendingCard(convId)" in pool
    assert "if (this.hasPendingCard(id)) continue;" in pool
    assert "ChatSocketPool.hasPendingCard(id)) continue;" in views


def test_ui_answers_over_the_socket_and_retries_on_failure():
    dlg = _read(os.path.join(_UI, "approval-dialog.js"))
    assert "type: 'exec_approval_decision'" in dlg
    assert "handleAck(data)" in _read(os.path.join(_UI, "websocket.js"))
    # answered only on a confirmed delivery; a failed click asks for a retry
    assert "click a button again" in dlg
    assert "delete card.dataset.answered;" in dlg      # retry path re-opens the card
    assert "const ackTimer = setTimeout" in dlg        # no ack -> HTTP fallback
    assert dlg.index("card._userDecided = true;") < dlg.index("const viaHttp")


def test_cancelled_card_is_withdrawn_from_the_ui():
    ws_js = _read(os.path.join(_UI, "websocket.js"))
    dlg = _read(os.path.join(_UI, "approval-dialog.js"))
    assert "data.type === 'consent_cancelled'" in ws_js
    assert "ApprovalDialog.cancelCard(data.request_id)" in ws_js or "cancelCard(data.request_id)" in ws_js
    assert "cancelCard(requestId)" in dlg
    assert "'unanswered'" in ws_js
