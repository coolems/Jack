"""Generic self-unpacking runtime bootstrap for content-generating tools.

WHY THIS MODULE EXISTS
Heavy generation tools (image, and later video) must ship MINIMAL from the SERVER: just
their tool .py plus a tiny tool_manifest.json. The heavy runtime - a dedicated venv with
torch/diffusers-class packages plus the tool's worker script - is created ON THE USER
MACHINE at first use, CLIENT-INTERNAL and never in the user's working folder:

    <CLIENT_DIR>/tools/runtimes/<runtime_name>/
        <worker_filename>   <- written from the embedded WORKER_SOURCE (hash-verified)
        requirements.txt    <- from the manifest 'requirements' list
        .deps_installed     <- marker: pip install already ran for this venv
        venv/               <- dedicated virtual env, REUSED across iterations

ensure_tool_runtime() is idempotent: an existing healthy runtime is detected and reused, so
only the first call pays setup. The worker file is hash-verified before reuse.
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
