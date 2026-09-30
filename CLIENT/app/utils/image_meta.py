"""Image metadata extraction for the preview panel (EXIF, GPS, size on disk).

ADDED (2026-09-18): powers GET /api/file-metadata so the UI can show a rich
metadata card under a previewed image: resolution, format, file size on disk,
modification time, camera EXIF data and - when present - GPS coordinates with
a ready-made Google Maps link.

Design notes:
  * Pillow is already a hard project requirement (requirements.txt), so no new
    dependency is introduced here.
  * Every extraction step is individually guarded: one broken tag must never
    fail the whole endpoint.
  * Pure helper functions are kept free of FastAPI imports so they can be unit
    tested without an app instance.
"""

import os
from datetime import datetime


# EXIF tags we surface as "interesting" camera/scene metadata (tag id -> label).
# Anything else is skipped to keep the card compact; GPS is handled separately.
_INTERESTING_TAGS = {
    0x010F: ("Make", None),          # Camera manufacturer
    0x0110: ("Model", None),         # Camera model
    0x9003: ("Date Taken", "date"),  # DateTimeOriginal (fallback handled by 0x0132)
    0x0132: ("Modified", "date"),    # DateTime fallback for non-camera images
    0xA002: ("Pixel X-Size", None),
    0xA003: ("Pixel Y-Size", None),
    0x829A: ("Exposure Time", "exposure"),
    0x829D: ("F-Number", None),
    0x8827: ("ISO Speed", None),
    0x920A: ("Focal Length (mm)", None),
    0xA403: ("Flash", "flash"),
    0xA406: ("White Balance", None),
    0x9286: ("Light Source", None),
    0x0112: ("Orientation", "orientation"),
}

# Flash states per the CIPA EXIF bitfield: bit 0 = fired, bits 1-2 = return status,
# bit 3 = on/compulsory/auto mode, bit 4 = off, bit 5 = no flash function.
# (AUDIT FIX 2026-08-23: the old code read bits 4-5 as a 2-bit state and mislabeled
#  e.g. value 16 'Off, did not fire' as 'Fired'. Now exact known values are mapped,
#  with the return-status bits (1-2) ignored on fallback - verified against exiftool.)
_FLASH_STATES = {
    0x00: "No flash",
    0x01: "Fired",
    0x08: "On, did not fire",
    0x09: "On, fired",
    0x10: "Off, did not fire",
    0x18: "Auto, did not fire",
    0x19: "Auto, fired",
    0x20: "No flash function",
}

# EXIF Orientation tag values -> short human text.
_ORIENTATIONS = {
    1: "Normal",
    2: "Mirrored horizontally",
    3: "Rotated 180°",
    4: "Flipped vertically",
    5: "Transposed (90° CW + mirror)",
    6: "Rotated 90° CW",
    7: "Transposed (90° CCW + mirror)",
    8: "Rotated 90° CCW",
}


def _to_number(value):
    """Convert an EXIF value to a plain number.

    Handles ints, floats, IFDRational (a tuple subclass with numerator/
    denominator) and lists of those (e.g. GPS DMS triples). Returns None when
    the value cannot be reduced to a number.
    """
    try:
        # IFDRational first - it IS a 2-tuple but must divide, not flatten.
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            num = float(value.numerator)
            den = float(value.denominator or 0)
            return num / den if den else None
        if isinstance(value, (list, tuple)):
            flat = [_to_number(v) for v in value]
            if any(f is None for f in flat):
                return None
            return flat
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_exif_value(tag_id: int, raw):
    """Render one EXIF tag value as a short human-readable string."""
    kind = _INTERESTING_TAGS.get(tag_id, (None, "raw"))[1]

    if kind == "date":
        # EXIF dates are 'YYYY:MM:DD HH:MM:SS' strings.
        s = str(raw).strip()
        return s.replace(":", "-", 2) if len(s) >= 10 else (s or None)

    if kind == "exposure":
        n = _to_number(raw)
        secs = None
        if isinstance(n, list):
            # Rational pair (numerator, denominator), e.g. (1, 80).
            secs = n[0] / n[1] if len(n) > 1 and n[1] else None
        elif isinstance(n, float):
            secs = n
        if not secs or secs <= 0:
            return str(raw)
        if secs >= 1:
            return f"{secs:g} s"
        return f"1/{max(1, round(1 / secs))} s"

    if kind == "flash":
        n = _to_number(raw)
        if isinstance(n, float):
            v = int(n)
            label = _FLASH_STATES.get(v) or _FLASH_STATES.get(v & ~0b110)
            return label if label is not None else f"flags={v:08b}"
        return str(raw)

    if kind == "orientation":
        n = _to_number(raw)
        if isinstance(n, float):
            return _ORIENTATIONS.get(int(n), f"value {int(n)}")
        return str(raw)

    # Generic: number (incl. rational like F-Number 16/1 -> 16).
    n = _to_number(raw)
    if isinstance(n, float):
        return f"{n:g}"
    if isinstance(n, list):
        return ", ".join(f"{x:g}" for x in n)
    s = str(raw).strip()
    return s or None


def extract_gps(exif) -> dict:
    """Extract GPS coordinates from a Pillow Exif object.

    Returns {"lat": float|None, "lng": float|None, "google_maps_url": str|None}.
    Coordinates are decimal degrees (7 decimals ~= 1 cm precision). The Google
    Maps URL is only set when BOTH lat and lng could be resolved.
    """
    result = {"lat": None, "lng": None, "google_maps_url": None}
    if exif is None:
        return result

    # Standard layout: GPS sub-IFD referenced from IFD0 tag 0x8825 (GPSInfo).
    gps_info = None
    try:
        from PIL import ExifTags
        gps_tag_id = int(getattr(ExifTags.IFD, "GPSInfo", 0x8825))
        if hasattr(exif, "get_ifd"):
            gps_info = exif.get_ifd(gps_tag_id)
    except Exception:
        gps_info = None

    if not gps_info or not isinstance(gps_info, dict):
        return result

    # GPS sub-IFD tag ids (constants per the EXIF spec).
    try:
        from PIL import ExifTags
        ids = {name: int(val) for name, val in vars(ExifTags.GPS).items() if isinstance(val, int)}
        lat_ref_id = ids.get("GPSLatitudeRef", 1)
        lat_id = ids.get("GPSLatitude", 2)
        lng_ref_id = ids.get("GPSLongitudeRef", 3)
        lng_id = ids.get("GPSLongitude", 4)
    except Exception:
        lat_ref_id, lat_id, lng_ref_id, lng_id = 1, 2, 3, 4

    def _dms_to_decimal(dms, max_deg=90.0):
        """Convert [deg, min, sec] (each possibly rational) to decimal degrees.

        max_deg bounds the sanity check: 90 for latitude, 180 for longitude."""
        n = _to_number(dms)
        if not isinstance(n, list) or len(n) < 3:
            return None
        deg, minute, sec = float(n[0]), float(n[1]), float(n[2])
        if not (0 <= deg <= max_deg):
            return None
        return round(deg + minute / 60.0 + sec / 3600.0, 7)

    lat = _dms_to_decimal(gps_info.get(lat_id), 90.0)
    lng = _dms_to_decimal(gps_info.get(lng_id), 180.0)
    if lat is not None:
        try:
            ref = str(gps_info.get(lat_ref_id) or "N").upper()[:1]
            if ref == "S":
                lat = -lat
        except Exception:
            pass
    if lng is not None:
        try:
            ref = str(gps_info.get(lng_ref_id) or "E").upper()[:1]
            if ref == "W":
                lng = -lng
        except Exception:
            pass

    result["lat"] = lat
    result["lng"] = lng
    if lat is not None and lng is not None:
        result["google_maps_url"] = f"https://www.google.com/maps?q={lat:.7f},{lng:.7f}"
    return result


def _human_size(num_bytes) -> str:
    """Format a byte count as a short human-readable string."""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return "?"


def extract_image_metadata(full_path: str) -> dict:
    """Build the full metadata payload for one file on disk.

    Never raises: any failure degrades to partial data + an 'error' note so the
    UI can still show what it has (e.g. size/resolution without EXIF).
    """
    meta = {
        "path": None,
        "name": os.path.basename(full_path),
        "file": {},
        "image": {},
        "exif": [],
        "gps": {"lat": None, "lng": None, "google_maps_url": None},
        "error": None,
    }

    # ---- File system facts (always available) --------------------------------
    try:
        st = os.stat(full_path)
        meta["file"] = {
            "size_bytes": int(st.st_size),
            "size_human": _human_size(st.st_size),
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except OSError as e:
        meta["error"] = f"stat failed: {e}"

    # ---- Image facts via Pillow -----------------------------------------------
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - Pillow is a hard requirement
        meta["error"] = (meta["error"] or "") + f" Pillow not available ({e})"
        return meta

    exif = None
    try:
        with Image.open(full_path) as img:
            width = int(getattr(img, "width", 0) or 0)
            height = int(getattr(img, "height", 0) or 0)

            # Pixel dimensions sometimes live only in EXIF - fall back to them.
            try:
                exif = img.getexif() if hasattr(img, "getexif") else None
            except Exception:
                exif = None
            if exif is not None and (width == 0 or height == 0):
                px = _to_number(exif.get(0xA002))
                py = _to_number(exif.get(0xA003))
                if width == 0 and isinstance(px, float):
                    width = int(px)
                if height == 0 and isinstance(py, float):
                    height = int(py)

            meta["image"] = {
                "format": getattr(img, "format", None),
                "mode": getattr(img, "mode", None),
                "width": width,
                "height": height,
            }

    except Exception as e:
        # Not a decodable image (or corrupt): keep what we have, report the issue.
        meta["image"] = {}
        meta["error"] = (meta["error"] or "") + f" image decode failed: {e}"

    # ---- EXIF tags -------------------------------------------------------------
    if exif is not None:
        for tag_id in sorted(exif.keys()):
            label = _INTERESTING_TAGS.get(tag_id)
            if not label:
                continue
            name, kind = label
            try:
                value = _format_exif_value(tag_id, exif.get(tag_id))
            except Exception:
                value = None
            if value in (None, "", 0):
                continue
            meta["exif"].append({"tag": name, "value": str(value)})

    # ---- GPS --------------------------------------------------------------------
    try:
        meta["gps"] = extract_gps(exif)
    except Exception as e:
        meta["error"] = (meta["error"] or "") + f" gps: {e}"

    return meta


def is_image_file(filename: str) -> bool:
    """Cheap extension check used by the endpoint to short-circuit non-images."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "tif", "tiff"}
