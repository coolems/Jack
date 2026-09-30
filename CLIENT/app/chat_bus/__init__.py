"""app.chat_bus — CLIENT-side per-chat background sessions (2026-09-08 multi-chat, Phase 3).

The UI WebSocket no longer owns a generation turn. Each conversation has a ChatChannel in
the ChatBus that runs its turn as an independent asyncio task: frames stream live while a
UI socket is attached and are buffered (bounded) otherwise, so switching chats in the
browser never kills another chat's session — exactly the requested behaviour.

Public API (kept minimal on purpose):
    ChatBus           — conv_id → channel registry + submit/attach/detach/stop (bus.py)
    ChatChannel       — one chat's persistent state: status, stop event, buffer (channel.py)
    ChannelWebSocket  — provider-facing socket that routes frames through the channel
    run_generation_turn — background turn runner extracted from websocket/handler.py
    ExecApprovalState — per-run user approval gate for python_exec (exec_approval.py):
                        'allow' / 'deny' / 'run_all' ("run until task done", resets each run)

Singletons: set_chat_bus()/get_chat_bus() — wired at app startup; tests build their own.
"""

from .bus import ChatBus, get_chat_bus, set_chat_bus
from .channel import ChannelStatus, ChannelWebSocket, ChatChannel
from .generation_task import run_generation_turn
from .exec_approval import (
    ExecApprovalState,
    build_approval_frame,
)

__all__ = [
    "ChatBus",
    "ChatChannel",
    "ChannelStatus",
    "ChannelWebSocket",
    "ExecApprovalState",
    "build_approval_frame",
    "get_chat_bus",
    "run_generation_turn",
    "set_chat_bus",
]
