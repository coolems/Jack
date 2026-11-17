"""Agent/Model endpoints - models, provider status, agent DNA, learning, stop generation.

SECURITY BOUNDARY: the CLIENT resolves no permissions from profiles.json and scans no
disk. The SERVER authenticates the key over WS and sends only what that role may use.

MODEL SWITCH: "SERVER not answering" during a switch is expected - one clean log line,
never a traceback.

SETUP MODE: POST /api/auth/set-key writes the first API key while none is configured;
the auth middleware lets only that path through, and only from loopback.

One module per endpoint group, attached in order: auth_setup, settings, mcp_servers,
search_engines, model_switching, python_exec_approval, question. The flat app/routers/agent.py
is a re-export facade for create_agent_router."""

from fastapi import APIRouter

from .auth_setup import register as _register_auth
from .mcp_servers import register as _register_mcp_servers
from .model_switching import register as _register_model_switching
from .python_exec_approval import register as _register_python_exec_approval
from .question import register as _register_question
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
    # MCP tab: own path group /api/mcp/* - registered with the other config-management routes.
    _register_mcp_servers(router, provider, model_name, api_timeout, agent, stop_events)
    _register_search_engines(router, provider, model_name, api_timeout, agent, stop_events)
    _register_model_switching(router, provider, model_name, api_timeout, agent, stop_events)
    _register_python_exec_approval(router, provider, model_name, api_timeout, agent, stop_events)
    _register_question(router, provider, model_name, api_timeout, agent, stop_events)

    return router
