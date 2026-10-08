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

MODULE SPLIT (2026-10): the original flat CLIENT/app/routers/agent.py was split into
one module per endpoint group; each register() attaches its routes VERBATIM in the
original order:

    auth_setup.py            POST /api/auth/set-key + POST /api/auth/media-token
    settings.py              GET/POST /api/settings (connection mode, relay, runtime tab)
    search_engines.py        GET/POST /api/search-engines
    model_switching.py       /api/models, POST /api/llama/load-model (guards 1+2 + async
                             switch task), ollama status/check, /api/status, agent DNA,
                             learning, /api/my-permissions
    python_exec_approval.py  POST /api/stop/{conv_id} (bus-aware) +
                             POST /api/exec-approval/{conv_id}/{request_id}

The flat CLIENT/app/routers/agent.py file is a re-export facade so the existing import
('from app.routers.agent import create_agent_router' in app/endpoints.py) keeps working.
"""

from fastapi import APIRouter

from .auth_setup import register as _register_auth
from .model_switching import register as _register_model_switching
from .python_exec_approval import register as _register_python_exec_approval
from .search_engines import register as _register_search_engines
from .settings import register as _register_settings


def create_agent_router(provider, model_name: str, api_timeout: int,
                        agent, stop_events) -> APIRouter:
    """Factory function that returns a configured agent/models router."""
    router = APIRouter(tags=["agent"])

    # Route registration order is the ORIGINAL flat-file order (matters for duplicate-path
    # resolution and keeps the OpenAPI operation ordering stable).
    _register_auth(router, provider, model_name, api_timeout, agent, stop_events)
    _register_settings(router, provider, model_name, api_timeout, agent, stop_events)
    _register_search_engines(router, provider, model_name, api_timeout, agent, stop_events)
    _register_model_switching(router, provider, model_name, api_timeout, agent, stop_events)
    _register_python_exec_approval(router, provider, model_name, api_timeout, agent, stop_events)

    return router
