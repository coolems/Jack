"""Win32 window/identity/clipboard primitives for computer_use (Windows).

Split from the original single native module so each dep holds ONE concern and stays
readable: _desktop_input owns SendInput (mouse/keyboard), this module owns windows, process
identity, session lock and clipboard, _desktop_gdi owns pixel capture.

Deps exec in SEPARATE namespaces (loader.globals_builder.install_shared_deps execs each dep in
its own globals), so nothing here imports a sibling dep. Public names are unique across the
folder so the merged exec globals can never shadow one dep's symbol with another's; the raw
API handles stay PRIVATE for the same reason (_desktop_gdi holds its own user32 handle).

Public API only (no underscore-prefixed functions): install_shared_deps merges only
non-underscore symbols into the tool's exec globals, so a private name here would be
invisible to the tools (the 2026-10-23 '_BROWSER_HEADERS' incident). Tool files never import
ctypes themselves, so the AST guard needs no policy row for this folder.
"""

import ctypes
import logging
import os
from ctypes import wintypes

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Win32")

#: ctypes.windll only exists on Windows. Guarding it keeps this dep importable on any
#: platform (SERVER tool discovery imports every tool file) and every call answers honestly
#: instead of raising AttributeError deep inside a tool.
_WINDOWS = os.name == "nt" and hasattr(ctypes, "windll")

if _WINDOWS:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
else:
    _user32 = _kernel32 = None

#: Explicit prototypes for the clipboard memory functions (Windows, 64-bit).
#:
#: WHY: ctypes defaults an undeclared function's return to c_long - a 32-bit signed int - so
#: GlobalAlloc's 64-bit HGLOBAL handle was TRUNCATED, and GlobalLock then returned NULL for
#: it. cua_paste crashed with 'access violation writing 0x0000000000000000' on the memmove
#: (verified live 2026-11-07). Declaring the real types fixes it: handles are pointer-sized,
#: and GlobalLock returns a pointer. Nothing else in this repo declares kernel32 functions,
#: so these prototypes cannot collide with another module (unlike user32.GetWindowRect, which
#: tools/web_interact/browser_screenshot.py also declares - see RectBuffer above).
if _WINDOWS:
    _kernel32.GlobalAlloc.restype = ctypes.c_void_p
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalFree.restype = ctypes.c_void_p
    _kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    _kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    _user32.GetClipboardData.restype = ctypes.c_void_p
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.SetClipboardData.restype = ctypes.c_void_p
    _user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000

def cua_focus_allowed(app_pid: int, focused_pid: int, app_exe_path: str = "") -> bool:
    """True when keyboard focus belongs to the granted app.

    Fail-closed: anything unreadable or unmatched returns False, so keys are never sent on a
    guess. Two accepted forms of 'the same app':

      * the focused pid IS the bound app's pid
      * the focused pid runs the SAME executable as the bound app (identity comparison, the
        same verified identity the consent key is built from)

    The second form is required in practice: Chrome/Edge render web content in CHILD processes
    that all run chrome.exe, and their UIA elements report the CHILD pid. Strict pid equality
    would refuse typing into the field the user just allowed. An app running a DIFFERENT
    executable is still refused - that is the whole point of the guard.
    """
    if not app_pid or not focused_pid:
        return False
    if int(focused_pid) == int(app_pid):
        return True
    if not app_exe_path:
        return False
    focused_exe = cua_process_image_path(int(focused_pid))
    if not focused_exe:
        return False
    return os.path.normcase(focused_exe) == os.path.normcase(app_exe_path)

#: GetWindowRect buffer: a plain LONG[4] array, NOT a struct.
#:
#: tools/web_interact/browser_screenshot.py declares
#: 'GetWindowRect.argtypes = [HWND, POINTER(c_int * 4)]' on the SHARED
#: ctypes.windll.user32 object at import time, so whichever module loads last decides the
#: prototype. A struct then raises ctypes.ArgumentError inside cua_window_rect (the crash
#: this avoids); a LONG[4] array satisfies BOTH the declared array prototype and the
#: undeclared default, so the two tools can load in any order without breaking each other.
#: Same layout, same meaning: left, top, right, bottom.
RectBuffer = ctypes.c_long * 4

_dpi_ready = False


def desktop_available() -> bool:
    """True when this process can reach the Win32 desktop APIs at all."""
    return _WINDOWS and _user32 is not None


def _make_dpi_aware() -> bool:
    """Make this process DPI-aware once, so window/screen pixels match SendInput.

    Private on purpose: _desktop_input exposes the public cua_make_dpi_aware, and a public
    duplicate here would collide in the merged exec globals. Without DPI awareness a scaled
    display reports virtualized pixels and every click lands on the wrong pixel.
    """
    global _dpi_ready
    if not _dpi_ready:
        if not desktop_available():
            return False
        try:
            _user32.SetProcessDPIAware()
        except Exception as e:
            logger.warning("SetProcessDPIAware failed: %s", e)
        _dpi_ready = True
    return _dpi_ready


def cua_screen_size():
    """Physical screen size in pixels: (width, height)."""
    _make_dpi_aware()
    return int(_user32.GetSystemMetrics(0)), int(_user32.GetSystemMetrics(1))


#: GetSystemMetrics indices of the VIRTUAL desktop (all monitors, union).
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def cua_virtual_desktop() -> dict:
    """Virtual desktop as {left, top, width, height} in physical pixels.

    Needed to convert absolute screen pixels into SendInput's normalized MOUSEF_ABSOLUTE
    space (0..65535 across the virtual desktop), which is how a held-button drag produces
    real WM_MOUSEMOVE events instead of just relocating the cursor.
    """
    _make_dpi_aware()
    left = int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if width <= 0 or height <= 0:
        w, h = cua_screen_size()
        return {"left": 0, "top": 0, "width": w, "height": h}
    return {"left": left, "top": top, "width": width, "height": height}


def cua_window_rect(hwnd: int):
    """Window rectangle as a dict, or None when the handle is no longer valid."""
    _make_dpi_aware()
    buf = RectBuffer()
    if not _user32.GetWindowRect(int(hwnd), ctypes.byref(buf)):
        return None
    left, top, right, bottom = int(buf[0]), int(buf[1]), int(buf[2]), int(buf[3])
    return {"left": left, "top": top, "right": right, "bottom": bottom,
            "width": right - left, "height": bottom - top}


def cua_enum_windows():
    """Every visible top-level window: {hwnd, title, pid, rect}.

    Minimized windows report the off-screen (-32000) sentinel rect; callers keep it so
    'on_screen' is derived honestly instead of pretending the window is gone.
    """
    _make_dpi_aware()
    found = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            n = _user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            rect = cua_window_rect(int(hwnd))
            if rect is None:
                return True
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append({"hwnd": int(hwnd), "title": buf.value,
                          "pid": int(pid.value), "rect": rect})
        except Exception as e:
            logger.debug("window enumeration skipped one entry: %s", e)
        return True

    _user32.EnumWindows(proc_type(_cb), 0)
    return found


def cua_process_image_path(pid: int) -> str:
    """Full executable path of *pid* (verified identity), '' when unavailable.

    Uses QUERY_LIMITED_INFORMATION only: no process is opened for writing, and nothing here
    can terminate or inject into a process.
    """
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def cua_session_locked() -> bool:
    """True when the desktop input desktop is not reachable (screen locked).

    Screenshots taken while locked are not current observations, and input sent there is
    delivered to the lock screen, not the app - so every action tool refuses first.
    """
    handle = _user32.OpenInputDesktop(0, False, GENERIC_READ | GENERIC_WRITE)
    if not handle:
        return True
    _kernel32.CloseHandle(handle)
    return False


def cua_show_window(hwnd: int, restore: bool = False) -> bool:
    """Unhide/unminimize a window WITHOUT activating it. Returns whether the call landed.

    SW_RESTORE is the only size-changing command used and SW_SHOW makes the window paintable.
    SW_SHOWNORMAL / SetForegroundWindow are deliberately NOT used: they activate the window,
    which steals focus from the user. Activation is a separate explicit tool, not shipped.
    """
    SW_SHOW = 5
    SW_RESTORE = 9
    cmd = SW_RESTORE if restore else SW_SHOW
    return bool(_user32.ShowWindow(hwnd, cmd))


def cua_foreground_hwnd() -> int:
    """HWND currently owning the foreground, 0 when there is none."""
    return int(_user32.GetForegroundWindow() or 0)


def cua_clipboard_get() -> str:
    """Current clipboard text (CF_UNICODETEXT), '' when empty or not text.

    Read-only: nothing is written or cleared here. The memory handle stays owned by the
    clipboard, so the text is copied out and never freed by this function.
    """
    CF_UNICODETEXT = 13
    if not _user32.OpenClipboard(0):
        return ""
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            size = _kernel32.GlobalSize(handle)
            raw = ctypes.string_at(ptr, size) if size else b""
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def cua_clipboard_set(text: str) -> bool:
    """Put *text* on the clipboard as CF_UNICODETEXT. Returns whether it landed.

    This REPLACES the clipboard, so a caller that must preserve the user's content reads it
    first (cua_clipboard_get) and restores it afterwards - cua_paste does exactly that. Only
    the text format is written; no other clipboard format is touched.

    Paste uses the clipboard rather than keystrokes because Return/Tab can trigger a
    control's action instead of inserting text; delivered clipboard data is still not proof
    of insertion, so the caller must inspect the target afterwards.
    """
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    if not _user32.OpenClipboard(0):
        return False
    try:
        _user32.EmptyClipboard()
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            return False
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            # Nothing was copied, so this handle is still ours - free it rather than hand a
            # lock-less handle to the clipboard.
            _kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            _kernel32.GlobalUnlock(handle)
        # SetClipboardData takes ownership of the handle - it must not be freed here.
        return bool(_user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        _user32.CloseClipboard()
