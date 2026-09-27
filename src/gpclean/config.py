"""Scan/merge configuration, config hashing, and input validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from gpclean.version import EXTRACT_VERSION, SIG_VERSION

CLIP_MODELS = ("b32", "b16", "none")
THRESHOLD_CHOICES = (2, 3, 4, 5)

_FOLDER_RE = re.compile(r"^[A-Za-z0-9 _.()-]{1,100}(/[A-Za-z0-9 _.()-]{1,100})*$")


@dataclass(frozen=True)
class ScanConfig:
    """Everything that changes what a scan writes. Hashed into ``cfg``."""

    include_albums: bool = False
    clip_model: str = "b32"
    photos_per_shard: int = 1000
    work_edge: int = 640
    thumb_grid_px: int = 160
    thumb_grid_q: int = 70
    thumb_preview_px: int = 640
    thumb_preview_q: int = 75
    max_member_bytes: int = 200 * 1024 * 1024
    max_pixels: int = 250_000_000

    def __post_init__(self) -> None:
        if self.clip_model not in CLIP_MODELS:
            raise ValueError("clip_model must be one of " + ", ".join(CLIP_MODELS))
        if not (10 <= self.photos_per_shard <= 100_000):
            raise ValueError("photos_per_shard out of range")

    def cfg_hash(self) -> str:
        """Short stable hash naming the Drive work/bundle folders for this config."""
        payload = {"extract_version": EXTRACT_VERSION, "sig_version": SIG_VERSION, **asdict(self)}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:10]


@dataclass(frozen=True)
class MergeConfig:
    """Merge-time knobs. NOT part of cfg: changing them only needs a (cheap) re-merge."""

    threshold: int = 3
    aspect_tol: float = 0.02
    bucket_cap: int = 500
    sig_mad_max: float = 4.0
    sig_block_max: float = 8.0
    sig_chroma_max: float = 10.0
    capture_window_s: float = 60.0
    burst_window_s: float = 3.0
    burst_phash_max: int = 10
    tz_fallback: str = "America/New_York"

    def __post_init__(self) -> None:
        if self.threshold not in THRESHOLD_CHOICES:
            raise ValueError("threshold must be one of 2..5")


def validate_folder(folder: str) -> str:
    """Validate a Drive folder path from a workflow input (public, attacker-influenced text)."""
    if not isinstance(folder, str) or not _FOLDER_RE.match(folder):
        raise ValueError("invalid folder name")
    parts = folder.split("/")
    if any(p in (".", "..") or p.startswith("-") or p != p.strip() for p in parts):
        raise ValueError("invalid folder name")
    return folder


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    v = (value or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("", "0", "false", "no", "off"):
        return False
    raise ValueError("invalid boolean")
