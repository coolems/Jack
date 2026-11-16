"""Agent/model endpoints - models, provider status, agent DNA, learning, stop generation.

SECURITY BOUNDARY: the CLIENT resolves no permissions from profiles.json and scans no
disk. The SERVER authenticates the user's key over WebSocket and sends only the model
lists / tool definitions allowed for that role, pre-filtered.

MODEL SWITCH ERROR HANDLING
  - While a switch is in progress, "SERVER not answering" is an EXPECTED state: logged as
    INFO/WARNING with one clean line, never as an ERROR traceback.
  - The background switch task waits within config.MODEL_SWITCH_*. No fallbacks: either
    the new model is confirmed loaded or one clear failure is reported.

SETUP MODE: POST /api/auth/set-key writes the first API key while none is configured.
The auth middleware lets ONLY that path through in setup mode, and only from loopback.

The implementation lives in CLIENT/app/routers/agent/, ordered by create_agent_router()
(see that package's __init__.py). This flat file is a re-export facade so existing
imports keep working:
    from app.routers.agent import create_agent_router   # CLIENT/app/endpoints.py
"""

from .agent import create_agent_router  # noqa: F401  -- package facade; implementation in CLIENT/app/routers/agent/
