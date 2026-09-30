"""Auth Gateway Package - SERVER-side authentication and profile enforcement.

Loads .api_keys.json and profiles.json from config/. On WebSocket auth, looks up user role
and returns their permissions (allowed models, allowed tools). SERVER uses this to filter
everything before relaying to LLM.

No fallbacks. If a user is not in .api_keys.json or has no valid profile -> rejected.

Backward compatibility: importing AuthGateway from app.auth_gateway still works exactly as before.
"""

from .auth_core import AuthGateway

__all__ = ["AuthGateway"]
