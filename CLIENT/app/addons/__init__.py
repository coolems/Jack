"""Voice addons (TTS + STT) - one subfolder per local engine, one file per API wrapper.

Mirrors the MCP pattern: client_config/addons.json is the single source of truth,
the registry lazy-loads the active provider per addon, and the Settings > Addons tab
reads/writes the file through /api/addons. NOTHING is enabled by default.
"""
