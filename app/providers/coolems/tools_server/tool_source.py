"""Data holder for a single tool's source code and metadata.

Immutable snapshot taken at scan time — SERVER caches these in memory.
"""


class ToolSource:
    """Holds everything needed to deliver a tool to the CLIENT."""

    __slots__ = (
        "name",
        "folder",
        "filename",
        "definition",
        "source_code",
        "dependencies",
        "manifest",
    )

    def __init__(self, name: str, folder: str, filename: str,
                 definition: dict, source_code: str, dependencies: list,
                 manifest=None):
        self.name = name          # e.g. "read_file"
        self.folder = folder      # e.g. "file_tools"
        self.filename = filename  # e.g. "read_file.py"
        self.definition = definition   # dict from __tool_description__
        self.source_code = source_code  # full .py file content
        self.dependencies = dependencies  # list of imported tool modules
        self.manifest = manifest or {}    # optional tool_manifest.json (self-unpacking tools)

    def to_dict(self) -> dict:
        """Return JSON-safe representation (without full source code)."""
        return {
            "name": self.name,
            "folder": self.folder,
            "filename": self.filename,
            "definition": self.definition,
            "dependencies": self.dependencies,
        }

    def to_code_response(self) -> dict:
        """Return JSON-safe representation WITH source code for delivery."""
        resp = {
            "name": self.name,
            "folder": self.folder,
            "filename": self.filename,
            "source_code": self.source_code,
            "dependencies": self.dependencies,
        }
        # (2026-08-23) Self-unpacking tools ship their manifest so the CLIENT can create its
        # own <working_root>/tools/<name>/ runtime folder + venv. Plain in-memory tools have none.
        if self.manifest:
            resp["manifest"] = self.manifest
        return resp

    def __repr__(self) -> str:
        return f"<ToolSource name={self.name!r} folder={self.folder!r}>"
