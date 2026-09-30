"""COOLEMS CLIENT - shared utility modules.

Sub-modules:
    history_manager  - conversation history loading/trimming (token budgets)
    history_trimmer  - token-aware message trimming helpers
    logging_utils    - runtime verbose-logging toggle
    retry            - generic retry-with-backoff decorator/helpers
    clear_logs       - CLI: delete rotated log files (utils/clear_logs.py)
    clear_pycache    - CLI: remove __pycache__ directories

NOTE: ssrf_defense no longer has a CLIENT disk copy. The single source of truth
lives on the SERVER (tools/ssrf_defense.py) and is delivered at runtime as
in-memory code ('tools.ssrf_defense'), dropped when the client process exits.
"""
