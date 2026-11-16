"""COOLEMS CLIENT - shared utility modules.

Sub-modules:
    history_manager  - conversation history loading/trimming (token budgets)
    history_trimmer  - token-aware message trimming helpers
    retry            - generic retry-with-backoff decorator/helpers
    clear_logs       - CLI: delete rotated log files (utils/clear_logs.py)
    clear_pycache    - CLI: remove __pycache__ directories

ssrf_defense has NO CLIENT disk copy: the single source of truth is the SERVER's
tools/ssrf_defense.py, delivered at runtime as in-memory code ('tools.ssrf_defense') and
dropped when the client process exits.
"""
