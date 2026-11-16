"""app.chat_bus - CLIENT-side per-chat background sessions.

Each conversation has a ChatChannel in the ChatBus that runs its turn as an independent
asyncio task: frames stream live while a UI socket is attached and are buffered (bounded)
otherwise, so switching chats in the browser never kills another chat's session.
"""

from .bus import ChatBus, get_chat_bus, set_chat_bus
from .channel import ChannelStatus, ChannelWebSocket, ChatChannel
from .generation_task import run_generation_turn
from .exec_approval import (
    ExecApprovalState,
    build_approval_frame,
)

# (2026-07-18 unified consent dialog) ONE shared code path for all four card kinds:
# python / url / file / network - see consent_dialog.py.
from .consent_dialog import (
    ConsentDialog,
    ConsentResult,
    build_frame,
    explain_instruction,
)

__all__ = [
    "ConsentDialog,"
    "ConsentResult,"
    "build_frame,"
    "explain_instruction,"
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
