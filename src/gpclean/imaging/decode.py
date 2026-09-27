"""Decode one photo into the single canonical "work" image every fingerprint derives from.

The work image is RGB, sRGB, orientation-corrected, at most ``work_edge`` pixels on its long
edge, and carries no metadata. Deriving pHash, signature, features, thumbnails and the CLIP
input from this one image (PLAN section 5, "Fingerprint") means two copies of a photo only
have to agree once, here, instead of in five different pipelines.

Safety (design-security S-11): only the six formats we need are allowed, so Pillow never
reaches its EPS (Ghostscript), PSD or TIFF decoders; the pixel limit is enforced explicitly;
truncated files raise instead of being silently padded.
"""

from __future__ import annotations

import functools
import io
import logging
import math
import struct
import warnings
import zlib
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageCms, ImageFile

log = logging.getLogger(__name__)

# The only decoders Pillow may use. HEIF is provided by pillow-heif's opener. MPO files are
# recognised by the JPEG opener (Pillow has no separate MPO opener), see _open_formats().
ALLOWED_FORMATS = ["JPEG", "MPO", "PNG", "WEBP", "GIF", "HEIF"]

# Formats whose multi-frame flag means a real animation. MPO "frames" are a second image
# (gain map / depth / stereo view) and HEIF sequences are not decoded, so neither counts.
_ANIMATION_FORMATS = frozenset({"GIF", "PNG", "WEBP"})

# EXIF tag numbers (see the EXIF 2.32 spec).
_TAG_ORIENTATION = 0x0112
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_DATETIME = 306
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825
_TAG_EXPOSURE_TIME = 33434
_TAG_DT_ORIGINAL = 36867
_TAG_OFFSET_ORIGINAL = 36881
_TAG_SUBSEC_ORIGINAL = 37521

# EXIF orientation -> the transpose that turns stored pixels into displayed pixels
# (same table as PIL.ImageOps.exif_transpose).
_ORIENTATION_OPS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}

# EXIF text values are attacker-influenced (shared/downloaded images); keep them short.
_MAX_TEXT = 128

_EXIF_KEYS = ("dt", "offset", "subsec", "dt_any", "make", "model", "has_camera_exif", "lat", "lon")

# Exceptions that, raised while loading pixels, mean "this file is broken" (see decode()).
_DATA_ERRORS = (SyntaxError, struct.error, IndexError, EOFError, zlib.error, ValueError,
                RuntimeError)

_state: dict = {"heif_registered": False, "max_pixels": None, "formats": None}


@dataclass
class Decoded:
    """Result of :func:`decode`. ``work`` is the canonical image; the rest is metadata."""

    work: Image.Image  # RGB, sRGB, orientation applied, long edge <= work_edge
    width: int  # full-resolution dims after orientation
    height: int
    stored_w: int  # dims as stored in the file (before orientation / draft)
    stored_h: int
    orientation: int  # EXIF orientation 1..8 (1 for HEIF: pillow-heif already applied it)
    format: str  # PIL format name
    animated: bool
    exif: dict = field(default_factory=dict)  # see _EXIF_KEYS; missing values are None


def configure_pillow(max_pixels: int = 250_000_000) -> None:
    """Register the HEIF opener and set Pillow's safety limits. Safe to call repeatedly.

    Call once per worker process (the scan pool initializer does). :func:`decode` calls it
    with the default limit if nobody has yet, so a forgotten call cannot disable the limit.
    """
    if not _state["heif_registered"]:
        import pillow_heif

        # Thumbnails embedded in HEIC files are never needed; skipping them saves memory.
        pillow_heif.register_heif_opener(thumbnails=False)
        # decode() enforces the pixel limit itself as a hard error, so Pillow's softer
        # "between 1x and 2x the limit" warning would only be noise in the private log.
        warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
        _state["heif_registered"] = True
        _state["formats"] = _open_formats()
    Image.MAX_IMAGE_PIXELS = int(max_pixels)
    # Never pad a truncated file with grey: a half-decoded photo would fingerprint wrongly.
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    _state["max_pixels"] = int(max_pixels)


def decode(data: bytes, *, work_edge: int = 640) -> Decoded:
    """Decode image bytes into a :class:`Decoded`. Raises on anything undecodable.

    Raises ``PIL.UnidentifiedImageError`` (an OSError) for disallowed/unknown formats,
    ``PIL.Image.DecompressionBombError`` above the configured pixel limit, and ``OSError``
    for truncated or corrupt data, including corrupt PNG IDAT streams. Corruption *inside*
    JPEG entropy-coded data, which libjpeg tolerates, decodes to garbage rather than
    raising; such a fingerprint simply matches nothing. Callers catch per item and record
    only the exception class name.
    """
    if _state["max_pixels"] is None:
        configure_pillow()
    max_pixels = _state["max_pixels"]

    with _open(data) as im:
        fmt = im.format or "UNKNOWN"
        # Record the stored size BEFORE draft(), which shrinks im.size.
        stored_w, stored_h = im.size
        if stored_w * stored_h > max_pixels:
            raise Image.DecompressionBombError(
                f"image has {stored_w * stored_h} pixels, limit is {max_pixels}"
            )
        animated = fmt in _ANIMATION_FORMATS and bool(getattr(im, "is_animated", False))

        if fmt in ("JPEG", "MPO"):
            # DCT-domain downscale: Pillow picks the largest 1/2^k scale that keeps BOTH
            # dims >= the request, so the long edge stays >= work_edge. It is the main
            # speed win (a 12 MP JPEG decodes at 1/4 scale).
            im.draft("RGB", (work_edge, work_edge))
            if max(im.size) < min(work_edge, max(stored_w, stored_h)):
                # Never expected; guard so a Pillow change cannot silently shrink W.
                raise OSError("JPEG draft produced a smaller image than requested")
        try:
            # Load frame 0 only (animated GIF/WebP/APNG and MPO all start there).
            im.load()
            # EXIF is read only AFTER the pixels load. PngImageFile.getexif() calls load()
            # itself when no eXIf chunk precedes IDAT, and _safe_getexif swallows every
            # error, so reading EXIF first would hide a corrupt IDAT and leave a
            # half-decoded image behind. JPEG/WebP/HEIF keep EXIF in im.info after load,
            # and PNG's load_end() also picks up an eXIf chunk that follows IDAT.
            exif_obj = _safe_getexif(im)
            exif = _extract_exif(exif_obj)
            if fmt == "HEIF":
                # pillow-heif applies irot/imir while decoding and resets the tag to 1;
                # transposing again would double-rotate.
                orientation = 1
            else:
                orientation = _orientation(exif_obj)
            icc = im.info.get("icc_profile")
            work = _to_work_image(im, icc, work_edge)
            if work is im:
                # _to_work_image had nothing to change and returned the opened file itself,
                # which still exposes EXIF (incl. GPS) via getexif()/applist and holds the
                # source buffer. A copy is a plain Image with no plugin state.
                work = im.copy()
        except _DATA_ERRORS as exc:
            # Decoders signal broken data with a zoo of exception types (PNG: SyntaxError,
            # libheif: ValueError/RuntimeError, ...). Normalise so callers see OSError.
            raise OSError(f"undecodable image data ({type(exc).__name__})") from exc

    op = _ORIENTATION_OPS.get(orientation)
    if op is not None:
        # Rotating the small image is cheap and gives the same pixels as rotating first.
        work = work.transpose(op)
    width, height = (stored_h, stored_w) if orientation >= 5 else (stored_w, stored_h)
    # The work image feeds thumbnails; it must never carry EXIF/ICC/XMP from the source.
    work.info = {}
    return Decoded(
        work=work,
        width=width,
        height=height,
        stored_w=stored_w,
        stored_h=stored_h,
        orientation=orientation,
        format=fmt,
        animated=animated,
        exif=exif,
    )


def _open(data: bytes) -> Image.Image:
    """Image.open restricted to the allowed formats; broken headers raise OSError."""
    try:
        return Image.open(io.BytesIO(data), formats=_state["formats"])
    except _DATA_ERRORS as exc:  # e.g. pillow-heif rejecting a malformed HEIF box
        raise OSError(f"undecodable image header ({type(exc).__name__})") from exc


def _open_formats() -> list[str]:
    """ALLOWED_FORMATS restricted to names that have a registered opener.

    Image.open(formats=...) raises KeyError on a name without an opener (it does for "MPO",
    which the JPEG opener handles), so pass only names Pillow actually knows.
    """
    Image.init()
    formats = [f for f in ALLOWED_FORMATS if f in Image.OPEN]
    if "JPEG" not in formats or "HEIF" not in formats:
        raise RuntimeError("Pillow is missing the JPEG or HEIF decoder")
    return formats


# ---------------------------------------------------------------------------------------------
# Pixels
# ---------------------------------------------------------------------------------------------


def _to_work_image(im: Image.Image, icc: bytes | None, work_edge: int) -> Image.Image:
    """Normalise mode, shrink to work_edge, convert to sRGB and flatten alpha -> RGB."""
    im = _normalise_mode(im)

    # Shrink before colour management: converting 640 px instead of 12 MP is much cheaper,
    # and the difference between resampling in P3 vs sRGB gamma space is negligible here.
    w, h = im.size
    long_edge = max(w, h)
    if long_edge > work_edge:
        scale = work_edge / long_edge
        new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
        # Pillow premultiplies alpha for RGBA/LA resizes, so edges do not bleed dark.
        # reducing_gap=3 box-reduces by an integer factor first while staying >= 3x the
        # target, then LANCZOS: visually identical, and ~2x faster on full-res PNG/HEIF.
        im = im.resize(new_size, Image.Resampling.LANCZOS, reducing_gap=3.0)

    alpha = None
    if im.mode in ("RGBA", "LA"):
        alpha = im.getchannel("A")
        im = im.convert("RGB") if im.mode == "RGBA" else im.getchannel("L")

    if icc:
        im = _icc_to_srgb(im, icc)
    if im.mode != "RGB":
        im = im.convert("RGB")

    if alpha is not None:
        # Transparent regions become white, which is how viewers usually show them.
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=alpha)
        im = flat
    return im


def _normalise_mode(im: Image.Image) -> Image.Image:
    """Map every mode Pillow can hand us to one of RGB, RGBA, L, LA or CMYK (8-bit)."""
    mode = im.mode
    if mode in ("RGB", "RGBA", "L", "LA", "CMYK"):
        return im
    if mode in ("P", "PA"):
        # Palette images may carry a transparent index; RGBA keeps it for flattening.
        return im.convert("RGBA")
    if mode.startswith("I;16") or mode == "I":
        # 16-bit grayscale PNG. Pillow's own I->L conversion clips instead of scaling, so
        # scale with numpy: 16-bit data -> top 8 bits.
        arr = np.asarray(im).astype(np.float64)
        if mode == "I" and arr.max(initial=0) <= 255:
            scaled = arr  # an "I" image that already holds 8-bit values
        else:
            scaled = arr / 257.0
        return Image.fromarray(np.clip(np.rint(scaled), 0, 255).astype(np.uint8), "L")
    if mode == "F":
        arr = np.asarray(im).astype(np.float64)
        if arr.max(initial=0) <= 1.0:
            arr = arr * 255.0
        return Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8), "L")
    if mode == "1":
        return im.convert("L")
    if mode in ("La", "RGBa"):
        return im.convert("LA" if mode == "La" else "RGBA")
    # YCbCr, RGBX, LAB, HSV, ... : let Pillow do the colour conversion.
    return im.convert("RGB")


_SRGB = ImageCms.createProfile("sRGB")


@functools.lru_cache(maxsize=32)
def _transform_for(icc: bytes, mode: str) -> ImageCms.ImageCmsTransform:
    """Build (and cache) the ICC -> sRGB transform. Phones reuse one profile for every photo."""
    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    return ImageCms.buildTransform(src, _SRGB, mode, "RGB")


def _icc_to_srgb(im: Image.Image, icc: bytes) -> Image.Image:
    """Convert ``im`` (RGB, L or CMYK) from its embedded profile to sRGB.

    A broken or mismatched profile (e.g. an RGB profile on a grayscale image) is ignored:
    the pixels are then treated as sRGB, which is what most viewers do too.
    """
    try:
        return ImageCms.applyTransform(im, _transform_for(bytes(icc), im.mode))
    except (ImageCms.PyCMSError, OSError, ValueError, TypeError) as exc:
        log.debug("ignoring unusable ICC profile (%s)", type(exc).__name__)
        return im


# ---------------------------------------------------------------------------------------------
# EXIF
# ---------------------------------------------------------------------------------------------


def _safe_getexif(im: Image.Image) -> Image.Exif | None:
    """Return the parsed EXIF block, or None when it is missing or malformed."""
    try:
        return im.getexif()
    except Exception as exc:  # malformed EXIF must never make the photo undecodable
        log.debug("unreadable EXIF (%s)", type(exc).__name__)
        return None


def _orientation(exif: Image.Exif | None) -> int:
    """EXIF orientation 1..8; anything missing or out of range means 1 (no transform)."""
    if exif is None:
        return 1
    try:
        value = int(exif.get(_TAG_ORIENTATION, 1))
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= 8 else 1


def _extract_exif(exif: Image.Exif | None) -> dict:
    """Pull the handful of EXIF fields the merge needs. Never raises."""
    out: dict = dict.fromkeys(_EXIF_KEYS)
    out["has_camera_exif"] = False
    if exif is None:
        return out
    try:
        ifd0 = exif
        sub = _get_ifd(exif, _IFD_EXIF)

        def pick(tag: int) -> str | None:
            # DateTimeOriginal & co. belong in the Exif sub-IFD, but some writers put them
            # in IFD0; accept either.
            value = _text(sub.get(tag))
            return value if value is not None else _text(ifd0.get(tag))

        out["dt"] = pick(_TAG_DT_ORIGINAL)
        out["offset"] = pick(_TAG_OFFSET_ORIGINAL)
        out["subsec"] = pick(_TAG_SUBSEC_ORIGINAL)
        out["dt_any"] = _text(ifd0.get(_TAG_DATETIME))
        out["make"] = _text(ifd0.get(_TAG_MAKE))
        out["model"] = _text(ifd0.get(_TAG_MODEL))
        exposure = sub.get(_TAG_EXPOSURE_TIME, ifd0.get(_TAG_EXPOSURE_TIME))
        out["has_camera_exif"] = bool(out["make"] or out["model"] or exposure is not None)
        out["lat"], out["lon"] = _gps(_get_ifd(exif, _IFD_GPS))
    except Exception as exc:  # partial garbage in EXIF: keep what we have
        log.debug("EXIF extraction failed (%s)", type(exc).__name__)
    return out


def _get_ifd(exif: Image.Exif, tag: int) -> dict:
    try:
        return exif.get_ifd(tag) or {}
    except Exception as exc:
        log.debug("unreadable EXIF sub-IFD (%s)", type(exc).__name__)
        return {}


def _text(value: object) -> str | None:
    """Clean an EXIF ASCII value: bytes -> str, drop NULs/padding, cap length, '' -> None."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "").strip()
    return value[:_MAX_TEXT] or None


def _gps(gps: dict) -> tuple[float | None, float | None]:
    """GPS IFD -> signed decimal (lat, lon). 0/0 (a common "no fix" value) -> (None, None)."""
    lat = _dms(gps.get(2), gps.get(1), "S", 90.0)
    lon = _dms(gps.get(4), gps.get(3), "W", 180.0)
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return None, None
    return lat, lon


def _dms(value: object, ref: object, negative_ref: str, limit: float) -> float | None:
    """(degrees, minutes, seconds) rationals + hemisphere ref -> signed decimal degrees."""
    try:
        parts = [float(v) for v in value]  # type: ignore[union-attr]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not parts or len(parts) > 3:
        return None
    parts += [0.0] * (3 - len(parts))
    deg = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    if not math.isfinite(deg) or deg > limit or deg < 0:
        return None
    ref_text = _text(ref) or ""
    return -deg if ref_text.upper().startswith(negative_ref) else deg
