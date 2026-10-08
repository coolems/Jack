"""The generic self-unpacking runtime unfold step (or reuse of an existing one).

ensure_tool_runtime() is the public contract - see the module docstring of
tools/tool_bootstrap.py for the full description. Idempotent: an existing healthy runtime
is detected and reused; only the FIRST generation pays setup cost.
"""

import logging
import os
import subprocess
import sys
import time


logger = logging.getLogger("COOLEMS.Tools.Bootstrap")

from .pip_parse import (_apply_pip_progress, _public_pip_snapshot, apply_pip_event,
                        parse_pip_line, parse_pip_progress_line, _strip_internal_markers)
from .runtime_paths import (_migrate_legacy_runtime, _run_checked, _venv_python,
                            resolve_runtime_dir)
from .setup_progress import PROGRESS_FILENAME, SetupProgress, sha256_prefix
from .streaming import run_streaming

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
