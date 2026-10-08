"""Live setup progress tracking for a tool runtime (2026-09-21).

The long first-run steps (venv creation, pip install of multi-GB wheels, model weight
downloads by the tool itself) are TRACKED in a tiny JSON file inside the runtime folder:
<runtime_dir>/.setup_progress.json. It is updated live while each subprocess runs and read
by the CLIENT's GET /api/setup/status endpoint so the UI can show a "Setup / Downloads"
section (per-pip-package status; per-model-file size, downloaded bytes, speed and ETA).

DESIGN RULES:
  * The state file is INFORMATIONAL ONLY. Every tracking call is wrapped in try/except -
    a progress failure must NEVER break or delay the actual setup.
  * Writes are atomic (tmp + os.replace) so a concurrent reader never sees a torn file.
  * A stage row exists in the file ONLY when that work actually ran this setup call
    (2026-09-24): an already-existing venv or a satisfied .deps_installed marker writes NO
    row, so a fully-cached machine shows nothing in the UI's Setup/Downloads section -
    only a first run (create / install / download) does.

State shape (all stages optional - absent means "not started/not applicable"): see the
module docstring of tools/tool_bootstrap.py for the full schema.
"""

import hashlib
import json
import logging
import os
import time

logger = logging.getLogger("COOLEMS.Tools.Bootstrap")

def sha256_prefix(text: str) -> str:
    """First 16 hex chars of the SHA-256 of *text* (integrity fingerprint)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Live setup progress (2026-09-21) - see module docstring for the state shape.
# ---------------------------------------------------------------------------

PROGRESS_FILENAME = ".setup_progress.json"

#: Stage names in display order (UI renders them top-to-bottom).
STAGE_ORDER = ("venv", "pip", "model")


def _progress_path(runtime_dir: str) -> str:
    return os.path.join(runtime_dir, PROGRESS_FILENAME)


class SetupProgress:
    """Tiny atomic JSON state file tracking one tool runtime's setup stages.

    Pure information channel for the UI (GET /api/setup/status). Every public method
    is fail-safe by contract: a progress bookkeeping failure logs at debug level and
    returns - it must never raise into (or slow down) the real setup work.
    """

    def __init__(self, runtime_dir: str):
        self.tool = os.path.basename(os.path.normpath(runtime_dir)) or "tool"
        self.path = _progress_path(runtime_dir)
        # Bumped once per setup RUN (ensure_tool_runtime / model pre-flight). An orphaned
        # process from an earlier killed attempt may still be writing its old state - when
        # it notices the file carries a newer generation it stops, so the live run wins.
        self.generation = int(self.load().get("generation") or 0) + 1

    # -- low-level ---------------------------------------------------------
    def load(self) -> dict:
        """Current state, or a fresh skeleton when missing/corrupt."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if not isinstance(data.get("stages"), dict):
                    data["stages"] = {}
                return data
        except Exception:
            pass  # missing/corrupt -> fresh skeleton below
        return {"tool": self.tool, "updated_at": time.time(), "stages": {}}

    def is_current(self) -> bool:
        """True while no NEWER setup run has claimed the state file.

        Ownership is claimed on every write (save() stamps this writer's generation), so a
        fresh run - which starts at file_gen + 1 and resets/updates its stages first - takes
        over immediately; an orphaned process from an OLDER attempt (e.g. a pip install left
        running after its parent was killed) then sees the higher generation here and stops
        clobbering the live run's progress.
        """
        try:
            return int(self.load().get("generation") or 0) <= self.generation
        except Exception:
            return True  # unreadable file - keep writing (load() will rebuild it)

    def reset(self, stages=None) -> None:
        """Start a fresh generation and drop stale stage state before new work begins.

        Without this, a RETRY after an interrupted run would still show the previous
        attempt's 'done' rows while pip is actually installing again - exactly the
        'UI says completed but venv is still installing' confusion (2026-09-21 field fix).
        *stages* names whose old state to wipe; others are left untouched.
        """
        try:
            state = self.load()
            for name in (stages or list(state["stages"].keys())):
                state["stages"].pop(name, None)
            state["generation"] = self.generation
            self.save(state)
        except Exception as e:  # informational only - never break setup
            logger.debug(f"[bootstrap] progress reset failed (ignored): {e}")

    def save(self, state: dict) -> None:
        """Atomic write (tmp + os.replace); readers never observe a torn file."""
        try:
            # every write claims/keeps ownership: an older orphaned writer notices the
            # newer generation on its next update and stops clobbering this run's state
            state["generation"] = self.generation
            state["updated_at"] = time.time()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self.path)
        except Exception as e:  # informational only - never break setup
            logger.debug(f"[bootstrap] progress save failed (ignored): {e}")

    # -- stage helpers ------------------------------------------------------
    def set_stage(self, stage: str, **fields) -> None:
        """Merge *fields* into one stage dict and persist.

        Silently drops the update when a NEWER setup run has taken over (stale orphaned
        writer) - the live run's state must not be clobbered by a dead attempt.
        """
        try:
            if not self.is_current():
                return  # superseded by a newer run - stop writing
            state = self.load()
            st = state["stages"].setdefault(stage, {})
            if isinstance(st, dict):
                st.update(fields)
            else:
                state["stages"][stage] = fields
            self.save(state)
        except Exception as e:  # informational only - never break setup
            logger.debug(f"[bootstrap] progress set_stage({stage}) failed (ignored): {e}")

    def stage(self, stage: str) -> dict:
        try:
            st = self.load()["stages"].get(stage)
            return st if isinstance(st, dict) else {}
        except Exception:
            return {}
