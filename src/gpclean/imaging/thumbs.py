"""Grid and preview thumbnails (WebP) for the review site and contact sheets.

Thumbnails are the only image bytes that ever leave the runner, so they must carry no
metadata at all: no EXIF (GPS, camera, dates), no XMP, no ICC profile (design-security S-14).
We build them from a metadata-free copy and then check the RIFF container to be sure.
"""

from __future__ import annotations

import io
import struct

from PIL import Image

from gpclean.config import ScanConfig

# RIFF chunk ids that would carry metadata inside a WebP file.
_METADATA_CHUNKS = frozenset({b"EXIF", b"XMP ", b"ICCP"})


def _fit(im: Image.Image, long_edge: int) -> Image.Image:
    """Downscale so the long edge is at most ``long_edge`` (LANCZOS); never upscale."""
    w, h = im.size
    if max(w, h) <= long_edge:
        return im
    scale = long_edge / max(w, h)
    return im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)


def _encode_webp(im: Image.Image, quality: int) -> bytes:
    """Encode as lossy WebP from a pixel-only copy, then verify no metadata chunk slipped in."""
    # Rebuilding from raw pixels drops im.info entirely (exif, icc_profile, xmp, ...), so
    # nothing the source file carried can reach the encoder even by default.
    clean = Image.frombytes("RGB", im.size, im.convert("RGB").tobytes())
    buf = io.BytesIO()
    clean.save(buf, format="WEBP", quality=quality, method=4)
    data = buf.getvalue()
    if webp_metadata_chunks(data):
        raise RuntimeError("WebP thumbnail unexpectedly contains metadata")
    return data


def webp_metadata_chunks(data: bytes) -> set[bytes]:
    """Return the metadata chunk ids (EXIF / XMP / ICCP) present in a WebP file.

    Walks the RIFF chunk list; a malformed container counts as containing metadata, so the
    caller fails closed.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return {b"????"}
    found: set[bytes] = set()
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        if chunk_id in _METADATA_CHUNKS:
            found.add(chunk_id)
        pos += 8 + size + (size & 1)  # chunks are padded to even length
    if pos != len(data):
        found.add(b"????")
    return found


def thumbs(work: Image.Image, cfg: ScanConfig) -> tuple[bytes, bytes]:
    """Return (grid, preview) WebP bytes: long edges ``thumb_grid_px`` / ``thumb_preview_px``."""
    grid = _encode_webp(_fit(work, cfg.thumb_grid_px), cfg.thumb_grid_q)
    preview = _encode_webp(_fit(work, cfg.thumb_preview_px), cfg.thumb_preview_q)
    return grid, preview
