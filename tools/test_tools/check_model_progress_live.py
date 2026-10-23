"""End-to-end check for the LIVE model-download progress chain (2026-10-23).

WHAT IT PROVES
==============
The UI is only as "realtime" as the state file it polls. This check runs the REAL
pipeline with a REAL child process and no fake pipes:

    prep child (JSON lines + flush, like huggingface_hub)
        -> tools/tool_bootstrap/streaming.run_streaming  (live delivery)
        -> generate_image._ensure_model_cached           (merge + lazy stage write)
        -> <runtime>/.setup_progress.json                (what GET /api/setup/status serves)

and samples the state file from a watcher thread while the download is "running", so a
regression to buffered/batched delivery fails here instead of on a user's machine.

Run: python tools/test_tools/check_model_progress_live.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOTAL_A, TOTAL_B, TOTAL_C = 1_000_000, 500_000, 250_000

# A stand-in for the hf pre-flight: one manifest, then 10 throttled per-file events
# (~0.3 s apart, exactly like ProgressTqdm's 0.5 s throttle), then finalize. The child
# records its OWN start epoch so the test measures delivery latency from the child's
# first line, not from process spawn (a cold Python start alone can take seconds).
PREP_CHILD = """
import json, sys, time
open(%r, "w").write(str(time.time()))
files = [{"filename": "a.bin", "size": %d},
         {"filename": "b.bin", "size": %d},
         {"filename": "c.bin", "size": %d}]
print(json.dumps({"type": "manifest", "repo_index": 1, "repo_count": 1, "files": files}), flush=True)
print(json.dumps({"type": "count", "done": 0, "total": 3}), flush=True)
for i in range(1, 11):
    time.sleep(0.3)
    print(json.dumps({"type": "file", "filename": "a.bin", "size": %d,
                      "downloaded": i * 100000, "speed_bps": 333333.3, "eta_sec": 3.0}), flush=True)
print(json.dumps({"type": "file", "filename": "a.bin", "size": %d, "downloaded": %d,
                  "speed_bps": 300000.0, "eta_sec": 0.0, "done": True}), flush=True)
time.sleep(0.3)
print(json.dumps({"type": "count", "done": 3, "total": 3}), flush=True)
print("MODEL_READY: fake/repo: /fake/cache", flush=True)
"""


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    gi = _load("jack_gi_progress_check", "tools/generate_tool/generate_image.py")
    sp = _load("jack_setup_progress_check", "tools/tool_bootstrap/setup_progress.py")

    failures = []

    def check(label, cond, detail=""):
        print(("PASS  " if cond else "FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    tmp = tempfile.mkdtemp(prefix="jack_model_live_")
    try:
        rt_dir = os.path.join(tmp, "generate_tool")
        os.makedirs(rt_dir, exist_ok=True)
        rt = {"runtime_dir": rt_dir, "python_exe": sys.executable, "worker_script": "unused"}
        state_path = os.path.join(rt_dir, sp.PROGRESS_FILENAME)
        child_ts_path = os.path.join(tmp, "child_start_epoch.txt")
        prep_script = PREP_CHILD % (child_ts_path, TOTAL_A, TOTAL_B, TOTAL_C, TOTAL_A, TOTAL_A, TOTAL_A)

        samples = []          # (abs_time, downloaded, current_file, speed_bps)
        seen_names = set()
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        stage = (json.load(f).get("stages") or {}).get("model") or {}
                    if stage:
                        samples.append((time.time(),
                                        int(stage.get("downloaded") or 0),
                                        stage.get("current_file"),
                                        stage.get("speed_bps")))
                        if stage.get("current_file"):
                            seen_names.add(stage["current_file"])
                except Exception:
                    pass  # missing/torn file - the writer is atomic, so just retry
                time.sleep(0.1)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()

        # Wire the REAL collaborators; only the child payload is substituted (no network).
        gi._model_prep_script = lambda ids=None: prep_script
        gi._setup_progress = lambda rt: sp.SetupProgress(rt["runtime_dir"])
        gi._migrate_legacy_model_cache = lambda rt: None
        gi._tool_env = lambda rt: None

        t_start = time.time()
        ok = gi._ensure_model_cached(rt, ["fake/repo"])
        elapsed = time.time() - t_start
        stop.set()
        watcher.join(timeout=3)

        check("E1 pre-flight reports success", ok is True, str(ok))
        check("E2 child ran at its real pacing (10 x 0.3 s), not instant",
              elapsed >= 2.5, f"{elapsed:.2f}s")

        dl_values = [d for (_, d, _, _) in samples]
        distinct = sorted(set(dl_values))
        check("E3 state file was polled while the download ran", len(samples) >= 5,
              f"{len(samples)} samples")
        check("E4 bytes advanced LIVE through many separate writes (not one final dump)",
              len(distinct) >= 4, f"distinct {distinct}")
        # Delivery latency measured from the CHILD's own first line (process startup is
        # excluded: a cold interpreter start alone can take seconds on Windows).
        try:
            child_start = float(open(child_ts_path, encoding="utf-8").read().strip())
        except Exception:
            child_start = None
        lag = (samples[0][0] - child_start) if (samples and child_start) else None
        check("E5 first live sample reached the state file within ~2 s of the child's first line",
              lag is not None and lag <= 2.0, f"lag={lag if lag is None else round(lag, 2)}s")
        check("E6 Current line named the active file while it downloaded",
              "1:a.bin" in seen_names, f"seen {sorted(seen_names)}")
        check("E7 speed reached the state file", any((s or 0) > 0 for (_, _, _, s) in samples),
              str(samples[:3]))

        with open(state_path, "r", encoding="utf-8") as f:
            final = (json.load(f).get("stages") or {}).get("model") or {}
        total = TOTAL_A + TOTAL_B + TOTAL_C
        check("E8 final stage status is done", final.get("status") == "done", str(final.get("status")))
        check("E9 final Total line is 100% (ghost rows counted after their sweep)",
              int(final.get("downloaded") or 0) == total and int(final.get("total_size") or 0) == total,
              f"{final.get('downloaded')} / {final.get('total_size')} (want {total})")
        check("E10 final files done/total complete",
              int(final.get("files_done") or 0) == 3 and int(final.get("files_total") or 0) == 3,
              f"{final.get('files_done')}/{final.get('files_total')}")
        check("E11 Current line cleared at completion", final.get("current_file") is None,
              str(final.get("current_file")))
        check("E12 repo context exposed for the 'Repo X of Y' UI line",
              int(final.get("repo_index") or 0) == 1 and int(final.get("repo_count") or 0) == 1,
              f"{final.get('repo_index')}/{final.get('repo_count')}")

        # ---------------------------------------------------------------- M) two repos
        # The Qwen high tier downloads Qwen-Image AND Qwen-Image-Edit back to back, and
        # both repos contain paths like 'transformer/diffusion_pytorch_model.safetensors'.
        # The rows must stay distinct, repo 2 must start its own 0->100% arc (not inherit
        # repo 1's bytes), and the Current line must follow repo 2.
        state = {"status": "running", "current_file": None}
        gi._merge_model_progress(state, {"type": "manifest", "repo_index": 1, "repo_count": 2,
                                         "files": [{"filename": "a.bin", "size": 100},
                                                   {"filename": "b.bin", "size": 100}]})
        gi._merge_model_progress(state, {"type": "count", "done": 2, "total": 2})
        gi._merge_model_progress(state, {"type": "file", "filename": "a.bin", "size": 100,
                                         "downloaded": 100, "done": True})
        gi._merge_model_progress(state, {"type": "file", "filename": "b.bin", "size": 100,
                                         "downloaded": 100, "done": True})
        check("M1 repo 1 arc completes at 100%",
              int(state.get("downloaded") or 0) == 200 and int(state.get("total_size") or 0) == 200,
              f"{state.get('downloaded')}/{state.get('total_size')}")

        gi._merge_model_progress(state, {"type": "manifest", "repo_index": 2, "repo_count": 2,
                                         "files": [{"filename": "a.bin", "size": 100},
                                                   {"filename": "b.bin", "size": 100}]})
        rows = state.get("files") or {}
        check("M2 same path in both repos stays two distinct rows",
              "1:a.bin" in rows and "2:a.bin" in rows and len(rows) == 4, f"{sorted(rows)}")
        check("M3 repo 2 starts its own arc at 0 bytes (no phantom 100%)",
              int(state.get("downloaded") or 0) == 0 and state.get("repo_index") == 2,
              f"downloaded={state.get('downloaded')} repo={state.get('repo_index')}")
        check("M4 repo 1 rows keep their bytes / done flags",
              rows["1:a.bin"].get("done") and int(rows["1:a.bin"].get("downloaded") or 0) == 100)

        gi._merge_model_progress(state, {"type": "file", "filename": "a.bin", "size": 100,
                                         "downloaded": 40, "speed_bps": 1000.0})
        check("M5 repo 2 live bytes are its own, not repo 1's",
              int(state.get("downloaded") or 0) == 40, f"{state.get('downloaded')}")
        check("M6 Current line follows the repo 2 row", state.get("current_file") == "2:a.bin",
              str(state.get("current_file")))
        check("M7 stage Total line still 100% of repo 2's own size",
              int(state.get("total_size") or 0) == 200, f"{state.get('total_size')}")

        # ------------------------------------------- X) xet transfer-only progress
        # huggingface_hub 1.33 + hf_xet reports a downloading file through
        # update_transfer() every ~0.1 s and calls update() only when the file is fully
        # assembled (measured on a real 253 MB download: 380 transfer calls, 2 update
        # calls at the very end). If the shim ignores update_transfer - as it did - the
        # row produces no event for the entire file and the UI sits on the previous
        # finished file for minutes. This pins the live behaviour.
        import contextlib
        import io
        ns = {"_sys": sys, "_json": json, "_time": time}
        exec(compile(gi._PROGRESS_TQDM_SOURCE, "<progress_tqdm>", "exec"), ns)
        P = ns["ProgressTqdm"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bar = P(desc="vae/model.safetensors: reconstructing file", total=1000)
            bar.update_transfer(100)          # t=0  -> emits immediately
            time.sleep(0.6)
            bar.update_transfer(150)          # t=.6 -> emits (throttle allows)
            time.sleep(0.6)
            bar.update(400)                   # reconstruction catches up
            bar.close()                       # final done event
        evs = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        dl = [e.get("downloaded") for e in evs if e.get("type") == "file"]
        check("X1 transfer-only stream emits multiple live events", len(dl) >= 3, f"{dl}")
        check("X2 live byte counts increase (network bytes drive the row)",
              dl[:3] == [100, 250, 400], f"{dl}")
        check("X3 events carry per-file speed + ETA",
              any(e.get("speed_bps") for e in evs) and any(e.get("eta_sec") for e in evs),
              str(evs))
        check("X4 final event marks the file done at its full size (close snaps to total)",
              bool(evs) and evs[-1].get("done") is True and evs[-1].get("downloaded") == 1000,
              str(evs[-1] if evs else None))
        check("X5 no event exceeds the file size", all((d or 0) <= 1000 for d in dl), f"{dl}")

        # ------------------------------------------------ N) file-name matching
        # Qwen repos carry 'config.json' in four components. hf's xet bar used to report the
        # bare basename, so a suffix match could land on the wrong row (and the UI would
        # show/PROGRESS the wrong file). The full repo path is now passed into the bar; a
        # basename-only event is disambiguated by its size, and if that is still ambiguous
        # it must stay UNKNOWN instead of being attributed to the wrong file.
        nm = {"current_repo": 1, "files": {
            "1:text_encoder/config.json": {"filename": "text_encoder/config.json", "size": 3217, "_repo": 1},
            "1:transformer/config.json": {"filename": "transformer/config.json", "size": 339, "_repo": 1},
            "1:vae/config.json": {"filename": "vae/config.json", "size": 730, "_repo": 1},
        }}
        key, exact = gi._match_known_file(nm, "vae/config.json", 730)
        check("N1 full repo path matches exactly", key == "1:vae/config.json" and exact is True, key)
        key, exact = gi._match_known_file(nm, "config.json", 339)
        check("N2 bare basename resolved by its size", key == "1:transformer/config.json", key)
        key, exact = gi._match_known_file(nm, "config.json", 999)
        check("N3 ambiguous basename stays unknown (never a wrong row)",
              key == "config.json" and exact is False, f"{key} {exact}")

        print()
        if failures:
            print(f"{len(failures)} CHECK(S) FAILED")
            return 1
        print("ALL CHECKS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
