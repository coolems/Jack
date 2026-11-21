"""OCR llama-file manifest builder (SERVER side).

Lists the files a CLIENT needs to run OCR locally — llama-server.exe + its flat DLLs +
the GLM-OCR model + mmproj — so the SERVER can provision them to clients that lack them.

Each entry is ``{name, size, sha256}`` where *name* is a posix path RELATIVE to the
server's ``llama_server/`` root (e.g. ``models/glm_ocr/GLM-OCR.Q4_K_M.gguf``). sha256 is
computed lazily and cached (the files are large and stable); it is recomputed only when a
file's size or mtime changes.
"""

import glob
import hashlib
import logging
import os

logger = logging.getLogger("COOLEMS.Provider.Llama.OCRManifest")

# The vision model + projector live in this subfolder of llama_server/models/.
OCR_MODEL_SUBDIR = "glm_ocr"

# name -> (size, sha256, mtime_ns)
_cache: dict = {}


def llama_server_root() -> str:
    """Absolute path of the SERVER's llama_server/ directory."""
    from .path_utils import _resolve_server_root
    return os.path.join(_resolve_server_root(), "llama_server")


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry_for(name: str, abspath: str):
    """Build a manifest entry for one file, or None when it is missing/unreadable."""
    if not os.path.isfile(abspath):
        return None
    try:
        st = os.stat(abspath)
        key = (name, os.path.abspath(abspath))
        cached = _cache.get(key)
        if cached and cached[2] == st.st_mtime_ns and cached[0] == st.st_size:
            size, digest, _mtime = cached
        else:
            size = st.st_size
            digest = _sha256_of(abspath)
            _cache[key] = (size, digest, st.st_mtime_ns)
        return {"name": name, "size": size, "sha256": digest}
    except OSError as e:
        logger.warning("[OCRManifest] cannot stat %s: %s", abspath, e)
        return None


def build_ocr_manifest() -> list:
    """The full list of files a client OCR instance needs (relative to llama_server/)."""
    root = llama_server_root()
    entries = []

    # 1. llama-server.exe + every flat DLL it links (ggml*, llama-common, CUDA, openmp).
    exe = os.path.join(root, "llama-server.exe")
    entries.append(_entry_for("llama-server.exe", exe))
    for p in sorted(glob.glob(os.path.join(root, "*.dll"))):
        entries.append(_entry_for(os.path.basename(p), p))

    # 2. GLM-OCR vision model + projector.
    model_dir = os.path.join(root, "models", OCR_MODEL_SUBDIR)
    for p in sorted(glob.glob(os.path.join(model_dir, "*.gguf"))):
        rel = f"models/{OCR_MODEL_SUBDIR}/{os.path.basename(p)}"
        entries.append(_entry_for(rel, p))

    return [e for e in entries if e]


def resolve_ocr_file(name: str):
    """Absolute server path for a manifest *name*, or None when outside the allowed set.

    Safety: only names that appear in the manifest resolve, and the result is required to
    stay under the llama_server/ root (no path traversal).
    """
    if not name or not isinstance(name, str):
        return None
    allowed = {e["name"] for e in build_ocr_manifest()}
    if name not in allowed:
        return None
    root = llama_server_root()
    target = os.path.normpath(os.path.join(root, name.replace("/", os.sep)))
    try:
        if os.path.commonpath([os.path.abspath(root), os.path.abspath(target)]) != os.path.abspath(root):
            return None
    except ValueError:
        return None
    return target if os.path.isfile(target) else None
