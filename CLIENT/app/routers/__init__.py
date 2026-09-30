"""COOLEMS CLIENT - FastAPI route modules.

Contains the three router factory groups assembled by app.endpoints.create_app():
    conversations.py  - /api/conversations/* (CRUD, messages, truncate, reset)
    files/            - /api/upload, /api/tree, /api/download, file read/write endpoints
    agent.py          - /api/models, /api/status, /api/settings, /api/agent/*, model switching

Each module exposes a create_*_router() factory; none of them hold global state.
"""
