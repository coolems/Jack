"""
COOLEMS WebSocket module.

Provides the main WebSocket chat handler factory.
Split into focused sub-modules for maintainability:
  - handler.py        : Main websocket_chat() orchestrator
  - message_types.py  : WebSocket send helpers (system, content, error, etc.)
  - db_ops.py         : Database save operations
  - file_handler.py   : Media file processing (images, text files)
  - search_handler.py : /search command logic
"""

from app.websocket.handler import create_websocket_handler

__all__ = ["create_websocket_handler"]
