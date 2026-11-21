"""Client-local OCR: run a dedicated llama-server (GLM-OCR + mmproj) on THIS machine.

The CLIENT runs OCR locally instead of delegating to the SERVER. This module owns the
dedicated llama-server process: find the files under <CLIENT>/llama_server/, launch on a
side port, health-check, transcribe, and shut down. Lifecycle is strictly
start -> use -> stop when done: the provider calls shutdown_local_ocr_server() after every
transcription (the exit hook is only a safety net for abnormal termination).

The functions here are SYNCHRONOUS (subprocess + urllib) so they can be driven from
asyncio.to_thread() by the async provider, and so the launch/health logic is testable
without an event loop. Files are provisioned by ocr_files.py before launch.

The exact GLM-OCR call format (prompt "Text Recognition:", temperature 0, stream false)
must stay unchanged — it is the model's contract.
"""

import base64
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .ocr_files import client_llama_root

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient.OCRLocal")

# ---- local OCR config (client-side; mirrors the SERVER's dedicated OCR instance) ----
OCR_SERVER_PORT = 5010
OCR_CTX_SIZE = 32768
OCR_MAX_TOKENS = 2048
OCR_TIMEOUT = 240            # image encoding can take ~2 min on CPU
OCR_PROMPT = "Text Recognition:"
# Health-check window is sized from the files actually being loaded, at a conservative
# 4 MiB/s worst case, with a floor + startup buffer. The model + mmproj (~1 GB here)
# then get ~5 min to come up on a slow CPU instead of a fixed 120 s.
OCR_LOAD_RATE_BPS = 4 * 1024 * 1024
OCR_HEALTH_MIN_WAIT_SEC = 120
OCR_HEALTH_BUFFER_SEC = 60
OCR_HEALTH_POLL_SEC = 2
OCR_MODEL_SUBDIR = "glm_ocr"

_lock = threading.Lock()
_process = None
_last_model_path = None


def _base_url() -> str:
    return f"http://127.0.0.1:{OCR_SERVER_PORT}"


def _health_wait_seconds(model_path: str, mmproj_path: str) -> int:
    """Size the health-check window from the files being loaded (4 MiB/s worst case).

    Returns a floor (OCR_HEALTH_MIN_WAIT_SEC) for tiny/fast loads, or the load time of
    the model + mmproj plus a startup buffer for CUDA/DLL init.
    """
    total = 0
    for p in (model_path, mmproj_path):
        if p and os.path.isfile(p):
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
    load_sec = (total + OCR_LOAD_RATE_BPS - 1) // OCR_LOAD_RATE_BPS
    return max(OCR_HEALTH_MIN_WAIT_SEC, load_sec + OCR_HEALTH_BUFFER_SEC)


def _image_mime_from_base64(image_base64: str) -> str:
    """Sniff the image format from its magic bytes and return the MIME type.

    llama-server keys the vision pipeline off the bytes, but the data URL must carry the
    right MIME for any image (JPEG/PNG/GIF/BMP/WEBP) - never a hardcoded image/jpeg.
    """
    if not image_base64:
        return "image/jpeg"
    try:
        head = base64.b64decode(image_base64[:32])
    except Exception:
        return "image/jpeg"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _find_llama_exe():
    exe = os.path.join(client_llama_root(), "llama-server.exe")
    return exe if os.path.isfile(exe) else None


def _find_model_and_mmproj():
    """Return (model_path, mmproj_path) from <CLIENT>/llama_server/models/glm_ocr/."""
    model_dir = os.path.join(client_llama_root(), "models", OCR_MODEL_SUBDIR)
    if not os.path.isdir(model_dir):
        return None, None
    model = mmproj = None
    for name in sorted(os.listdir(model_dir)):
        low = name.lower()
        if not low.endswith(".gguf"):
            continue
        p = os.path.join(model_dir, name)
        if "mmproj" in low or "projector" in low:
            if mmproj is None:
                mmproj = p
        elif model is None:
            model = p
    return model, mmproj


def _is_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _terminate(proc) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _gpu_layers() -> int:
    try:
        return int(os.environ.get("COOLEMS_OCR_GPU_LAYERS", "0"))
    except ValueError:
        return 0


def ensure_local_ocr_server():
    """Launch (or reuse) the local OCR llama-server. Returns (ok, base_url_or_message)."""
    global _process, _last_model_path

    with _lock:
        proc = _process

    # Reuse a warm, healthy process.
    if proc is not None and proc.poll() is None and _is_healthy(OCR_SERVER_PORT):
        return True, _base_url()

    exe = _find_llama_exe()
    model, mmproj = _find_model_and_mmproj()
    if not exe:
        return False, "llama-server.exe not found under CLIENT/llama_server/ (provision it from the SERVER)"
    if not model or not mmproj:
        return False, "GLM-OCR model + mmproj not found under CLIENT/llama_server/models/glm_ocr/"

    # Clear a stale process before relaunching on the same port.
    with _lock:
        stale = _process
        _process = None
    if stale is not None:
        _terminate(stale)

    cmd = [
        exe, "--model", model, "--mmproj", mmproj,
        "--host", "127.0.0.1", "--port", str(OCR_SERVER_PORT),
        "--ctx-size", str(OCR_CTX_SIZE),
        "--n-gpu-layers", str(_gpu_layers()),
        "--parallel", "1",
    ]
    log_path = os.path.join(client_llama_root(), "ocr_server.log")
    try:
        logf = open(log_path, "wb")
    except OSError:
        logf = None

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    logger.info("[OCRLocal] launching llama-server for OCR: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=logf or subprocess.DEVNULL, stderr=logf or subprocess.DEVNULL,
            creationflags=creationflags, cwd=os.path.dirname(exe),
        )
    except OSError as e:
        if logf:
            logf.close()
        return False, f"failed to launch llama-server.exe: {e}"

    budget = _health_wait_seconds(model, mmproj)
    attempts = (budget + OCR_HEALTH_POLL_SEC - 1) // OCR_HEALTH_POLL_SEC
    for _ in range(attempts):
        time.sleep(OCR_HEALTH_POLL_SEC)
        if proc.poll() is not None:
            if logf:
                logf.close()
            return False, f"llama-server exited early with code {proc.returncode} (see CLIENT/llama_server/ocr_server.log)"
        if _is_healthy(OCR_SERVER_PORT):
            with _lock:
                _process = proc
                _last_model_path = model
            return True, _base_url()

    _terminate(proc)
    if logf:
        logf.close()
    return False, f"OCR server did not become healthy within {budget}s"


def transcribe_local(base_url: str, image_base64: str) -> str:
    """POST the image to the local OCR llama-server and return the extracted text.

    Raises RuntimeError with a human message on failure (the caller maps it to an
    "ERROR: ..." string).
    """
    mime = _image_mime_from_base64(image_base64)
    payload = {
        "model": os.path.basename(_last_model_path or "GLM-OCR.Q4_K_M.gguf"),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_base64}"}},
            ],
        }],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": OCR_MAX_TOKENS,
    }
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OCR_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"local OCR request failed: {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"local OCR returned invalid JSON: {e}") from e

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"unexpected local OCR response: {e}") from e


def shutdown_local_ocr_server() -> None:
    """Terminate the tracked local OCR process (called on client exit)."""
    global _process, _last_model_path
    with _lock:
        proc, _process = _process, None
        _last_model_path = None
    if proc is not None:
        _terminate(proc)
