"""The step-by-step init flow (main entry)."""

import os
import secrets
import shutil
import sys

from .constants import HF_MMPROJ_FILE, HF_MODEL_REPO, LLAMA_BUILD, MMPROJ_SIZE, MIN_PYTHON, OCR_FILES
from .errors import InitError
from .gpu import detect_gpu
from .llama import install_llama_libs
from .models import download_model_files, select_model
from .paths import ROOT, _read_client_ui_port
from .ui import ask, ask_download_now, ask_yn, header, human, info, line, ok, warn, _utf8_console
from .wiring import wire_configs


def _print_browser_steps(num: int, ui_url: str, where: str = "") -> None:
    """Standard browser login steps (open UI -> Settings email/key -> CONNECT -> refresh)."""
    info(f"  {num}. Open {ui_url} in a browser{where}.")
    info("     Expect NO model loaded yet - that is normal until you log in:")
    info("         open Settings -> enter the EMAIL + API KEY printed above -> press CONNECT.")
    info(f"  {num + 1}. Refresh the page (F5) - the model list now appears and you can start chatting.")


def _print_portable_client_block() -> None:
    """The [a]/[b] options for running this SERVER's client on ANOTHER PC.

    The CLIENT folder is self-contained: its API key file, settings.json, UI certs and the
    ZZZ_CLIENT.bat venv bootstrap all live INSIDE it (see working_root.py - the tree is
    designed to be moved; a stale .working_root.json self-heals on that PC).
    """
    info("     [a] FASTEST (no init there): copy the whole CLIENT\\ folder from THIS repo to that")
    info("         PC - it already contains its API key file + settings. That PC only needs Python >= 3.10,")
    info(f"         then just run ZZZ_CLIENT.bat there (first run builds its venv).")
    info("     [b] Or clone the repo on that PC and run ZZZ_initial_init.bat there too.")
    info("     File tools on that PC work against its own 'Working Folder' (settable in the UI).")


def _print_ca_note() -> None:
    """One-time browser trust for a SECOND PC when init generated the UI certificates."""
    info("     If UI certificates were generated, import CLIENT\\certs\\ca.crt ONCE on that PC")
    info(f"         (Trusted Root CAs) so its browser trusts https://127.0.0.1:{_read_client_ui_port()}/ automatically.")


def main() -> int:
    _utf8_console()
    if sys.version_info < MIN_PYTHON:
        print(f"\n  [ERROR] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (you have "
              f"{sys.version_info.major}.{sys.version_info.minor}).")
        print("  Install it from https://www.python.org/downloads/ and re-run.\n")
        return 2

    print()
    print(line("="))
    print("   COOLEMS - ZZZ initial init (fresh-clone setup)")
    print(f"   Repo root: {ROOT}")
    print(line("="))

    if not os.path.isdir(os.path.join(ROOT, "llama_server")) or \
       not os.path.isfile(os.path.join(ROOT, "config", "config.py")):
        raise InitError("This does not look like the COOLEMS repo root. Run ZZZ_initial_init.bat from the cloned repo.")

    try:
        # ---- STEP 1: GPU detection -------------------------------------------
        header("STEP 1 - HARDWARE DETECTION")
        gpu_name, vram_mb = detect_gpu()
        if gpu_name:
            ok(f"GPU: {gpu_name} ({human(vram_mb)} VRAM)")
        else:
            warn("No NVIDIA GPU found via nvidia-smi. Continuing in CPU-only mode (slow).")

        # ---- STEP 2: model menu ----------------------------------------------
        quant_entry, tier = select_model(gpu_name, vram_mb)
        q_label, _q_fname, q_size = quant_entry
        ok(f"Model choice: Qwen3.8-27B-{q_label} ({human(q_size)}) - context target {tier['ctx']} tokens")

        # ---- STEP 2a: download the model now, or skip it for later ------------
        header("STEP 2a - DOWNLOAD NOW OR SKIP FOR LATER")
        download_now = ask_download_now(q_size)
        if download_now:
            ok(f"Will download the model file now (~{human(q_size)})")
        else:
            warn("Model download SKIPPED for later - everything else is set up as usual. "
                 "Re-run ZZZ_initial_init.bat any time to fetch it (resumable).")

        # ---- STEP 2b: vision projector (mmproj) now, or skip for later --------
        header("STEP 2b - VISION PROJECTOR (MMPROJ): DOWNLOAD NOW OR SKIP FOR LATER")
        info(f"Vision projector file: {HF_MMPROJ_FILE} from huggingface.co/{HF_MODEL_REPO}")
        mmproj_now = ask_download_now(MMPROJ_SIZE, what="vision projector (mmproj)")
        if mmproj_now:
            ok(f"Will download the vision projector now (~{human(MMPROJ_SIZE)})")
        else:
            warn("Vision projector SKIPPED for later - image input will not work until it is fetched. "
                 "Re-run ZZZ_initial_init.bat any time to fetch it (resumable).")

        # ---- STEP 2c: optional downloads + disk pre-check ---------------------
        header("STEP 2c - OPTIONAL DOWNLOADS & DISK CHECK")
        want_mtp = ask_yn(
            "Enable MTP speculative decoding (>2x generation speedup)?\n"
            "      (no extra download - the head is already inside unsloth's GGUF; the file just gets\n"
            "       renamed with 'MTP' so our code auto-enables it. On very small GPUs say N)",
            default_yes=True)
        want_ocr = ask_yn(
            f"Download the GLM-OCR model (+{human(sum(s for _f, s in OCR_FILES))}, needed by the transcribe_image tool)?",
            default_yes=True)

        need = ((q_size if download_now else 0) + (MMPROJ_SIZE if mmproj_now else 0)
                + sum(s for _f, s in OCR_FILES if want_ocr))
        need = int(need * 1.25) + (1_500_000_000 if not os.path.exists(os.path.join(ROOT, "llama_server", "llama-server.exe")) else 0)
        free = shutil.disk_usage(ROOT).free
        if free < need:
            raise InitError(
                f"Not enough disk space: need ~{human(need)} free on the drive holding this repo, "
                f"only {human(free)} available. Free up space and re-run init (downloads resume where they stopped)."
            )
        ok(f"Disk space OK ({human(free)} free, ~{human(need)} needed)")

        # ---- llama.cpp binaries ----------------------------------------
        header("STEP 3 - LLAMA.CPP BINARIES")
        install_llama_libs(gpu_name)

        # ---- model + optional file downloads (or skipped for later) ---------------------------
        if download_now:
            header("STEP 4 - MODEL DOWNLOADS")
        else:
            header("STEP 4 - MODEL DOWNLOADS (SKIPPED FOR LATER)")
        ctx = {
            "root": ROOT,
            "quant_entry": quant_entry,
            "tier": tier,
            "want_mtp": want_mtp,
            "want_ocr": want_ocr,
            "download_now": download_now,
            "mmproj_now": mmproj_now,
            "models_root": os.path.join(ROOT, "llama_server", "models"),
            "model_folder": os.path.join(ROOT, "llama_server", "models", f"qwen38-27b-{q_label.lower()}"),
        }
        download_model_files(ctx)

        # ---- credentials + config wiring --------------------------------------------
        header("STEP 5 - CREDENTIALS & CONFIG WIRING")
        email = ask("Email address for the admin API key (used to log into the UI)", default="admin@localhost")
        api_key = secrets.token_hex(32)
        ctx.update({"email": email, "api_key": api_key})
        ctx["same_machine_client"] = ask_yn("Will the CLIENT run on THIS same PC?", default_yes=True)
        wire_configs(ctx)

        # ---- FINAL REPORT ---------------------------------------------------------
        header("INIT COMPLETE")
        print()
        ok(f"llama.cpp {LLAMA_BUILD}:      llama_server\\llama-server.exe")
        if ctx.get("download_now", True):
            ok(f"Model:                        {ctx['model_path']}")
        else:
            warn(f"Model (SKIPPED for later):  {ctx['model_path']}")
        if want_mtp:
            ok("MTP spec decoding ENABLED - head is built into the model file (no extra weights loaded)")
        if ctx.get("mmproj_now", True):
            ok(f"Vision projector (mmproj):  {os.path.join(ctx['model_folder'], HF_MMPROJ_FILE)}")
        else:
            warn(f"Vision projector (SKIPPED for later): {ctx['model_folder']}\\{HF_MMPROJ_FILE}")
        if want_ocr:
            ok(f"OCR model:                  {ctx['ocr_folder']}")
        print()
        info("API key for UI login (saved in config/.api_keys.json AND CLIENT/config/.api_client_keys.json - plaintext, single row):")
        print()
        print(f"      EMAIL:  {email}")
        print(f"      KEY:    {api_key}")
        print()
        info(f"Context window set to {tier['ctx']} tokens (config/config.py) - matches your GPU tier.")
        if ctx.get("ui_certs_ok"):
            print()
            info(f"UI certificates: CLIENT will serve https://127.0.0.1:{_read_client_ui_port()}/ with a locally trusted CA.")
            info("   One-time import of the CA (CLIENT\\certs\\ca.crt) was printed above - do it once per PC.")
        print()
        ui_port = _read_client_ui_port()
        ui_url = f"https://127.0.0.1:{ui_port}/" if ctx.get("ui_certs_ok") else f"http://127.0.0.1:{ui_port}/"
        info("Next steps:")
        step = 1
        if not ctx.get("download_now", True):
            info(f"  {step}. Re-run ZZZ_initial_init.bat to download the model file you skipped")
            info("         (it picks up where it left off - everything else is already set up).")
            step += 1
        if not ctx.get("mmproj_now", True):
            info(f"  {step}. Re-run ZZZ_initial_init.bat to download the vision projector (mmproj) you skipped")
            info("         (image input stays disabled until it is in place - everything else already works).")
            step += 1
        info(f"  {step}. Run ZZZ_SERVER.bat   (first run creates the venv + installs deps - takes a few minutes)")
        if ctx["same_machine_client"]:
            info(f"  {step + 1}. Run CLIENT\\ZZZ_CLIENT.bat")
            _print_browser_steps(step + 2, ui_url)
            print()
            info("Want to use this SERVER from ANOTHER PC too? The CLIENT folder is portable:")
            _print_portable_client_block()
            info("   Both PCs must be on the same network - on first boot the client finds this")
            info("   SERVER's LAN IP automatically (no address editing needed). Then log in on that PC")
            info(f"   exactly like above: {ui_url} -> Settings -> SAME EMAIL + API KEY -> CONNECT -> refresh.")
            if ctx.get("ui_certs_ok"):
                _print_ca_note()
        else:
            info(f"  {step + 1}. On the OTHER PC, get the client running there (the CLIENT folder is portable):")
            _print_portable_client_block()
            info("     Both PCs must be on the same network - on first boot the client finds this")
            info("     SERVER's LAN IP automatically (no address editing needed).")
            _print_browser_steps(step + 2, ui_url, where=" ON THAT PC")
            if ctx.get("ui_certs_ok"):
                _print_ca_note()

        print()
        return 0

    except InitError as e:
        print()
        warn(str(e))
        return 1
    except KeyboardInterrupt:
        print("\n\n  Aborted by user. Partial downloads are kept - re-run init to resume.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
