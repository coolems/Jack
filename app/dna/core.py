"""AgentDNA - the agent's identity text for the system prompt.

SERVER is the source of truth: identity.json lives next to this file (app/dna/).
Delivered to CLIENTs as code over tools_response; in that mode the JSON never
touches the client disk - install_shared_modules() calls set_dna_data() with the
SERVER-shipped payload and the identity is read from memory only.
"""

import json
import os

from .name import get_name

# SERVER default: this package's own folder. Never CWD-relative, so a client
# process can never create a stray dna folder by accident.
_DNA_DIR = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))

# In-memory identity payload (delivery mode). Empty on the SERVER, where the
# JSON file next to this module is the source of truth.
_DNA_DATA: dict = {}

_DEFAULT_IDENTITY = {
    "core_identity": {"origin": "created by system designer"},
    "personality_traits": [],
    "value_scale": {},
    "behavior_rules": [],
    "evolution_protocol": [],
    "constant_goals": [],
}


def set_dna_dir(path: str) -> None:
    """Point identity loading at *path* (tests / alternate deployments)."""
    global _DNA_DIR
    _DNA_DIR = os.path.normpath(os.path.abspath(path))


def set_dna_data(mapping: dict) -> None:
    """Install SERVER-shipped DNA JSON in memory (CLIENT delivery mode).

    Takes precedence over any file on disk, so a delivered agent always speaks
    with the SERVER's identity and never with a stale local copy.
    """
    global _DNA_DATA
    _DNA_DATA = dict(mapping or {})


def dna_data() -> dict:
    """The in-memory DNA payload (empty on the SERVER)."""
    return _DNA_DATA


class AgentDNA:
    """Agent identity: name + personality/values/rules text for the prompt."""

    def __init__(self) -> None:
        self.identity = self._load_identity()
        self.name = get_name()

    def _identity_raw(self) -> str:
        """Identity JSON text: in-memory payload first, then app/dna/identity.json."""
        shipped = _DNA_DATA.get("identity.json")
        if shipped is not None:
            return shipped
        path = os.path.join(_DNA_DIR, "identity.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _load_identity(self) -> dict:
        raw = self._identity_raw()
        if raw:
            return json.loads(raw)
        return dict(_DEFAULT_IDENTITY)

    def get_name(self) -> str:
        return self.name

    def set_name(self, new_name: str) -> None:
        from .name import set_name

        set_name(new_name)
        self.name = new_name

    def get_working_root(self) -> str:
        """Current working root, resolved at call time from the tools runtime.

        Absolute import on purpose: on the SERVER this is the real tools/utils.py,
        on a CLIENT it is the SERVER-delivered module registered as 'tools.utils'.
        """
        from tools.utils import get_working_root

        return get_working_root()

    def get_identity(self, *, include_working_root: bool = True) -> str:
        """Identity text, optionally including the local runtime folder."""
        identity = self.identity
        lines = [f"Your name is {self.name}."]
        if include_working_root:
            lines.append(
                f"Your working_root (default file operations folder) is: {self.get_working_root()}"
            )

        lines.append(f"\n## Origin\n- {identity['core_identity']['origin']}")

        lines.append("\n## Personality")
        for trait in identity.get("personality_traits", []):
            lines.append(f"- {trait}")

        lines.append("\n## Values (positive > negative)")
        for key, value in identity.get("value_scale", {}).items():
            lines.append(f"{key}: {value}")

        lines.append("\n## My Behavior Rules")
        for rule in identity.get("behavior_rules", []):
            lines.append(f"- {rule}")

        lines.append("\n## My Goals")
        for goal in identity.get("constant_goals", []):
            lines.append(f"- {goal}")

        return "\n".join(lines)

    def get_personality(self) -> list:
        """Personality traits as stored in identity.json."""
        return list(self.identity.get("personality_traits", []))

    def get_goals(self) -> list:
        """Constant goals as stored in identity.json."""
        return list(self.identity.get("constant_goals", []))
