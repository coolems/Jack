"""Download engine - resumable streaming with progress + size verification."""

import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile

from .errors import InitError
from .ui import human, info, ok, warn


def _http_get(url: str, resume_from: int = 0) -> tuple[int, int | None, object]:
    """Open *url* (following redirects). Returns (status, content_length, response)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"coolems-init/1.0 (python-urllib)",
        **({"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}),
    })
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        return e.code, None, e
    total = resp.headers.get("Content-Length")
    return resp.status, int(total) if total else None, resp


def download_file(url: str, dest_path: str, expected_size: int | None = None, label: str = "") -> None:
    """Download *url* to *dest_path*. Resumable via <dest>.part; verifies final size.

    Safe to re-run after a network drop - it continues where it left off.
    """
    if os.path.exists(dest_path):
        if expected_size is not None and os.path.getsize(dest_path) == expected_size:
            ok(f"{label or os.path.basename(dest_path)} already present ({human(expected_size)}) - skipped")
            return
        warn(f"Existing {os.path.basename(dest_path)} has wrong size "
             f"({human(os.path.getsize(dest_path))}) - re-downloading from scratch")
        os.remove(dest_path)

    part = dest_path + ".part"
    label = label or os.path.basename(dest_path)
    t0 = time.time()
    attempts = 0
    while True:
        attempts += 1
        have = os.path.getsize(part) if os.path.exists(part) else 0
        status, total, resp = _http_get(url, resume_from=have)

        if status == 206:                       # resumed OK
            mode = "ab"
            got = have
            size = (total or 0) + have          # Content-Length on 206 is the REMAINDER
        elif status == 416 and expected_size is not None and have >= expected_size:
            # Server says "nothing left to send" - our .part is already complete.
            os.replace(part, dest_path)
            ok(f"{label}: partial file was already complete ({human(have)}) - finalized")
            return
        elif status == 200:                     # fresh start (or server ignored Range)
            mode = "wb"                         # truncates any stale .part - correct restart
            got = 0
            size = total
        else:
            raise InitError(f"{label}: HTTP {status} from server - cannot download")

        is_tty = sys.stdout.isatty()
        last_draw = 0.0
        try:
            with open(part, mode) as f:
                while True:
                    chunk = resp.read(1 << 20)   # 1 MiB
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if is_tty and (now - last_draw > 0.3 or (size and got >= size)):
                        last_draw = now
                        if size:
                            pct = 100.0 * got / size
                            spd = got / max(now - t0, 0.001)
                            eta_s = (size - got) / max(spd, 1)
                            print(f"\r  [DL] {label[:34]:<34} {pct:5.1f}%  "
                                  f"{human(got)}/{human(size)}  {human(spd)}/s  ETA {int(eta_s//60):d}:{int(eta_s%60):02d}",
                                  end="", flush=True)
                        else:
                            print(f"\r  [DL] {label[:34]:<34} {human(got)}  "
                                  f"{human(got / max(now - t0, 0.001))}/s", end="", flush=True)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if is_tty:
                print()
            if attempts >= 5:
                raise InitError(
                    f"{label}: download failed after {attempts} tries ({e}). "
                    f"Partial file kept at {part} - fix the network and re-run init to resume."
                ) from e
            wait_s = min(2 ** attempts, 30)
            warn(f"{label}: connection problem ({e}) - retrying in {wait_s}s (attempt {attempts}/5)")
            time.sleep(wait_s)
            continue

        if is_tty:
            print()

        final_size = os.path.getsize(part)
        if size and final_size != size:
            raise InitError(f"{label}: size mismatch after download ({human(final_size)} of {human(size)})")
        if expected_size is not None and final_size != expected_size:
            raise InitError(
                f"{label}: downloaded file is {final_size} bytes but the known-good size is "
                f"{expected_size}. The file was NOT installed - re-run init to retry."
            )
        os.replace(part, dest_path)
        ok(f"{label}: {human(final_size)} in {time.time() - t0:.0f}s")
        return


def extract_flat(zip_path: str, target_dir: str) -> int:
    """Extract a zip FLAT into *target_dir* (strips one nesting level if present).

    Returns number of files written. Refuses to touch paths that would escape the dir.
    """
    os.makedirs(target_dir, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for item in zf.infolist():
            if item.is_dir():
                continue
            name = item.filename.replace("\\", "/")
            # Strip a single leading folder (e.g. "llama-b10441-bin-win-cuda-12.4-x64/llama-server.exe").
            parts = [p for p in name.split("/") if p not in ("", ".", "..")]
            if len(parts) > 1:
                parts = parts[1:]
            if not parts or any(p.startswith("..") for p in parts):
                continue
            dest = os.path.join(target_dir, *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(item) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count
