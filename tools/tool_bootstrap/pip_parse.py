"""Parsing of pip stdout into live progress events for the UI.

Turns pip's streamed lines (Collecting / Downloading / Installing / Successfully installed,
plus its in-place carriage-return progress repaints) into small event dicts that
apply_pip_event() merges into the 'pip' stage state written by SetupProgress.
"""

import os
import re

def _pip_size_to_bytes(text: str):
    """Parse pip's human sizes ('765 MB', '1.2 GB', '512 kB') -> int bytes, or None."""
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]{0,3})\b", text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "B").lower()
    mult = {"": 1, "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
    return int(value * mult.get(unit[:2] if unit.startswith("t") else unit, 1))


def _split_output_lines(raw: str):
    """Split a child's raw stdout into logical lines on BOTH '\n' and '\r'.

    pip redraws its download progress bar in place with carriage returns - no newline
    arrives until the wheel finishes (minutes for torch). Splitting on '\r' too turns
    every bar repaint into an event, so the UI gets live percent/speed/ETA instead of a
    silent 'downloading' line that only ends at EOF.
    """
    return [seg for seg in re.split(r"[\r\n]+", raw) if seg.strip()]


#: pip's carriage-return progress repaint: '765MB 94% <bar> 12.3MB/s eta 0:00:12' (the
#: bar body is box-drawing chars on ANSI terminals, CJK block elements in the Windows
#: console - both are matched and discarded).
#: pip's carriage-return progress repaints. Two styles exist depending on pip version:
#:   * percent style (tqdm-era):  '765MB 45% <bar> 12.3MB/s eta 0:00:38'
#:   * fraction style (rich era): '765.0/765.0 MB 12.3 MB/s eta 0:00:00'
#: The bar body is box-drawing chars on ANSI terminals, CJK block elements in the
#: Windows console - both are matched and discarded.
_PIP_BAR_CHARS = r"[\u2500-\u257f\u2580-\u259f\u25a0-\u25cf\u2e30-\u2e7f\ufb00-\ufbff]*"
_PIP_SPEED_RE = r"(?:\s+([\d.]+)\s*([KMGTkmgt]?)\s*B/s)?"
_PIP_ETA_RE = r"(?:\s+eta\s+(\S+))?"

_PIP_PROGRESS_PERCENT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*([KMGTkmgt]?B)\s+(\d{1,3})%\s*"
    + _PIP_BAR_CHARS
    + _PIP_SPEED_RE + _PIP_ETA_RE + r"\s*$")

_PIP_PROGRESS_FRAC_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([KMGTkmgt]?B)?"
    + _PIP_SPEED_RE + _PIP_ETA_RE + r"\s*$")

_PIP_UNIT_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _parse_eta(text: str):
    """'0:00:38' / '5:23' / '1:02:03' -> seconds (float), or None when unknown ('.')."""
    parts = (text or "").strip().split(":")
    if not all(re.fullmatch(r"\d+", x) for x in parts) or len(parts) > 3:
        return None
    sec = 0.0
    for x in parts:
        sec = sec * 60 + int(x)
    return sec


def _pip_speed_bytes_per_sec(num: str, unit: str):
    """'12.3' + 'M' -> bytes/second (binary units, as pip reports them)."""
    if not num:
        return None  # optional group - no speed reported on this repaint
    try:
        return float(num) * _PIP_UNIT_MULT.get((unit or "").upper(), 1)
    except ValueError:
        return None


def parse_pip_progress_line(line: str):
    """Parse one pip in-place progress repaint into {'percent','speed_bps','eta_sec'}.

    Handles BOTH bar styles (percent and downloaded/total) and both ETA shapes
    (M:SS / H:MM:SS). Returns None for anything that is not a progress repaint -
    those lines go through parse_pip_line as usual. Informational only: the UI shows
    the numbers on the package that is currently downloading.
    """
    s = (line or "").strip()
    m = _PIP_PROGRESS_PERCENT_RE.match(s)
    if m:
        speed = _pip_speed_bytes_per_sec(m.group(4), m.group(5))
        return {"percent": min(100, int(m.group(3))), "speed_bps": speed,
                "eta_sec": _parse_eta(m.group(6))}
    m = _PIP_PROGRESS_FRAC_RE.match(s)
    if m:
        try:
            got = float(m.group(1)); total = float(m.group(2))
        except ValueError:
            return None
        mult = _PIP_UNIT_MULT.get((m.group(3) or "").upper(), 1)
        pct = min(100, int(round(got / total * 100))) if total > 0 else 0
        speed = _pip_speed_bytes_per_sec(m.group(4), m.group(5))
        return {"percent": pct, "speed_bps": speed, "eta_sec": _parse_eta(m.group(6))}
    return None


def _apply_pip_progress(pip_state: dict, prog: dict) -> None:
    """Attach a percent/speed/ETA repaint to the package that is downloading right now.

    No-op when nothing is actively downloading (e.g. pip is between phases). The active
    key lives in pip_state['_active_download'] and is stripped before persisting.
    """
    pkgs = pip_state.get("packages") or {}
    active = pip_state.get("_active_download")
    if not active or active not in pkgs:
        return
    p = pkgs[active]
    if "percent" in prog and isinstance(prog["percent"], int):
        p["percent"] = min(100, max(0, prog["percent"]))
    if prog.get("speed_bps"):
        p["speed_bps"] = round(float(prog["speed_bps"]), 1)
    if prog.get("eta_sec") is not None:
        p["eta_sec"] = round(max(0.0, float(prog["eta_sec"])), 1)


def _strip_internal_markers(pip_state: dict) -> None:
    """Drop bookkeeping keys ('_active_download') before the state hits the JSON file."""
    pip_state.pop("_active_download", None)


def _public_pip_snapshot(pip_state: dict) -> dict:
    """Copy of the pip stage without internal bookkeeping keys ('_active_download')."""
    return {k: v for k, v in pip_state.items() if not str(k).startswith("_")}


def _normalize_pkg_name(name: str) -> str:
    """PEP-503-ish normalization so 'torch' == 'Torch_2.9.1+cu128-cp312...whl' match."""
    n = re.split(r"[=<>!~\s(]", name or "", 1)[0].strip()
    n = os.path.basename(n)
    if n.lower().endswith(".whl"):
        parts = n[:-4].split("-")
        n = parts[0] if len(parts) > 1 else n
    return re.sub(r"[_\.\-]+", "-", n).lower()


def parse_pip_line(line: str):
    """Parse ONE plain pip stdout line into a progress event, or None.

    Events (dicts):
      {'kind':'collect',   'name': 'torch==2.9.1+cu128'}
      {'kind':'satisfied', 'name': ...}
      {'kind':'download',  'file': 'torch-....whl', 'size_bytes': int|None, 'url': str}
      {'kind':'cached',    'file': ..., 'url': str?}
      {'kind':'install_start', 'names': [...]}   (continuation lines: {'kind':'install_cont','name':...})
      {'kind':'success',   'names': [...]}
      {'kind':'note',      'text': ...}          (anything else non-empty - kept as last line)
    """
    s = (line or "").strip()
    if not s:
        return None

    m = re.match(r"^Collecting\s+(\S+)", s)
    if m:
        return {"kind": "collect", "name": m.group(1)}

    m = re.match(r"^Requirement already satisfied:\s*(\S+)", s)
    if m:
        return {"kind": "satisfied", "name": m.group(1)}

    # 'Downloading https://...whl (765 MB)'  or  'Downloading torch-....whl'
    m = re.match(r"^Downloading\s+(\S+?)(?:\s+\(([^)]+)\))?\s*$", s)
    if m:
        from urllib.parse import unquote
        target = unquote(m.group(1))
        return {"kind": "download", "file": os.path.basename(target),
                "size_bytes": _pip_size_to_bytes(m.group(2)), "url": m.group(1)}

    # 'Using cached torch-....whl'  /  'Using cached https://... (765 MB)'
    m = re.match(r"^Using cached\s+(\S+)", s)
    if m:
        from urllib.parse import unquote
        return {"kind": "cached", "file": os.path.basename(unquote(m.group(1))),
                "url": m.group(1)}

    # 'Installing collected packages:' + names (may wrap onto continuation lines)
    if s.startswith("Installing collected packages"):
        rest = s.split(":", 1)[1].strip() if ":" in s else ""
        return {"kind": "install_start",
                "names": [x for x in re.split(r"[,\s]+", rest) if x]}

    m = re.match(r"^Successfully installed\s+(.+)$", s)
    if m:
        names = []
        for tok in m.group(1).split():
            # 'torch (2.9.1+cu128)' or plain 'torch'
            nm = re.sub(r"\s*\(.*\)$", "", tok)
            if nm:
                names.append(nm)
        return {"kind": "success", "names": names}

    # Indented continuation of the 'Installing collected packages:' name list.
    m = re.match(r"^\s+([A-Za-z0-9][A-Za-z0-9._+\-]*)\s*$", line)
    if m:
        return {"kind": "install_cont", "name": m.group(1)}

    # Progress-bar noise and other lines - surfaced as the 'last line' only.
    if re.match(r"^\d+/\d+\s*\[", s):
        return None
    return {"kind": "note", "text": s[:300]}


def apply_pip_event(pip_state: dict, ev: dict) -> None:
    """Merge one parse_pip_line() event into the 'pip' stage dict (in place).

    Package identity is matched by normalized name so a wheel filename maps back to
    its 'Collecting <spec>' entry. Unknown wheels get their own entry - nothing is
    ever dropped from what pip actually did.
    """
    pkgs = pip_state.setdefault("packages", {})

    def _find_by_norm(norm: str):
        """Exact normalized match first; else the LONGEST stored name that is a
        version-prefixed ancestor (stored 'torch' matches token 'torch-2.9.1-cu128').
        Longest-wins so 'foo-bar' never mis-matches onto stored 'foo'."""
        best = None
        for p in pkgs.values():
            pn = _normalize_pkg_name(p.get("name", ""))
            if not pn:
                continue
            if pn == norm or (norm.startswith(pn + "-") and
                              (best is None or len(pn) > len(_normalize_pkg_name(best.get("name", ""))))):
                best = p
        return best

    kind = ev.get("kind")
    if kind == "collect":
        name = ev["name"]
        key = _normalize_pkg_name(name) or name
        pkgs.setdefault(key, {"name": name, "status": "pending"})
    elif kind == "satisfied":
        p = _find_by_norm(_normalize_pkg_name(ev["name"]))
        if p:
            p["status"] = "done"
        else:
            key = _normalize_pkg_name(ev["name"]) or ev["name"]
            pkgs.setdefault(key, {"name": ev["name"], "status": "done"})
    elif kind in ("download", "cached"):
        norm = _normalize_pkg_name(ev.get("file", ""))
        key = norm or os.path.basename(ev.get("file") or ev.get("url") or "package")
        p = pkgs.get(key) if norm else None
        # a wheel whose exact spec was never collected may still match a stored entry by name prefix
        if p is None and norm:
            p = _find_by_norm(norm)
        if p is None:
            p = pkgs.setdefault(key, {"name": os.path.basename(ev.get("file") or ev.get("url") or "package"),
                                      "status": "pending"})
        if kind == "download":
            # a new download starts - clear stale progress numbers from the previous one
            prev = pip_state.get("_active_download")
            if prev and prev in pkgs:
                for k in ("percent", "speed_bps", "eta_sec"):
                    pkgs[prev].pop(k, None)
            p["status"] = "downloading"
            if ev.get("size_bytes"):
                p["size_bytes"] = ev["size_bytes"]
            if ev.get("url"):
                p["url"] = ev["url"]
            pip_state["_active_download"] = key
        else:
            # 'Using cached <wheel>' - the wheel is in pip's local cache; it still has to
            # be INSTALLED, so keep a distinct status (the UI shows it as ready-to-install)
            p["status"] = "cached"
            if pip_state.get("_active_download") == key:
                for k in ("percent", "speed_bps", "eta_sec"):
                    p.pop(k, None)
    elif kind == "install_start":
        for nm in ev.get("names", []):
            key = _normalize_pkg_name(nm) or nm
            pkgs.setdefault(key, {"name": nm, "status": "pending"})["status"] = "installing"
    elif kind == "install_cont":
        p = _find_by_norm(_normalize_pkg_name(ev.get("name", "")))
        if p:
            p["status"] = "installing"
    elif kind == "success":
        pip_state.pop("_active_download", None)  # all downloads are finished now
        for nm in ev.get("names", []):
            key = _normalize_pkg_name(nm) or nm
            pkgs.setdefault(key, {"name": nm, "status": "pending"})["status"] = "done"

    line_val = ev.get("text") or ev.get("file") or ev.get("url") \
        or (ev.get("names", [""])[0] if ev.get("names") else "")
    if line_val:
        pip_state["line"] = str(line_val)[:300]
