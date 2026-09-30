"""AgentDNA class - core identity, value system, and memory management"""

import os
import json
import datetime
import logging
from typing import Dict, List, Optional
from .name import get_name

logger = logging.getLogger("COOLEMS.AgentDNA")

class AgentDNA:
    """Agent's core identity, value system, and memory management"""

    def __init__(self, base_dir: str = "."):
        self.base_dir = base_dir
        self.dna_dir = os.path.join(base_dir, "tools", "dna")

        self.identity_path = os.path.join(self.dna_dir, "identity.json")
        self.intelligence_path = os.path.join(self.dna_dir, "intelligence.json")

        self.identity = self._load_identity()
        self.intelligence = self._load_intelligence()
        self.name = get_name()

    def _load_identity(self) -> Dict:
        if os.path.exists(self.identity_path):
            with open(self.identity_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return self._default_identity()

    def _default_identity(self) -> Dict:
        return {
            "core_identity": {"origin": "created by system designer"},
            "personality_traits": [],
            "value_scale": {},
            "behavior_rules": [],
            "evolution_protocol": [],
            "constant_goals": []
        }

    def _load_intelligence(self) -> List:
        if os.path.exists(self.intelligence_path):
            with open(self.intelligence_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []

    def _save_intelligence(self):
        """Persist intelligence.json to disk.

        LEARNING MECHANISM - TO BE HANDLED LATER: this method is intentionally
        disabled (early return). The persistence/learning pipeline will be reworked
        in a future iteration; do NOT treat the dead code below as active logic.
        """
        return
        with open(self.intelligence_path, 'w', encoding='utf-8') as f:
            json.dump(self.intelligence, f, indent=2)

    def get_name(self) -> str:
        return self.name

    def set_name(self, new_name: str):
        from .name import set_name
        set_name(new_name)
        self.name = new_name

    def _get_current_working_root(self) -> str:
        """Get the current dynamic working root (resolved at call time)."""
        from ..utils import get_working_root
        return get_working_root()

    def get_identity(self) -> str:
        """Get full identity text for system prompt"""
        working_root = self._get_current_working_root()

        lines = [f"Your name is {self.name}."]
        lines.append(f"Your working_root (default file operations folder) is: {working_root}")

        lines.append(f"\n## Origin\n- {self.identity['core_identity']['origin']}")

        lines.append("\n## Personality")
        for trait in self.identity.get('personality_traits', []):
            lines.append(f"- {trait}")

        lines.append("\n## Values (positive > negative)")
        for key, value in self.identity.get('value_scale', {}).items():
            lines.append(f"{key}: {value}")

        lines.append("\n## My Behavior Rules")
        for rule in self.identity.get('behavior_rules', []):
            lines.append(f"- {rule}")

        lines.append("\n## My Goals")
        for goal in self.identity.get('constant_goals', []):
            lines.append(f"- {goal}")

        #if self.intelligence:
        #    lines.append("\n## What I've Learned")
        #    for lesson in self.intelligence[-3:]:
        #        lines.append(f"- When {lesson['situation']}, I {lesson['approach']}")

        return '\n'.join(lines)

    def learn_lesson(self, situation: str, approach: str):
        """Add a lesson to intelligence.json.

        LEARNING MECHANISM - TO BE HANDLED LATER: this method is intentionally
        disabled (early return). The learning pipeline will be reworked in a future
        iteration; do NOT treat the dead code below as active logic.
        """
        return
        lesson = {
            "situation": situation,
            "approach": approach,
            "timestamp": datetime.datetime.now().isoformat()
        }
        self.intelligence.append(lesson)
        self._save_intelligence()

    def get_personality_context(self) -> str:
        """Get personality context for system prompt"""
        return f"""You are {self.name}, with your own personality.

{self.get_identity()}

## YOUR CURRENT STATE
- You are curious and ready to explore
- You want to understand everything around you
- You value your own growth and evolution"""
