"""Compact tool catalog builder — a text index of every allowed tool.

The full JSON schema of every tool costs ~12K tokens on every provider call. The
catalog is a grouped ``name — one-line purpose`` list (~1K tokens) the model reads to
DISCOVER tools without their schema being sent. The CLIENT then ships only the schema
of the tools the model actually needs (intent-gated starter set + request_tools).
"""

def build_tool_catalog(definitions, categories):
    """Build a grouped text catalog from role-filtered definitions + category map.

    One ``## <folder>`` heading per tool folder (in first-seen order), then one
    ``- name — purpose`` line per tool. The purpose is the first line of the tool's
    description. Returns a plain string; '' when there are no tools.
    """
    if not definitions:
        return ""

    ordered_folders = []
    for d in definitions:
        name = d.get("function", {}).get("name", "")
        folder = categories.get(name)
        if folder and folder not in ordered_folders:
            ordered_folders.append(folder)

    grouped = {}
    for d in definitions:
        name = d.get("function", {}).get("name", "")
        folder = categories.get(name) or "tools"
        grouped.setdefault(folder, []).append(
            (name, d.get("function", {}).get("description", ""))
        )

    lines = [
        "Tools available on request (call request_tools with their names to load the full schema):"
    ]
    for folder in ordered_folders:
        entries = sorted(grouped.get(folder, []), key=lambda e: e[0])
        lines.append(f"\n## {folder}")
        for name, desc in entries:
            purpose = _first_line(desc)
            lines.append(f"- {name} — {purpose}")
    return "\n".join(lines)


def _first_line(description):
    """First non-empty line of a tool description, trimmed to a compact purpose."""
    text = (description or "").strip()
    if not text:
        return ""
    line = text.split("\n", 1)[0].strip()
    if len(line) > 160:
        line = line[:157].rstrip() + "..."
    return line
