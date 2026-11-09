# ===== PERSISTENT PLAN LOG PROMPT =====
PERSISTENT_PLAN_PROMPT = """

## YOUR ROOT FOLDER
- working_root is dynamic system configuration, not a tool argument. Do not pass `working_root` or `work_folder`; the runtime resolves the root for each conversation.
- Use the `path` / `filename` rules above. Never guess or hardcode a root folder.
"""