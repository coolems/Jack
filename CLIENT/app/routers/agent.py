"""Agent/Model endpoints - Models, provider status, agent DNA, learning, stop generation.

UPDATED: Role-based model filtering happens ON THE SERVER (pre-filtered lists).
UPDATED: Model switching endpoint for llama.cpp
UPDATED: Settings endpoint for server_address

SECURITY BOUNDARY (2026-07): The CLIENT no longer resolves permissions from
profiles.json or scans disk. The SERVER authenticates the user's key over WS
and only sends model lists / tool definitions allowed for that role.

MODEL SWITCH ERROR HANDLING (2026-08-20):
  - While a switch is in progress, "SERVER not answering" is an EXPECTED state:
    it is logged as INFO/WARNING with one clean line - never as an ERROR traceback.
  - The background switch task waits for the SERVER reload within the configured
    budget (config.MODEL_SWITCH_*). No fallbacks: we either confirm the new model
    loaded or report a single, clear failure.

SETUP MODE (2026-09-01): POST /api/auth/set-key writes the first API key while no
key is configured yet. The auth middleware lets ONLY this path through in setup
mode (and only from loopback) - it is the one action possible before a key exists.

MODULE SPLIT (2026-10): the implementation now lives in the CLIENT/app/routers/agent/
package - one module per endpoint group, routes attached verbatim in the original
order by create_agent_router() (see that package's __init__.py for the map). This flat
file is a thin re-export facade so existing imports keep working unchanged:

    from app.routers.agent import create_agent_router   # CLIENT/app/endpoints.py
"""

from .agent import create_agent_router  # noqa: F401  -- package facade; implementation in CLIENT/app/routers/agent/
