"""Generic self-unpacking runtime bootstrap for content-generating tools (2026-08-23).

WHY THIS MODULE EXISTS
======================
Heavy generation tools (image, and later video) must ship MINIMAL from the SERVER:
just their tool .py + a tiny tool_manifest.json. The heavy runtime - a dedicated
venv with torch/diffusers-class packages plus the tool's worker script - is created
ON THE USER MACHINE at first use under (CLIENT-INTERNAL - never in the user's working folder):

    <CLIENT_DIR>/tools/runtimes/<runtime_name>/
        <worker_filename>      <- written from the embedded WORKER_SOURCE (hash-verified)
        requirements.txt       <- from the manifest 'requirements' list
        .deps_installed        <- marker: pip install already ran for this venv
        venv/                  <- dedicated virtual env, REUSED across iterations

<CLIENT_DIR> is published by code_client.py as the COOLEMS_CLIENT_ROOT environment
variable at startup. Without that variable (pure SERVER process / local dev) the legacy
location <working_root>/tools/<runtime_name> is used instead - and a runtime found there
is migrated ONCE to the client location on first use after an upgrade (the venv is
smoke-tested after the move; if broken it is removed and rebuilt fresh).

This module is that generic unfold step. It runs on BOTH sides:
  * SERVER process (local dev): normal package import from tools/tool_bootstrap.py
  * CLIENT sandbox: delivered as a shared module source and installed in sys.modules
    under 'tools.tool_bootstrap'; its public symbols are injected into every tool's
    exec globals, so delivered tool code calls ensure_tool_runtime() directly.

CONTRACT (idempotent - safe to call on every invocation)
========================================================
ensure_tool_runtime(manifest, worker_source=None) -> {'runtime_dir', 'python_exe', 'worker_script'}

  * existing healthy runtime is detected and reused - only the FIRST generation pays
    setup cost; later calls are a handful of stat() checks.
  * worker script is (re)written ONLY when missing or its SHA-256 prefix changed, so a
    SERVER update rolls out on next use without wiping the venv.
  * pip install runs at most ONCE per venv (.deps_installed marker). A broken/empty
    venv python triggers a clean re-create + reinstall.

The diffusion MODEL WEIGHTS are not part of this bootstrap either: tools keep them in a
TOOL-LOCAL Hugging Face cache at <runtime_dir>/hf_cache (2026-09-12) - every file a tool
needs lives inside its own runtime folder, so one place shows the space it eats and one
delete removes everything. Convention for EVERY self-unpacking tool: run each of its
subprocesses with HF_HOME=<runtime_dir>/hf_cache (see _tool_env in generate_image.py), so
snapshot_download / from_pretrained store weights there instead of the user's default
%USERPROFILE%\\.cache\\huggingface. Z-Image-Turbo is Apache 2.0 - free for commercial use.

LIVE SETUP PROGRESS (2026-09-21)
================================
The long first-run steps (venv creation, pip install of multi-GB wheels, model weight
downloads by the tool itself) are now TRACKED in a tiny JSON file inside the runtime
folder: <runtime_dir>/.setup_progress.json. It is updated live while each subprocess
runs and read by the CLIENT's GET /api/setup/status endpoint so the UI can show a
"Setup / Downloads" section (per-pip-package status; per-model-file size, downloaded
bytes, speed and ETA).

DESIGN RULES:
  * The state file is INFORMATIONAL ONLY. Every tracking call is wrapped in try/except
    - a progress failure must NEVER break or delay the actual setup.
  * Writes are atomic (tmp + os.replace) so a concurrent reader never sees a torn file.
  * The subprocess exit-code / timeout contract of every step is UNCHANGED from the
    original blocking implementation; only HOW stdout is consumed differs (streamed).
  * A stage row exists in the file ONLY when that work actually ran this setup call
    (2026-09-24): an already-existing venv or a satisfied .deps_installed marker writes NO
    row, so a fully-cached machine shows nothing in the UI's Setup/Downloads section -
    only a first run (create / install / download) does.

State shape (all stages optional - absent means "not started/not applicable"):
{
  "tool": "<runtime_name>",
  "generation": <int>,          # bumped at the start of each setup run - stale writers
                                # (an orphaned pip from an earlier, killed attempt) stop
                                # overwriting a newer run's state when they notice it
  "updated_at": <epoch seconds>,
  "stages": {
    "venv":  {"status": "running"|"done"|"error", "detail": str?},
    "pip":   {"status": ..., "line": str?,
              "packages": {"<key>": {"name","status":"pending|downloading|cached|installing|done",
                                     "size_bytes": int?, "url": str?}}},
    "model": {"status": ..., "phase": "downloading"|"finalizing",
              "total_size": int,          # true repo size (sum of file sizes) - the denominator
              "downloaded": int,          # max bytes seen across the download aggregates
              "speed_bps": float?, "eta_sec": float?,   # aggregate speed + ETA for the whole download
              "files_total": int?, "files_done": int?,  # 'Fetching N files' counter
              "current_file": str?,       # only when per-file events arrive (non-snapshot hf)
              "files": {"<filename>": {"filename","size","downloaded","speed_bps","eta_sec"}},
              "line": str?}
  }
}
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
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


def _pip_size_to_bytes(text: str):
    """Parse pip's human sizes ('765 MB', '1.2 GB', '512 kB') -> int bytes, or None."""
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]{0,3})\b", text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "B").lower()
    mult = {"": 1, "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
    return int(value * mult.get(unit[:2] if unit.startswith("t") else unit, 1))


def _split_output_lines(raw: str):
    """Split a child's raw stdout into logical lines on BOTH '\n' and '\r'.

    pip redraws its download progress bar in place with carriage returns - no newline
    arrives until the wheel finishes (minutes for torch). Splitting on '\r' too turns
    every bar repaint into an event, so the UI gets live percent/speed/ETA instead of a
    silent 'downloading' line that only ends at EOF.
    """
    return [seg for seg in re.split(r"[\r\n]+", raw) if seg.strip()]


#: pip's carriage-return progress repaint: '765MB 94% <bar> 12.3MB/s eta 0:00:12' (the
#: bar body is box-drawing chars on ANSI terminals, CJK block elements in the Windows
#: console - both are matched and discarded).
#: pip's carriage-return progress repaints. Two styles exist depending on pip version:
#:   * percent style (tqdm-era):  '765MB 45% <bar> 12.3MB/s eta 0:00:38'
#:   * fraction style (rich era): '765.0/765.0 MB 12.3 MB/s eta 0:00:00'
#: The bar body is box-drawing chars on ANSI terminals, CJK block elements in the
#: Windows console - both are matched and discarded.
_PIP_BAR_CHARS = r"[\u2500-\u257f\u2580-\u259f\u25a0-\u25cf\u2e30-\u2e7f\ufb00-\ufbff]*"
_PIP_SPEED_RE = r"(?:\s+([\d.]+)\s*([KMGTkmgt]?)\s*B/s)?"
_PIP_ETA_RE = r"(?:\s+eta\s+(\S+))?"

_PIP_PROGRESS_PERCENT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*([KMGTkmgt]?B)\s+(\d{1,3})%\s*"
    + _PIP_BAR_CHARS
    + _PIP_SPEED_RE + _PIP_ETA_RE + r"\s*$")

_PIP_PROGRESS_FRAC_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([KMGTkmgt]?B)?"
    + _PIP_SPEED_RE + _PIP_ETA_RE + r"\s*$")

_PIP_UNIT_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _parse_eta(text: str):
    """'0:00:38' / '5:23' / '1:02:03' -> seconds (float), or None when unknown ('.')."""
    parts = (text or "").strip().split(":")
    if not all(re.fullmatch(r"\d+", x) for x in parts) or len(parts) > 3:
        return None
    sec = 0.0
    for x in parts:
        sec = sec * 60 + int(x)
    return sec


def _pip_speed_bytes_per_sec(num: str, unit: str):
    """'12.3' + 'M' -> bytes/second (binary units, as pip reports them)."""
    if not num:
        return None  # optional group - no speed reported on this repaint
    try:
        return float(num) * _PIP_UNIT_MULT.get((unit or "").upper(), 1)
    except ValueError:
        return None


def parse_pip_progress_line(line: str):
    """Parse one pip in-place progress repaint into {'percent','speed_bps','eta_sec'}.

    Handles BOTH bar styles (percent and downloaded/total) and both ETA shapes
    (M:SS / H:MM:SS). Returns None for anything that is not a progress repaint -
    those lines go through parse_pip_line as usual. Informational only: the UI shows
    the numbers on the package that is currently downloading.
    """
    s = (line or "").strip()
    m = _PIP_PROGRESS_PERCENT_RE.match(s)
    if m:
        speed = _pip_speed_bytes_per_sec(m.group(4), m.group(5))
        return {"percent": min(100, int(m.group(3))), "speed_bps": speed,
                "eta_sec": _parse_eta(m.group(6))}
    m = _PIP_PROGRESS_FRAC_RE.match(s)
    if m:
        try:
            got = float(m.group(1)); total = float(m.group(2))
        except ValueError:
            return None
        mult = _PIP_UNIT_MULT.get((m.group(3) or "").upper(), 1)
        pct = min(100, int(round(got / total * 100))) if total > 0 else 0
        speed = _pip_speed_bytes_per_sec(m.group(4), m.group(5))
        return {"percent": pct, "speed_bps": speed, "eta_sec": _parse_eta(m.group(6))}
    return None


def _apply_pip_progress(pip_state: dict, prog: dict) -> None:
    """Attach a percent/speed/ETA repaint to the package that is downloading right now.

    No-op when nothing is actively downloading (e.g. pip is between phases). The active
    key lives in pip_state['_active_download'] and is stripped before persisting.
    """
    pkgs = pip_state.get("packages") or {}
    active = pip_state.get("_active_download")
    if not active or active not in pkgs:
        return
    p = pkgs[active]
    if "percent" in prog and isinstance(prog["percent"], int):
        p["percent"] = min(100, max(0, prog["percent"]))
    if prog.get("speed_bps"):
        p["speed_bps"] = round(float(prog["speed_bps"]), 1)
    if prog.get("eta_sec") is not None:
        p["eta_sec"] = round(max(0.0, float(prog["eta_sec"])), 1)


def _strip_internal_markers(pip_state: dict) -> None:
    """Drop bookkeeping keys ('_active_download') before the state hits the JSON file."""
    pip_state.pop("_active_download", None)


def _public_pip_snapshot(pip_state: dict) -> dict:
    """Copy of the pip stage without internal bookkeeping keys ('_active_download')."""
    return {k: v for k, v in pip_state.items() if not str(k).startswith("_")}


def _normalize_pkg_name(name: str) -> str:
    """PEP-503-ish normalization so 'torch' == 'Torch_2.9.1+cu128-cp312...whl' match."""
    n = re.split(r"[=<>!~\s(]", name or "", 1)[0].strip()
    n = os.path.basename(n)
    if n.lower().endswith(".whl"):
        parts = n[:-4].split("-")
        n = parts[0] if len(parts) > 1 else n
    return re.sub(r"[_\.\-]+", "-", n).lower()


def parse_pip_line(line: str):
    """Parse ONE plain pip stdout line into a progress event, or None.

    Events (dicts):
      {'kind':'collect',   'name': 'torch==2.9.1+cu128'}
      {'kind':'satisfied', 'name': ...}
      {'kind':'download',  'file': 'torch-....whl', 'size_bytes': int|None, 'url': str}
      {'kind':'cached',    'file': ..., 'url': str?}
      {'kind':'install_start', 'names': [...]}   (continuation lines: {'kind':'install_cont','name':...})
      {'kind':'success',   'names': [...]}
      {'kind':'note',      'text': ...}          (anything else non-empty - kept as last line)
    """
    s = (line or "").strip()
    if not s:
        return None

    m = re.match(r"^Collecting\s+(\S+)", s)
    if m:
        return {"kind": "collect", "name": m.group(1)}

    m = re.match(r"^Requirement already satisfied:\s*(\S+)", s)
    if m:
        return {"kind": "satisfied", "name": m.group(1)}

    # 'Downloading https://...whl (765 MB)'  or  'Downloading torch-....whl'
    m = re.match(r"^Downloading\s+(\S+?)(?:\s+\(([^)]+)\))?\s*$", s)
    if m:
        from urllib.parse import unquote
        target = unquote(m.group(1))
        return {"kind": "download", "file": os.path.basename(target),
                "size_bytes": _pip_size_to_bytes(m.group(2)), "url": m.group(1)}

    # 'Using cached torch-....whl'  /  'Using cached https://... (765 MB)'
    m = re.match(r"^Using cached\s+(\S+)", s)
    if m:
        from urllib.parse import unquote
        return {"kind": "cached", "file": os.path.basename(unquote(m.group(1))),
                "url": m.group(1)}

    # 'Installing collected packages:' + names (may wrap onto continuation lines)
    if s.startswith("Installing collected packages"):
        rest = s.split(":", 1)[1].strip() if ":" in s else ""
        return {"kind": "install_start",
                "names": [x for x in re.split(r"[,\s]+", rest) if x]}

    m = re.match(r"^Successfully installed\s+(.+)$", s)
    if m:
        names = []
        for tok in m.group(1).split():
            # 'torch (2.9.1+cu128)' or plain 'torch'
            nm = re.sub(r"\s*\(.*\)$", "", tok)
            if nm:
                names.append(nm)
        return {"kind": "success", "names": names}

    # Indented continuation of the 'Installing collected packages:' name list.
    m = re.match(r"^\s+([A-Za-z0-9][A-Za-z0-9._+\-]*)\s*$", line)
    if m:
        return {"kind": "install_cont", "name": m.group(1)}

    # Progress-bar noise and other lines - surfaced as the 'last line' only.
    if re.match(r"^\d+/\d+\s*\[", s):
        return None
    return {"kind": "note", "text": s[:300]}


def apply_pip_event(pip_state: dict, ev: dict) -> None:
    """Merge one parse_pip_line() event into the 'pip' stage dict (in place).

    Package identity is matched by normalized name so a wheel filename maps back to
    its 'Collecting <spec>' entry. Unknown wheels get their own entry - nothing is
    ever dropped from what pip actually did.
    """
    pkgs = pip_state.setdefault("packages", {})

    def _find_by_norm(norm: str):
        """Exact normalized match first; else the LONGEST stored name that is a
        version-prefixed ancestor (stored 'torch' matches token 'torch-2.9.1-cu128').
        Longest-wins so 'foo-bar' never mis-matches onto stored 'foo'."""
        best = None
        for p in pkgs.values():
            pn = _normalize_pkg_name(p.get("name", ""))
            if not pn:
                continue
            if pn == norm or (norm.startswith(pn + "-") and
                              (best is None or len(pn) > len(_normalize_pkg_name(best.get("name", ""))))):
                best = p
        return best

    kind = ev.get("kind")
    if kind == "collect":
        name = ev["name"]
        key = _normalize_pkg_name(name) or name
        pkgs.setdefault(key, {"name": name, "status": "pending"})
    elif kind == "satisfied":
        p = _find_by_norm(_normalize_pkg_name(ev["name"]))
        if p:
            p["status"] = "done"
        else:
            key = _normalize_pkg_name(ev["name"]) or ev["name"]
            pkgs.setdefault(key, {"name": ev["name"], "status": "done"})
    elif kind in ("download", "cached"):
        norm = _normalize_pkg_name(ev.get("file", ""))
        key = norm or os.path.basename(ev.get("file") or ev.get("url") or "package")
        p = pkgs.get(key) if norm else None
        # a wheel whose exact spec was never collected may still match a stored entry by name prefix
        if p is None and norm:
            p = _find_by_norm(norm)
        if p is None:
            p = pkgs.setdefault(key, {"name": os.path.basename(ev.get("file") or ev.get("url") or "package"),
                                      "status": "pending"})
        if kind == "download":
            # a new download starts - clear stale progress numbers from the previous one
            prev = pip_state.get("_active_download")
            if prev and prev in pkgs:
                for k in ("percent", "speed_bps", "eta_sec"):
                    pkgs[prev].pop(k, None)
            p["status"] = "downloading"
            if ev.get("size_bytes"):
                p["size_bytes"] = ev["size_bytes"]
            if ev.get("url"):
                p["url"] = ev["url"]
            pip_state["_active_download"] = key
        else:
            # 'Using cached <wheel>' - the wheel is in pip's local cache; it still has to
            # be INSTALLED, so keep a distinct status (the UI shows it as ready-to-install)
            p["status"] = "cached"
            if pip_state.get("_active_download") == key:
                for k in ("percent", "speed_bps", "eta_sec"):
                    p.pop(k, None)
    elif kind == "install_start":
        for nm in ev.get("names", []):
            key = _normalize_pkg_name(nm) or nm
            pkgs.setdefault(key, {"name": nm, "status": "pending"})["status"] = "installing"
    elif kind == "install_cont":
        p = _find_by_norm(_normalize_pkg_name(ev.get("name", "")))
        if p:
            p["status"] = "installing"
    elif kind == "success":
        pip_state.pop("_active_download", None)  # all downloads are finished now
        for nm in ev.get("names", []):
            key = _normalize_pkg_name(nm) or nm
            pkgs.setdefault(key, {"name": nm, "status": "pending"})["status"] = "done"

    line_val = ev.get("text") or ev.get("file") or ev.get("url") \
        or (ev.get("names", [""])[0] if ev.get("names") else "")
    if line_val:
        pip_state["line"] = str(line_val)[:300]


def run_streaming(cmd: list, timeout: int = 0, env: dict = None, input_text: str = None,
                  on_line=None):
    """Run *cmd* streaming stdout line-by-line (live progress hook).

    Drop-in upgrade of subprocess.run(..., capture_output=True) for the setup steps:
      * on_line(line) is called for EVERY completed stdout line while the process runs
        (the callback may never break setup - exceptions are swallowed and logged);
      * returns (returncode, [stdout lines], stderr_text);
      * timeout>0 kills the process after *timeout* seconds and raises
        subprocess.TimeoutExpired exactly like subprocess.run does;
      * input_text is written to stdin when given.

    stderr is drained by a background thread so a chatty child can never deadlock on
    its pipe buffer while we are busy reading stdout lines.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=(subprocess.PIPE if input_text is not None else subprocess.DEVNULL),
                            text=True, env=env)

    # Drain stderr concurrently (bounded list - setup children are chatty but finite).
    stderr_chunks: list = []

    def _drain_stderr():
        try:
            for chunk in iter(proc.stderr.readline, ""):
                stderr_chunks.append(chunk)
        except Exception:
            pass

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    if input_text is not None and proc.stdin:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except Exception:
            pass

    stdout_lines: list = []
    timed_out = [False]  # set by the kill timer when the budget is exceeded
    timer = None
    if timeout and timeout > 0:
        def _kill():
            timed_out[0] = True
            try:
                proc.kill()
            except Exception:
                pass
        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()

    try:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break  # EOF - child closed stdout (normal exit or killed by the timer)
            # pip redraws its progress bar with '\r' (no newline until the wheel is done),
            # so one readline() can hold MINUTES of repaints - split them into events.
            for seg in _split_output_lines(raw):
                stdout_lines.append(seg)
                if on_line is not None:
                    try:
                        on_line(seg)
                    except Exception as e:  # progress hook must never break the install
                        logger.debug(f"[bootstrap] on_line hook failed (ignored): {e}")
    finally:
        if timer is not None:
            timer.cancel()

    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    err_thread.join(timeout=10)

    # Close the pipe ends explicitly - the child has exited (or been killed), so no more
    # data can arrive; leaving them open leaks file descriptors (ResourceWarning).
    for _stream in (proc.stdout, proc.stderr):
        try:
            if _stream is not None:
                _stream.close()
        except Exception:
            pass

    # A child that went SILENT past the budget is killed by the timer and then exits on
    # EOF above - surface it as TimeoutExpired (same contract as subprocess.run), not as
    # a plain non-zero return code.
    if timed_out[0]:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return rc, stdout_lines, "".join(stderr_chunks)


def ensure_tool_runtime(manifest: dict, worker_source: str = None,
                        worker_filename: str = "worker.py",
                        bootstrap_timeout: int = 2400) -> dict:
    """Unfold (or reuse) a self-unpacking tool's runtime on the user machine.

    Args:
        manifest: The tool's TOOL_MANIFEST dict - must carry 'runtime_name' and may
                  carry 'requirements' (list of pip specifiers).
        worker_source: Optional embedded worker script source; written to
                       <runtime_dir>/<worker_filename> when missing or changed.
        worker_filename: Worker script file name inside the runtime dir.
        bootstrap_timeout: Max seconds for the one-time pip install (first run only).

    Returns:
        {'runtime_dir': str, 'python_exe': str, 'worker_script': str | None}

    Raises:
        ValueError / RuntimeError with a human-readable message on setup failure.

    While the long steps run, live progress is written to <runtime_dir>/
    .setup_progress.json (see module docstring) for the UI's Setup/Downloads section.
    """
    if not isinstance(manifest, dict) or not manifest.get("runtime_name"):
        raise ValueError(f"Invalid tool manifest (need 'runtime_name'): {manifest!r}")

    runtime_dir = resolve_runtime_dir(manifest["runtime_name"])
    # One-time upgrade path: move a pre-existing legacy (working-root) runtime into the
    # client-internal location. Any failure degrades to "build fresh at the new home".
    try:
        _migrate_legacy_runtime(runtime_dir)
    except Exception as e:
        logger.warning(f"[bootstrap] Migration skipped/failed ({e}) - continuing with a "
                       f"fresh runtime at {runtime_dir}")
    venv_py = _venv_python(runtime_dir)
    worker_script = None

    os.makedirs(runtime_dir, exist_ok=True)
    progress = SetupProgress(runtime_dir)  # informational channel for the UI (2026-09-21)

    # 1) Worker script - write only when missing or changed (hash-verified).
    if worker_source:
        worker_script = os.path.join(runtime_dir, worker_filename)
        expected_hash = sha256_prefix(worker_source)
        needs_write = True
        if os.path.isfile(worker_script):
            try:
                with open(worker_script, "r", encoding="utf-8") as f:
                    on_disk = f.read()
                if sha256_prefix(on_disk) == expected_hash:
                    needs_write = False
            except Exception:
                pass  # unreadable -> rewrite below
        if needs_write:
            tmp_path = worker_script + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(worker_source)
            os.replace(tmp_path, worker_script)
            logger.info(f"[bootstrap] Worker script written to {worker_script}")

    # 2) requirements.txt from the manifest (only when there are requirements).
    reqs = list(manifest.get("requirements") or [])
    reqs_path = None
    if reqs:
        reqs_path = os.path.join(runtime_dir, "requirements.txt")
        reqs_text = "\n".join(reqs) + "\n"
        needs_write = True
        if os.path.isfile(reqs_path):
            try:
                with open(reqs_path, "r", encoding="utf-8") as f:
                    needs_write = (f.read() != reqs_text)
            except Exception:
                pass
        if needs_write:
            with open(reqs_path, "w", encoding="utf-8") as f:
                f.write(reqs_text)

    # 3) Dedicated venv - create once; a missing python.exe means re-create.
    venv_created = False
    if not os.path.isfile(venv_py):
        logger.info(f"[bootstrap] Creating dedicated venv in {runtime_dir} (first run)")
        progress.reset(stages=["venv"])  # drop the previous attempt's state first
        progress.set_stage("venv", status="running")
        try:
            _run_checked([sys.executable, "-m", "venv", os.path.join(runtime_dir, "venv")],
                         timeout=600, label="venv creation")
        except Exception as e:
            progress.set_stage("venv", status="error", detail=str(e)[:300])
            raise
        if not os.path.isfile(venv_py):
            progress.set_stage("venv", status="error",
                               detail=f"venv python missing after creation: {venv_py}")
            raise RuntimeError(f"venv python missing after creation: {venv_py}")
        venv_created = True
    else:
        # venv already exists from a previous run -> NO work performed this call, so NO
        # 'venv' row is written (2026-09-24): the UI's Setup/Downloads section must only
        # appear when something actually had to be created/downloaded. A stale row from an
        # earlier first run stays in the file untouched - and we do not reset here, so an
        # in-flight model stage (written by the tool itself) is never wiped either.
        pass  # nothing to create -> no 'venv' progress row for this run

    # 4) pip install - once per venv (.deps_installed marker stores the requirements
    #    hash). A requirements.txt that CHANGED, or a freshly created/repaired venv,
    #    invalidates it so dependency bumps roll out on next use.
    marker = os.path.join(runtime_dir, ".deps_installed")
    deps_stale = True
    if reqs_path and not venv_created and os.path.isfile(marker) and os.path.isfile(reqs_path):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                recorded = f.read().strip()
            with open(reqs_path, "r", encoding="utf-8") as f:
                deps_stale = (recorded != sha256_prefix(f.read()))
        except Exception:
            deps_stale = True

    if reqs and deps_stale:
        # Fresh pip run -> wipe the PREVIOUS attempt's package rows first. Without this a
        # retry after an interrupted install would show stale 'done' packages while pip is
        # actually still working (field report 2026-09-21). The new generation also tells
        # any orphaned old pip process to stop overwriting our state file.
        progress.reset(stages=["pip"])
        logger.info("[bootstrap] Installing tool dependencies (first run - this can take a while)")
        pip_cmd = [venv_py, "-m", "pip", "install", "--disable-pip-version-check"]
        # NOTE 2026-09-21: the old '-q' flag is gone on purpose - plain output gives us
        # per-package lines (Collecting/Downloading/Installing/Successfully installed)
        # that feed the UI's live pip progress. Output volume stays small (a few lines
        # per package), and it is streamed, never buffered in memory.
        # Optional extra package indexes from the manifest (e.g. pytorch.org cu128 wheels -
        # PyPI's Windows torch builds are CPU-only, so GPU tools pin a +cuXXX local version
        # that only exists on the vendor index).
        for url in (manifest.get("extra_index_urls") or []):
            pip_cmd += ["--extra-index-url", str(url)]
        pip_cmd += ["-r", reqs_path]

        pip_state = {"status": "running"}
        progress.set_stage("pip", **pip_state)

        def _on_pip_line(line: str):
            # pip's in-place progress repaints (percent/speed/ETA for the wheel that is
            # downloading right now) arrive as carriage-return segments - separate path.
            prog = parse_pip_progress_line(line)
            if prog is not None:
                _apply_pip_progress(pip_state, prog)
                now = time.time()
                if now - getattr(_on_pip_line, "_last_write", 0.0) > 0.5:
                    _on_pip_line._last_write = now
                    progress.set_stage("pip", **_public_pip_snapshot(pip_state))
                return
            ev = parse_pip_line(line)
            if not ev:
                return
            apply_pip_event(pip_state, ev)
            # Throttle writes: at most ~1x/0.5s plus a write on every status change.
            now = time.time()
            changed = ev.get("kind") in ("download", "success", "install_start") \
                or (ev.get("kind") == "collect" and len(pip_state.get("packages", {})) <= 1)
            if changed or now - getattr(_on_pip_line, "_last_write", 0.0) > 0.5:
                _on_pip_line._last_write = now
                progress.set_stage("pip", **_public_pip_snapshot(pip_state))

        try:
            rc, out_lines, err_text = run_streaming(pip_cmd, timeout=bootstrap_timeout,
                                                    on_line=_on_pip_line)
        except subprocess.TimeoutExpired:
            _strip_internal_markers(pip_state)
            for p in pip_state.get("packages", {}).values():
                if p.get("status") == "downloading":
                    p["status"] = "error"  # the wheel never finished
            progress.set_stage("pip", status="error", detail=f"timed out after {bootstrap_timeout}s",
                               packages=pip_state.get("packages"))
            raise RuntimeError(f"pip install timed out after {bootstrap_timeout} s")
        if rc != 0:
            detail = ((err_text or "\n".join(out_lines[-5:]) or "").strip())[-500:]
            _strip_internal_markers(pip_state)
            for p in pip_state.get("packages", {}).values():
                if p.get("status") == "downloading":
                    p["status"] = "error"  # the wheel never finished
            progress.set_stage("pip", status="error", detail=detail,
                               packages=pip_state.get("packages"))
            raise RuntimeError(f"pip install failed (exit {rc}): {detail}")

        # Mark anything still pending as done - pip only lists NEW installs in its final
        # line, so 'Successfully installed' may not name every package we tracked.
        for p in pip_state.get("packages", {}).values():
            if p.get("status") != "done":
                p["status"] = "done"
        # no download can be active any more - drop leftover progress numbers
        for p in pip_state.get("packages", {}).values():
            for k in ("percent", "speed_bps", "eta_sec"):
                p.pop(k, None)
        _strip_internal_markers(pip_state)  # internal marker - never persisted
        progress.set_stage("pip", status="done", packages=pip_state.get("packages"),
                           line=None)

        # Record success so future calls skip pip entirely (idempotent reuse).
        # Write the hash of exactly what was installed; a later requirements change
        # still invalidates it and triggers one legitimate reinstall, then re-records.
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(sha256_prefix(reqs_text))
        except Exception as e:
            logger.warning(f"[bootstrap] Could not write deps marker: {e}")
    elif reqs:
        # Dependencies already installed for this venv (.deps_installed matches) -> NO
        # work performed this call, so NO 'pip' row is written (2026-09-24). Same rule as
        # the venv branch above: the UI only shows a stage when it actually ran.
        pass  # nothing to install -> no 'pip' progress row for this run

    logger.debug(f"[bootstrap] Runtime ready at {runtime_dir}")
    return {"runtime_dir": runtime_dir, "python_exe": venv_py, "worker_script": worker_script}


def _get_working_root() -> str:
    """Live working root - single source of truth is <CLIENT>/config/.working_root.json."""
    from tools.utils import get_working_root

    return get_working_root()


def _client_tools_root() -> str | None:
    """Client-internal runtimes root, or None when not running inside the CLIENT process.

    code_client.py publishes its own directory as COOLEMS_CLIENT_ROOT at startup; tool
    runtimes are INTERNAL to the client and must never clutter the user's working folder.
    """
    client_root = os.environ.get("COOLEMS_CLIENT_ROOT", "").strip()
    if not client_root:
        return None
    return os.path.normpath(os.path.join(client_root, "tools", "runtimes"))


def resolve_runtime_dir(runtime_name: str) -> str:
    """Absolute path of this tool's own runtime folder.

    CLIENT process (COOLEMS_CLIENT_ROOT set): <CLIENT_DIR>/tools/runtimes/<runtime_name>
    - internal to the client, independent of the working folder. Otherwise (pure server
    process / local dev) the legacy location: <working_root>/tools/<runtime_name>.
    """
    if not isinstance(runtime_name, str) or "/" in runtime_name or "\\" in runtime_name \
            or ".." in runtime_name or not runtime_name.strip():
        raise ValueError(f"Invalid runtime name: {runtime_name!r}")
    client_root = _client_tools_root()
    base_dir = client_root if client_root else os.path.join(_get_working_root(), "tools")
    return os.path.normpath(os.path.join(base_dir, runtime_name))


def _smoke_test_venv(venv_py: str) -> bool:
    """True when the (possibly relocated) venv python still starts and imports sys.

    NOTE: must use "-c import sys" - a BARE python.exe enters interactive mode and
    waits on stdin forever, which would look like a broken venv (60s timeout).
    """
    try:
        r = subprocess.run([venv_py, "-c", "import sys"], capture_output=True,
                           text=True, timeout=60)
        return r.returncode == 0
    except PermissionError:
        # POSIX: a relocated venv may have lost its exec bit - restore and retry once.
        try:
            os.chmod(venv_py, 0o755)
        except Exception:
            return False
        try:
            r = subprocess.run([venv_py, "-c", "import sys"], capture_output=True,
                               text=True, timeout=60)
            return r.returncode == 0
        except Exception:
            return False
    except Exception as e:
        logger.warning(f"[bootstrap] venv smoke test failed to run: {e}")
        return False


def _migrate_legacy_runtime(runtime_dir: str) -> None:
    """One-time move of a pre-upgrade runtime from the legacy working-root location.

    Only runs when COOLEMS_CLIENT_ROOT is set, the new location is empty and the legacy
    folder still holds a real runtime (venv python or deps marker). The moved venv is
    smoke-tested; on failure it is removed so the normal fresh-create path rebuilds it.
    """
    client_root = _client_tools_root()
    if not client_root:
        return  # server-side dev: legacy location IS canonical here - nothing to migrate
    if os.path.exists(runtime_dir):
        return  # already at the new home (or partially created) - leave it alone
    legacy_dir = os.path.normpath(os.path.join(_get_working_root(), "tools",
                                               os.path.basename(runtime_dir)))
    if not os.path.isdir(legacy_dir) or legacy_dir == runtime_dir:
        return  # only stray files - nothing worth migrating; let the fresh path run

    has_venv_py = os.path.isfile(_venv_python(legacy_dir))
    has_marker = os.path.isfile(os.path.join(legacy_dir, ".deps_installed"))
    if not (has_venv_py or has_marker):
        return  # only stray files - nothing worth migrating; let the fresh path run

    logger.info(f"[bootstrap] Migrating legacy tool runtime {legacy_dir} -> {runtime_dir}")
    os.makedirs(os.path.dirname(runtime_dir), exist_ok=True)
    try:
        shutil.move(legacy_dir, runtime_dir)
    except Exception as e:
        # Partial move cleanup so the next call can retry from a clean slate.
        if os.path.isdir(runtime_dir):
            shutil.rmtree(runtime_dir, ignore_errors=True)
        raise RuntimeError(f"Legacy runtime migration failed ({e}); "
                           f"a fresh runtime will be built instead") from e

    venv_py = _venv_python(runtime_dir)
    if has_venv_py and os.path.isfile(venv_py) and not _smoke_test_venv(venv_py):
        logger.warning("[bootstrap] Migrated venv failed its smoke test - removing it; "
                       "a fresh venv will be created (one-time reinstall)")
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _venv_python(runtime_dir: str) -> str:
    """Path of the venv's python executable (Windows + POSIX layouts)."""
    if os.name == "nt":
        return os.path.join(runtime_dir, "venv", "Scripts", "python.exe")
    return os.path.join(runtime_dir, "venv", "bin", "python")


def _run_checked(cmd: list, timeout: int, label: str) -> None:
    """Run *cmd*, raising a clean RuntimeError with the tail of stderr on failure."""
    logger.info(f"[bootstrap] {label}: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[-500:]
        raise RuntimeError(f"{label} failed (exit {r.returncode}): {detail}")
