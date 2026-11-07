"""SendInput primitives for computer_use (mouse + keyboard, Windows).

Split out of _desktop_win.py so each dep stays small and one concern per file: this module
owns input delivery, _desktop_win owns windows/identity/clipboard, _desktop_gdi owns capture.

Deps exec in SEPARATE namespaces (loader.globals_builder.install_shared_deps execs each dep
in its own globals), so nothing here imports a sibling dep - the DPI check is local.

Public API only (no underscore-prefixed functions): install_shared_deps merges non-underscore
symbols into the tool's exec globals, so a private name here would be invisible to the tools
(the 2026-10-23 '_BROWSER_HEADERS' incident). Tool files never import ctypes themselves, so
the AST guard needs no policy row for this folder.
"""

import ctypes
import logging
import os
import time
from ctypes import wintypes

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Input")

#: ctypes.windll only exists on Windows. Guarding it keeps this dep importable on any
#: platform (SERVER tool discovery imports every tool file), and every call below answers
#: honestly instead of raising AttributeError deep inside a tool.
HAVE_WIN = os.name == "nt" and hasattr(ctypes, "windll")
user32 = ctypes.windll.user32 if HAVE_WIN else None

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEF_MOVE = 0x0001
MOUSEF_LEFTDOWN = 0x0002
MOUSEF_LEFTUP = 0x0004
MOUSEF_RIGHTDOWN = 0x0008
MOUSEF_RIGHTUP = 0x0010
MOUSEF_MIDDLEDOWN = 0x0020
MOUSEF_MIDDLEUP = 0x0040
MOUSEF_WHEEL = 0x0800
#: MOUSEEVENTF_ABSOLUTE is 0x8000 - NOT 0x0002 (that is MOUSEEVENTF_LEFTDOWN). Getting this
#: wrong turns a normalized absolute move into a huge RELATIVE delta, which slams the pointer
#: into the screen corner and fires no drag at all (verified live).
MOUSEF_ABSOLUTE = 0x8000

#: SendInput MOUSEF_ABSOLUTE coordinate space is 0..65535 across the virtual desktop.
ABSOLUTE_SPACE = 65535

KEYF_EXTENDED = 0x0001
KEYF_KEYUP = 0x0002
KEYF_UNICODE = 0x0004

#: Mouse buttons accepted by cua_click / cua_drag -> (press flag, release flag).
MOUSE_BUTTONS = {"left": (MOUSEF_LEFTDOWN, MOUSEF_LEFTUP),
                 "right": (MOUSEF_RIGHTDOWN, MOUSEF_RIGHTUP),
                 "middle": (MOUSEF_MIDDLEDOWN, MOUSEF_MIDDLEUP)}

#: Named keys -> (virtual key code, needs the extended-key flag). The fixed table is the
#: only accepted key source: an arbitrary key code from the model is never honoured.
VK_CODES = {
    "backspace": (0x08, False), "tab": (0x09, False), "enter": (0x0D, False),
    "return": (0x0D, False), "escape": (0x1B, False), "space": (0x20, False),
    "prior": (0x21, False), "pageup": (0x21, False), "next": (0x22, False),
    "pagedown": (0x22, False), "end": (0x23, False), "home": (0x24, False),
    "left": (0x25, True), "up": (0x26, True), "right": (0x27, True),
    "down": (0x28, True), "insert": (0x2D, True), "delete": (0x2E, True),
    "ctrl": (0x11, False), "control": (0x11, False), "alt": (0x12, False),
    "shift": (0x10, False), "cmd": (0x5B, False), "win": (0x5B, False),
    "menu": (0x5D, False), "capslock": (0x14, False), "numlock": (0x90, True),
    # The literal '+' and '-' keys: the chord syntax writes Command-plus as 'cmd++'. These are
    # the OEM codes for the physical keys - VK_OEM_PLUS (0xBB) and VK_OEM_MINUS (0xBD). The
    # arithmetic codes VK_ADD (0x6B) / VK_SUBTRACT (0x2D) are numpad keys and insert nothing
    # into a text field; 0x6E is VK_DECIMAL, which is neither key (the first version used it
    # for 'plus', so 'ctrl++' zoomed nothing and 'cmd++' sent the wrong key).
    "plus": (0xBB, False), "+": (0xBB, False), "kpadd": (0x6B, True),
    "minus": (0xBD, False), "-": (0xBD, False), "kpsubtract": (0x6B, True),
    "pause": (0x13, True), "printscreen": (0x2C, True), "scrolllock": (0x91, True),
    "f1": (0x70, False), "f2": (0x71, False), "f3": (0x72, False), "f4": (0x73, False),
    "f5": (0x74, False), "f6": (0x75, False), "f7": (0x76, False), "f8": (0x77, False),
    "f9": (0x78, False), "f10": (0x79, False), "f11": (0x7A, False), "f12": (0x7B, False),
    "kp0": (0x60, False), "kp1": (0x61, False), "kp2": (0x62, False), "kp3": (0x63, False),
    "kp4": (0x64, False), "kp5": (0x65, False), "kp6": (0x66, False), "kp7": (0x67, False),
    "kp8": (0x68, False), "kp9": (0x69, False),
}


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtra", ctypes.POINTER(ctypes.c_ulong))]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtra", ctypes.POINTER(ctypes.c_ulong))]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


_dpi_ready = False


def input_available() -> bool:
    """True when SendInput is reachable in this process."""
    return HAVE_WIN and user32 is not None


def cua_make_dpi_aware() -> bool:
    """Make this process DPI-aware so screen pixels match SendInput.

    Called once per process; later calls are free. Without it a scaled display reports
    virtualized pixels and every click lands on the wrong pixel.
    """
    global _dpi_ready
    if not _dpi_ready:
        if not input_available():
            return False
        try:
            user32.SetProcessDPIAware()
        except Exception as e:
            logger.warning("SetProcessDPIAware failed: %s", e)
        _dpi_ready = True
    return _dpi_ready


def cua_cursor_pos():
    """Current cursor position as (x, y), or None when the cursor is unreadable."""
    if not input_available():
        return None
    cua_make_dpi_aware()
    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return None


def cua_move_cursor(x: int, y: int) -> tuple:
    """Move the real cursor to absolute screen pixels. Returns (target, actual)."""
    if not input_available():
        return (int(x), int(y)), None
    cua_make_dpi_aware()
    user32.SetCursorPos(int(x), int(y))
    return (int(x), int(y)), cua_cursor_pos()


def cua_send_mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> int:
    """One SendInput mouse event. Returns the number of events actually queued (0 = refused)."""
    if not input_available():
        return 0
    cua_make_dpi_aware()
    inp = _Input()
    inp.type = INPUT_MOUSE
    inp.union.mi.dx = int(dx)
    inp.union.mi.dy = int(dy)
    inp.union.mi.mouseData = int(data)
    inp.union.mi.dwFlags = int(flags)
    return int(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_Input)))


def cua_send_key(vk: int, extended: bool, up: bool = False) -> int:
    """Send one keyboard event by virtual-key code. Returns events queued (0 = refused)."""
    if not input_available():
        return 0
    cua_make_dpi_aware()
    flags = (KEYF_EXTENDED if extended else 0) | (KEYF_KEYUP if up else 0)
    inp = _Input()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = int(vk)
    inp.union.ki.dwFlags = flags
    return int(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_Input)))


def cua_send_unicode(ch: str) -> int:
    """Send one character as a Unicode keyboard event (no per-keyboard layout assumptions)."""
    if not input_available():
        return 0
    cua_make_dpi_aware()
    inp = _Input()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wScan = ord(ch)
    inp.union.ki.dwFlags = KEYF_UNICODE | KEYF_KEYUP
    return int(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_Input)))


def cua_press(button: str, hold_ms: int = 0) -> bool:
    """Press and release one mouse button, holding it for *hold_ms* milliseconds.

    The order matters: the press event goes out FIRST, then the hold, then the release. The
    first version slept before the press, so hold_ms delayed the click instead of holding the
    button - a long hold never produced a real press-and-hold (no drag-start, no long-press).
    The release is ALWAYS attempted, even if the hold raised, so a button can never stay
    stuck. The hold is clamped here as well as by the calling tool.
    """
    if button not in MOUSE_BUTTONS:
        return False
    down, up = MOUSE_BUTTONS[button]
    sent_down = cua_send_mouse(down)
    try:
        if hold_ms > 0:
            time.sleep(min(int(hold_ms), 5000) / 1000.0)
    finally:
        sent_up = cua_send_mouse(up)
    return sent_down > 0 and sent_up > 0


def cua_wheel(direction: str, amount: int) -> int:
    """Scroll the wheel by *amount* clicks. Returns the events queued (0 = not delivered).

    SendInput reports how many events it queued, not the delta, so the caller reads the
    delta from cua_wheel_delta() when it wants to report the size of the scroll.
    """
    return cua_send_mouse(MOUSEF_WHEEL, data=cua_wheel_delta(direction, amount))


def cua_wheel_delta(direction: str, amount: int) -> int:
    """The wheel delta for *amount* clicks, in the sign Windows expects (down = negative)."""
    delta = 120 * max(1, min(int(amount), 20))
    return -delta if direction == "down" else delta


def cua_drag(from_xy: tuple, to_xy: tuple, button: str = "left", steps: int = 20) -> bool:
    """Hold a button while moving the pointer from one absolute point to another.

    Shape of a gesture apps accept (verified live against Chrome):
      1. put the pointer ON the start point first (SetCursorPos - no button involved),
      2. press with a SendInput button event,
      3. move with SendInput MOUSE_MOVE|MOUSE_ABSOLUTE events, spaced apart,
      4. release at the end point.

    Absolute SendInput events are used for the path because relocating the cursor does not
    deliver mousemove events to the app: a gesture an app only recognises when it sees
    mousemove events *with the button held* (Chrome's HTML5 drag-and-drop, slider dragging)
    would silently ignore plain cursor relocation. The spacing matters too - a burst of
    events collapses and Chrome's drag manager starts nothing.

    The release is in a finally block: a failure mid-drag must never leave the button pressed.
    """
    if button not in MOUSE_BUTTONS:
        return False
    down, up = MOUSE_BUTTONS[button]
    steps = max(2, min(int(steps), 40))
    sx, sy = int(from_xy[0]), int(from_xy[1])
    ex, ey = int(to_xy[0]), int(to_xy[1])

    cua_move_cursor(sx, sy)
    time.sleep(0.08)
    pressed = cua_send_mouse(down) > 0
    try:
        time.sleep(0.08)
        for i in range(1, steps + 1):
            t = i / steps
            cua_send_mouse(MOUSEF_MOVE | MOUSEF_ABSOLUTE,
                           dx=_norm_x(sx + int((ex - sx) * t)),
                           dy=_norm_y(sy + int((ey - sy) * t)),
                           data=0)
            time.sleep(0.03)
    finally:
        released = cua_send_mouse(up | MOUSEF_ABSOLUTE, dx=_norm_x(ex), dy=_norm_y(ey), data=0) > 0
    return pressed and released


def _norm_x(px: int) -> int:
    """Absolute screen pixel -> SendInput normalized x (0..65535 across the virtual desktop)."""
    vd = _virtual_desktop()
    if not vd or vd["width"] <= 0:
        return int(px)
    return int((px - vd["left"]) * ABSOLUTE_SPACE / vd["width"])


def _norm_y(px: int) -> int:
    """Absolute screen pixel -> SendInput normalized y (0..65535 across the virtual desktop)."""
    vd = _virtual_desktop()
    if not vd or vd["height"] <= 0:
        return int(px)
    return int((px - vd["top"]) * ABSOLUTE_SPACE / vd["height"])


def _virtual_desktop() -> dict:
    """Virtual desktop rect, read locally (deps exec in separate namespaces, so no sibling import).

    Uses the module's own user32 handle and the guarded DPI call, so a drag's 40 normalized
    moves do not each re-run SetProcessDPIAware.
    """
    if not HAVE_WIN:
        return {}
    try:
        cua_make_dpi_aware()
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width <= 0 or height <= 0:
            return {"left": 0, "top": 0, "width": int(user32.GetSystemMetrics(0)),
                    "height": int(user32.GetSystemMetrics(1))}
        return {"left": left, "top": top, "width": width, "height": height}
    except Exception:
        return {}


def cua_hold_keys(vk_list, body) -> bool:
    """Hold modifiers, run *body* (a zero-arg callable), then release the modifiers.

    Release runs in a finally block so a failure inside the body cannot leave a modifier
    pressed, which would corrupt every later keystroke in the session.
    """
    for vk, ext in vk_list:
        cua_send_key(vk, ext)
    if vk_list:
        # A held modifier needs a moment before the key it modifies: back-to-back events
        # produced nothing for shifted characters in Chrome's web content.
        time.sleep(0.012)
    try:
        body()
    finally:
        for vk, ext in reversed(vk_list):
            cua_send_key(vk, ext, up=True)
    return True


def cua_vk_for(name: str):
    """(vk, extended) for one key name, or None. Names come from the fixed tables only.

    VK_CODES holds the named keys (enter, f1-f12, arrows, modifiers, numpad). Single
    characters - letters, digits, punctuation - are resolved from the typing tables, because
    the common chords are character chords: 'ctrl+c', 'ctrl+v', 'ctrl+0' (reset zoom),
    'ctrl+shift+_'. A name in no table returns None, so an arbitrary key code is never
    honoured. The first version looked only in VK_CODES, so EVERY letter chord was refused
    as UNKNOWN_KEY - including the copy/paste chords cua_paste depends on.
    """
    entry = VK_CODES.get(str(name).strip().lower())
    if entry is not None:
        return entry
    text = str(name)
    if len(text) == 1:
        plain = CHAR_VK_PLAIN.get(text)
        if plain is not None:
            return (plain, False)
        shifted = CHAR_VK_SHIFT.get(text)
        if shifted is not None:
            return (shifted, False)
    return None


def cua_press_chord(key: str, modifiers=None) -> bool:
    """Press one key with held modifiers, then release the modifiers.

    'cmd++' means Command-plus; 'cmd+' alone is rejected by the caller (an incomplete
    chord). Release always happens via cua_hold_keys.
    """
    entry = cua_vk_for(key)
    if entry is None:
        return False
    vk, extended = entry
    mods = []
    for mod in (modifiers or []):
        m = VK_CODES.get(str(mod).strip().lower())
        if m is not None:
            mods.append(m)

    def _body():
        cua_send_key(vk, extended)
        cua_send_key(vk, extended, up=True)

    if not mods:
        _body()
        return True
    return cua_hold_keys(mods, _body)


#: Printable characters SendInput can send by virtual-key code. Chrome's web content does not
#: accept KEYF_UNICODE events in a form field (verified live: a unicode event inserted nothing,
#: a VK event did), so keyboard text goes through VK codes and only characters outside this
#: table fall back to unicode events.
#:
#: The punctuation codes are the OEM codes for the physical key, NOT the arithmetic numpad
#: codes: VK_SUBTRACT (0x2D) inserts nothing into a text field, VK_OEM_MINUS (0xBD) is the
#: main-row minus key (verified live - 0x2D silently dropped every '-' and '_').
CHAR_VK_PLAIN = {}
CHAR_VK_SHIFT = {}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHAR_VK_PLAIN[_ch] = 0x41 + _i          # VK_A..VK_Z (no shift = lowercase)
for _i, _ch in enumerate("0123456789"):
    CHAR_VK_PLAIN[_ch] = 0x30 + _i          # VK_0..VK_9
CHAR_VK_PLAIN.update({" ": 0x20, "\n": 0x0D, "\r": 0x0D, "\t": 0x09, "\t": 0x09})
# physical key -> (unshifted char, shifted char); code is the same VK for both.
_OEM_KEYS = {
    0xBD: ("-", "_"),   # VK_OEM_MINUS   main-row minus
    0xBB: ("=", "+"),   # VK_OEM_PLUS
    0xBA: (";", ":"),   # VK_OEM_1
    0xBF: ("/", "?"),   # VK_OEM_2
    0xC0: ("`", "~"),   # VK_OEM_3
    0xDB: ("[", "{"),   # VK_OEM_4
    0xDC: ("\\", "|"),  # VK_OEM_5
    0xDD: ("]", "}"),   # VK_OEM_6
    0xDE: ("'", '"'),   # VK_OEM_7
    0xBC: (",", "<"),   # VK_OEM_COMMA
    0xBE: (".", ">"),   # VK_OEM_PERIOD
}
for _vk, (_plain, _shifted) in _OEM_KEYS.items():
    CHAR_VK_PLAIN[_plain] = _vk
    CHAR_VK_SHIFT[_shifted] = _vk
for _i, _ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    CHAR_VK_SHIFT[_ch] = 0x41 + _i
# Shifted digits live on the same number-row keys (VK_1..VK_0), not on separate keys.
for _i, _ch in enumerate("!@#$%^&*()"):
    CHAR_VK_SHIFT[_ch] = 0x31 + _i if _ch != ")" else 0x30


def cua_type_keys(text: str, interval_ms: float = 0.012) -> int:
    """Send *text* as keyboard events. Returns how many events were queued.

    Characters in the VK table go out as virtual-key events (with Shift held for the shifted
    ones - released in a finally block, so Shift can never stay pressed). Anything outside the
    table (non-ASCII) is sent as a unicode event. Delivered events are not proof of insertion,
    so the caller must re-observe the field.
    """
    sent = 0
    for ch in str(text):
        vk = CHAR_VK_PLAIN.get(ch)
        if vk is not None:
            sent += cua_send_key(vk, False)
            sent += cua_send_key(vk, False, up=True)
        else:
            shifted = CHAR_VK_SHIFT.get(ch)
            if shifted is not None:
                sent += cua_send_key(VK_CODES["shift"][0], False)
                # A held modifier needs a moment before the key it modifies: sending the two
                # events back-to-back produced nothing for shifted characters in Chrome.
                time.sleep(0.012)
                try:
                    sent += cua_send_key(shifted, False)
                    sent += cua_send_key(shifted, False, up=True)
                finally:
                    cua_send_key(VK_CODES["shift"][0], False, up=True)
            else:
                sent += cua_send_unicode(ch)
        if interval_ms > 0:
            time.sleep(interval_ms / 1000.0)
    return sent


def cua_type_unicode(text: str, interval_ms: float = 0.25) -> int:
    """Send *text* as Unicode keyboard events. Returns how many events were queued.

    Kept for characters the VK table cannot express and for apps that accept unicode events.
    Delivery goes to whatever the target app currently has focused, so the calling tool must
    have verified focus first - and a delivered character is not proof of insertion.
    """
    sent = 0
    for ch in str(text):
        sent += cua_send_unicode(ch)
        if interval_ms > 0:
            time.sleep(interval_ms / 1000.0)
    return sent
