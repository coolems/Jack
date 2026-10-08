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

# ---------------------------------------------------------------------------
# Lazy attribute facade (PEP 562).
#
# WHY LAZY: on the CLIENT this module is delivered as shared source and installed in
# sys.modules BEFORE its submodules (install order is by dot-depth, parents first).
# A module-level 'from .X import Y' would therefore fail at install time. The real
# implementation lives in the tools/tool_bootstrap/ package; each public symbol is
# imported on first access - by then every submodule is already in sys.modules on the
# CLIENT (or importable from disk on the SERVER). The flat tools/tool_bootstrap.py
# file carries this same facade - on the SERVER it is shadowed by this package (a directory
# beats a .py of the same name), and its content is what gets SHIPPED to clients as the
# 'tool_bootstrap' shared source.
# ---------------------------------------------------------------------------

_LAZY_IMPORTS = {
    "ensure_tool_runtime":      "bootstrap",
    "run_streaming":            "streaming",
    "SetupProgress":            "setup_progress",
    "PROGRESS_FILENAME":        "setup_progress",
    "STAGE_ORDER":              "setup_progress",
    "sha256_prefix":            "setup_progress",
    "parse_pip_line":           "pip_parse",
    "parse_pip_progress_line":  "pip_parse",
    "apply_pip_event":          "pip_parse",
    "resolve_runtime_dir":      "runtime_paths",
}


def __getattr__(name):
    try:
        submodule = _LAZY_IMPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'tools.tool_bootstrap' has no attribute {name!r}") from None
    import importlib
    try:
        mod = importlib.import_module(f"tools.tool_bootstrap.{submodule}")
    except Exception as e:  # submodule not installed (CLIENT) / missing on disk - fail clean
        raise AttributeError(
            f"tools.tool_bootstrap.{name} unavailable: {e!r} (is the tools/tool_bootstrap "
            f"package fully delivered/installed?)") from None
    val = getattr(mod, name)
    globals()[name] = val  # cache: subsequent accesses skip the lookup entirely
    return val


def __dir__():
    return sorted(set(globals()) | set(_LAZY_IMPORTS))
