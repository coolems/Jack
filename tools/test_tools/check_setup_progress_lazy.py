"""Behavior check for the 2026-09-24 'show setup only when needed' fix.

Simulates the .setup_progress.json state file through the REAL code:

  A) fully-cached machine (venv + deps exist, model cache complete):
     ensure_tool_runtime must NOT write any venv/pip row and must not spawn anything;
     _ensure_model_cached must not start a 'model' stage -> UI gets an empty stages dict
     and drops its card.
  B) fresh machine: the model stage appears only on the first REAL progress event, then
     is finalized done (live download still fully visible).

Run: python tools/test_tools/check_setup_progress_lazy.py
"""
import importlib.util
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)  # so `from config import ...` inside generate_image.py resolves

    # Deterministic environment: never let the REAL client runtimes root leak into this check.
    os.environ.pop("COOLEMS_CLIENT_ROOT", None)

    tb = _load("tools.tool_bootstrap", "tools/tool_bootstrap.py")

    failures = []

    def check(label, cond):
        print(("PASS  " if cond else "FAIL  ") + label)
        if not cond:
            failures.append(label)

    tmp = tempfile.mkdtemp(prefix="jack_setup_lazy_")
    try:
        # -------------------------------------------------------------- A) cached
        rt_dir = os.path.join(tmp, "generate_tool")
        venv_py = os.path.join(rt_dir, "venv", "Scripts", "python.exe")  # nt layout on win
        if os.name != "nt":
            venv_py = os.path.join(rt_dir, "venv", "bin", "python")
        os.makedirs(os.path.dirname(venv_py), exist_ok=True)
        with open(venv_py, "w", encoding="utf-8") as f:
            f.write("fake python marker")  # file existence is what bootstrap checks
        reqs_text = "torch==2.9.1\n"
        reqs_path = os.path.join(rt_dir, "requirements.txt")
        with open(reqs_path, "w", encoding="utf-8") as f:
            f.write(reqs_text)
        marker = os.path.join(rt_dir, ".deps_installed")
        with open(marker, "w", encoding="utf-8") as f:
            f.write(tb.sha256_prefix(reqs_text))

        prog_path = os.path.join(rt_dir, tb.PROGRESS_FILENAME)
        if os.path.isfile(prog_path):
            os.remove(prog_path)

        # Point the bootstrap at our synthetic runtime dir (deterministic - never touch the
        # real client runtimes) and disable the legacy migration.
        tb.resolve_runtime_dir = lambda name: rt_dir
        tb._migrate_legacy_runtime = lambda runtime_dir: None

        # A fully-cached run must never spawn a subprocess (venv + pip both skipped). If it
        # DID try to create/install anything, this sentinel raises and the check fails.
        def _no_spawn(cmd, timeout=0, env=None, input_text=None, on_line=None):
            raise AssertionError(f"cached run tried to spawn: {cmd}")

        tb.run_streaming = _no_spawn  # module-global lookup inside ensure_tool_runtime

        manifest = {"runtime_name": "generate_tool", "requirements": ["torch==2.9.1"]}
        try:
            tb.ensure_tool_runtime(manifest, worker_source=None)
            spawned = False
        except AssertionError as e:
            spawned = True
            print("  (spawn attempt:", str(e)[:120], ")")

        check("A0 cached run spawns no subprocess at all (no venv/pip work)", not spawned)

        if os.path.isfile(prog_path):
            with open(prog_path, encoding="utf-8") as f:
                state = json.load(f)
            stages = state.get("stages", {})
        else:
            stages = {}
        check("A1 cached run writes NO venv row", "venv" not in stages)
        check("A2 cached run writes NO pip row", "pip" not in stages)

        # Now simulate the model pre-flight of a fully-cached machine: the prep script emits
        # only 'MODEL_READY' (no JSON progress events). Drive _ensure_model_cached's exact
        # write pattern by exec-ing the REAL function with stubbed collaborators.
        gi = _load("tools.generate_tool.generate_image", "tools/generate_tool/generate_image.py")

        calls = {"reset": 0, "set_stage": []}

        class FakeProgress:
            def reset(self, stages=None):
                calls["reset"] += 1

            def set_stage(self, stage, **fields):
                calls["set_stage"].append((stage, dict(fields)))

        gi._setup_progress = lambda rt: FakeProgress()
        gi.run_streaming = lambda cmd, timeout=0, env=None, input_text=None, on_line=None: (
            0, ["MODEL_READY: fake-model: /cache"], "")  # fully cached: no progress events
        gi._migrate_legacy_model_cache = lambda rt: None

        rt = {"runtime_dir": rt_dir, "python_exe": venv_py,
              "worker_script": os.path.join(rt_dir, "worker.py")}
        ok = gi._ensure_model_cached(rt, "fake-model")
        check("A3 cached model pre-flight returns True", ok is True)
        check("A4 cached model pre-flight writes NO stage rows (UI shows nothing)",
              calls["set_stage"] == [])

        # -------------------------------------------------------------- B) fresh
        calls2 = {"reset": 0, "set_stage": []}

        class FakeProgress2:
            def reset(self, stages=None):
                calls2["reset"] += 1

            def set_stage(self, stage, **fields):
                calls2["set_stage"].append((stage, dict(fields)))

        gi._setup_progress = lambda rt: FakeProgress2()

        events = [
            json.dumps({"type": "manifest", "files": [{"filename": "a.safetensors", "size": 100}]}),
            json.dumps({"type": "file", "filename": "a.safetensors", "size": 100, "downloaded": 50}),
        ]

        def fake_stream(cmd, timeout=0, env=None, input_text=None, on_line=None):
            for line in events:
                if on_line:
                    on_line(line)
            return 0, ["MODEL_READY: fake-model: /cache"], ""

        gi.run_streaming = fake_stream
        ok = gi._ensure_model_cached(rt, "fake-model")
        model_rows = [f for (s, f) in calls2["set_stage"] if s == "model"]
        check("B1 fresh model pre-flight returns True", ok is True)
        check("B2 first write resets the previous attempt's rows once", calls2["reset"] == 1)
        check("B3 model stage written (running on first real event)",
              any(f.get("status") == "running" for f in model_rows))
        check("B4 model stage finalized done",
              bool(model_rows) and model_rows[-1].get("status") == "done")

        print()
        if failures:
            print(f"{len(failures)} CHECK(S) FAILED")
            return 1
        print("ALL CHECKS PASSED")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
