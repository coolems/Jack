"""Console output, interactive ask helpers and tiny file helpers."""

import json
import os
import shutil
import sys


def _utf8_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def line(char: str = "-", width: int = 74) -> str:
    return char * width


def header(title: str) -> None:
    print()
    print(line("="))
    print(f"  {title}")
    print(line("="))


def info(msg: str) -> None:
    print(f"  [i] {msg}")


def ok(msg: str) -> None:
    print(f"  [+] {msg}")


def warn(msg: str) -> None:
    print(f"  [!] {msg}")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def ask(question: str, default: str = "") -> str:
    """Ask a free-text question; empty input returns *default*."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {question}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def ask_yn(question: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    try:
        val = input(f"  {question} ({d}): ").strip().lower()
    except EOFError:
        return default_yes
    if not val:
        return default_yes
    return val in ("y", "yes")


def ask_download_now(size: int, what: str = "model") -> bool:
    """Ask whether to download the (big) *what* file NOW or skip it for later.

    Returns True = download now, False = skip (every other init step runs as usual).
    """
    print()
    info(f"The chosen {what} file is ~{human(size)}.")
    while True:
        val = ask(
            f"Download the {what} physically NOW, or SKIP it for later?\n"
            "      [D] Download now  (recommended - everything works out of the box)\n"
            "      [S] Skip for later  (init still does ALL other setup; re-run this init\n"
            "          any time to fetch the model - downloads resume where they stopped)",
            default="D").strip().lower()
        if val in ("d", "download"):
            return True
        if val in ("s", "skip"):
            return False
        warn("Please enter D (download now) or S (skip for later)")


def backup_once(path: str, stamp: str) -> None:
    """Copy *path* to <name>.bak-<stamp> once (idempotent within one run)."""
    bak = f"{path}.bak-{stamp}"
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copy2(path, bak)


def write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
