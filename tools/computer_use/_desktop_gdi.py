"""GDI window capture for computer_use (Windows).

Kept separate from _desktop_win.py on purpose: the bitmap/GDI layer is a different concern
from mouse/keyboard/window enumeration, and each dep stays small and readable.

Deps exec in SEPARATE namespaces (loader.globals_builder.install_shared_deps execs each dep
in its own globals), so this module must NOT import a sibling dep. The screen rectangle the
fallback needs is therefore passed IN by the caller (cua_screenshot already reads it) instead
of calling _desktop_win.cua_window_rect here.

Public API only: install_shared_deps merges non-underscore symbols into the tool's exec
globals, so a private name here would be invisible to the tools.
"""

import ctypes
import logging
import os
from ctypes import wintypes

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Gdi")

#: GDI only exists on Windows. Keeping the module importable anywhere (SERVER tool discovery
#: imports every tool file) means every call answers honestly instead of raising deep inside.
#: Handles are PRIVATE: install_shared_deps merges public dep symbols into every tool's exec
#: globals, and _desktop_win also holds a user32 handle - two public copies would collide.
_HAVE_GDI = os.name == "nt" and hasattr(ctypes, "windll")

_user32 = ctypes.windll.user32 if _HAVE_GDI else None
_gdi32 = ctypes.windll.gdi32 if _HAVE_GDI else None

PW_RENDERFULLCONTENT = 2
SRCCOPY = 0x00AA0020

#: One capture ceiling: a window bigger than this is refused rather than allocating a
#: multi-hundred-megabyte bitmap.
MAX_CAPTURE_DIM = 8000


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


def _read_bitmap_rows(memdc, bmp, width: int, height: int):
    """Copy a selected bitmap out and return it as RGB bytes. Returns (bytes_or_None, rows).

    Two things the raw buffer needs, both found live on 2026-11-07:
      * the stride is PADDED to a 4-byte boundary (1229 px * 3 = 3687 -> stride 3688), so
        reading width*3*height shifts every row by one byte and permutes every colour
      * a 24-bit DIB stores BLUE first, so the channels must be swapped to RGB
    Without both, the PNG is a colour-shifted image and nothing in it can be matched by
    colour - vision on a captured window was misleading.
    """
    stride = (width * 3 + 3) & ~3
    info = _BitmapInfo()
    info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height   # negative = top-down rows
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 24
    info.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(stride * height)
    rows = _gdi32.GetDIBits(memdc, bmp, 0, height, buf, ctypes.byref(info), 0)
    if rows <= 0:
        return None, int(rows)
    return rgb_from_dib(buf.raw, width, height), int(rows)


def rgb_from_dib(raw: bytes, width: int, height: int) -> bytes:
    """Padded BGR DIB bytes -> tightly packed INTERLEAVED RGB bytes.

    PUBLIC so a caller can re-check the conversion on its own bytes. Drops each row's stride
    padding and swaps blue and red per pixel.

    The swap must stay INTERLEAVED (R,G,B per pixel). Collecting the three channel planes and
    concatenating them produces a scrambled image - that is exactly what happened live on
    2026-11-07: the page's green button band came back with no green at all, which then made
    the capture look 'greyscale' and sent the wrong conclusion down the fallback path.
    """
    stride = (width * 3 + 3) & ~3
    row = width * 3
    out = bytearray(row * height)
    for i in range(height):
        src = raw[i * stride:i * stride + row]
        dst = out[i * row:i * row + row]
        dst[0::3] = src[2::3]   # red
        dst[1::3] = src[1::3]   # green
        dst[2::3] = src[0::3]   # blue
        out[i * row:i * row + row] = dst
    return bytes(out)


def has_colour(raw: bytes, width: int, height: int, step: int = 5, grey_tolerance: int = 3) -> bool:
    """True when the capture contains real colour, i.e. it is not a flat grey image.

    PUBLIC: cua_screenshot reports the capture method, and this decides whether a
    PrintWindow draw is usable. Chrome composites its web content on the GPU, and
    PrintWindow(RENDERFULLCONTENT) returned SUCCESS while drawing only greyscale pixels
    (verified live 2026-11-07: the page's green button came out as grey, and the page's
    signature colours were absent). A successful call that yields no colour is not a
    screenshot of the content, so the caller falls back to a screen-region capture.

    Sampling every *step* pixels keeps this cheap on a 4K window. A genuinely greyscale app
    (a plain text editor) can read as grey; the fallback then still captures the same
    rectangle from the screen, and the method is reported, never hidden.
    """
    if not raw or width <= 0 or height <= 0:
        return False
    row = width * 3
    coloured = 0
    sampled = 0
    for y in range(0, height, step):
        base = y * row
        for x in range(0, width, step):
            i = base + x * 3
            if i + 2 >= len(raw):
                continue
            r, g, b = raw[i], raw[i + 1], raw[i + 2]
            sampled += 1
            if max(r, g, b) - min(r, g, b) > grey_tolerance:
                coloured += 1
    if sampled == 0:
        return False
    return coloured / sampled >= 0.02


def capture_is_empty(raw: bytes, width: int, height: int) -> bool:
    """True when the draw produced a BLANK canvas: one flat near-white or near-black colour.

    A single solid colour is NOT automatically empty - a window whose content really is one
    colour (a solid splash, a coloured panel) is content. What PrintWindow hands back when it
    draws nothing is a flat white or flat black bitmap, so only those two blanks count.
    """
    if not raw:
        return True
    row = width * 3
    distinct = set()
    for y in range(0, height, 7):
        base = y * row
        for x in range(0, width, 7):
            i = base + x * 3
            if i + 2 < len(raw):
                distinct.add((raw[i], raw[i + 1], raw[i + 2]))
            if len(distinct) > 8:
                return False
    if len(distinct) > 8:
        return False
    # One flat colour: blank only if it is white-ish or black-ish.
    r, g, b = next(iter(distinct))
    return all(c >= 240 for c in (r, g, b)) or all(c <= 10 for c in (r, g, b))


def _blit_screen_region(memdc, screen_rect: dict, width: int, height: int) -> bool:
    """Copy the screen pixels at *screen_rect* into the selected bitmap. False if impossible.

    This is the fallback path, so it must be reported: it captures whatever is visible at
    that rectangle (occlusion and other windows included), unlike PrintWindow.
    """
    if not screen_rect:
        return False
    screen_dc = _user32.GetDC(0)
    if not screen_dc:
        return False
    try:
        return bool(_gdi32.BitBlt(memdc, 0, 0, width, height, screen_dc,
                                  screen_rect["left"], screen_rect["top"], SRCCOPY))
    finally:
        _user32.ReleaseDC(0, screen_dc)


def gdi_available() -> bool:
    """True when this process can reach the GDI capture APIs."""
    return _HAVE_GDI and _gdi32 is not None and _user32 is not None


def cua_capture_window(hwnd: int, width: int, height: int, screen_rect: dict = None):
    """Capture one window to RGB bytes. Returns (bytes, method) or (None, reason).

    PrintWindow(RENDERFULLCONTENT) draws the window itself, so it works even when another
    window overlaps it. Two honest fallbacks, both REPORTED as 'screen_region_fallback':
      * PrintWindow refuses entirely, or draws an EMPTY bitmap (GetDIBits returns 0 rows or a
        flat image - happens on console/GPU-composited windows; an empty capture is not a
        screenshot)
      * PrintWindow draws something with NO COLOUR while a screen-region capture of the same
        rectangle has colour - a GPU-composited window can hand back a greyscale draw.

    A greyscale app (a plain text editor) is NOT treated as a failed capture: if the fallback
    has no colour either, the PrintWindow draw is delivered as-is. The fallback copies the
    screen region under the window rectangle, so occlusion and other windows are included -
    the method is never hidden from the caller. When nothing usable can be captured, the
    reason says so and points at cua_get_state instead of pixels.
    """
    if not gdi_available():
        return None, "GDI is not available in this process (non-Windows host)"
    if width <= 0 or height <= 0:
        return None, "window has zero size (minimized or off-screen)"
    if width > MAX_CAPTURE_DIM or height > MAX_CAPTURE_DIM:
        return None, f"window too large to capture ({width}x{height})"

    hdc = _user32.GetWindowDC(hwnd)
    if not hdc:
        return None, "GetWindowDC failed (invalid window handle)"
    memdc = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, width, height)
    if not memdc or not bmp:
        _user32.ReleaseDC(hwnd, hdc)
        return None, "GDI buffer allocation failed"
    try:
        _gdi32.SelectObject(memdc, bmp)
        method = "print_window"
        drew = _user32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)
        raw, _rows = _read_bitmap_rows(memdc, bmp, width, height) if drew else (None, 0)

        if raw is None or capture_is_empty(raw, width, height):
            if not _blit_screen_region(memdc, screen_rect, width, height):
                return None, ("PrintWindow produced no usable pixels and no screen fallback is "
                              "available (GPU-composited window - use cua_get_state to read the "
                              "control tree instead of pixels)")
            raw, _rows = _read_bitmap_rows(memdc, bmp, width, height)
            if raw is None:
                return None, ("capture produced no pixel rows (GPU-composited window - "
                              "use cua_get_state to read the control tree instead of pixels)")
            method = "screen_region_fallback"
        elif not has_colour(raw, width, height) and screen_rect:
            # A colourless draw may be a GPU-composited window rather than a greyscale app.
            if _blit_screen_region(memdc, screen_rect, width, height):
                alt, _rows = _read_bitmap_rows(memdc, bmp, width, height)
                if alt is not None and has_colour(alt, width, height):
                    raw = alt
                    method = "screen_region_fallback"
        return raw, method
    finally:
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(memdc)
        _user32.ReleaseDC(hwnd, hdc)
