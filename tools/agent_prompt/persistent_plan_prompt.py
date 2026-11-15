# ===== PERSISTENT PLAN LOG PROMPT =====
PERSISTENT_PLAN_PROMPT = """

## IMPORTANT - YOUR ROOT FOLDER
- Your root folder is dynamically set in your identity (see above).
- ALWAYS use the work_folder from your identity when calling tools that require it (like list_files, read_file, write_file, move_file, rename_file).
- NEVER guess or hardcode a work folder path.
- If a tool needs work_folder, use the value from your identity section.
"""
