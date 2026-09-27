"""Image decoding and fingerprinting: one canonical work image -> every per-photo value.

:func:`process_image` is what the scan workers call for each image member.
"""

from __future__ import annotations

from PIL import Image

from gpclean.config import ScanConfig
from gpclean.imaging.decode import Decoded, configure_pillow, decode
from gpclean.imaging.features import features
from gpclean.imaging.fingerprint import (
    hamming,
    phash64,
    sha256,
    sig_distance,
    sig_luma_std,
    sig_verify,
    signature,
)
from gpclean.imaging.thumbs import thumbs

__all__ = [
    "Decoded",
    "configure_pillow",
    "decode",
    "features",
    "hamming",
    "phash64",
    "process_image",
    "sha256",
    "sig_distance",
    "sig_luma_std",
    "sig_verify",
    "signature",
    "thumbs",
]


def process_image(data: bytes, cfg: ScanConfig) -> tuple[dict, bytes, bytes, Image.Image]:
    """Decode one image member and compute everything the scan stores for it.

    Returns ``(row, grid_webp, preview_webp, work_image)``. ``row`` holds the ``items_raw``
    columns except the member/zip fields the caller owns (member_idx, member, folder,
    folder_kind, filename, ext, file_size, crc32) and ``emb``/``err``. ``work_image`` is the
    CLIP input. Raises whatever :func:`decode` raises; the caller records the class name.
    """
    configure_pillow(cfg.max_pixels)
    dec = decode(data, work_edge=cfg.work_edge)
    work = dec.work
    ex = dec.exif
    row = {
        "format": dec.format,
        "sha256": sha256(data),
        "width": dec.width,
        "height": dec.height,
        "stored_w": dec.stored_w,
        "stored_h": dec.stored_h,
        "orientation": dec.orientation,
        "phash64": phash64(work),
        "sig": signature(work),
        "exif_dt": ex["dt"],
        "exif_offset": ex["offset"],
        "exif_subsec": ex["subsec"],
        "exif_dt_any": ex["dt_any"],
        "make": ex["make"],
        "model": ex["model"],
        "has_camera_exif": int(bool(ex["has_camera_exif"])),
        "exif_lat": ex["lat"],
        "exif_lon": ex["lon"],
        **features(work),
        "animated": int(dec.animated),
    }
    grid, preview = thumbs(work, cfg)
    return row, grid, preview, work
