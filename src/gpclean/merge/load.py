"""Read every shard meta file (schema.SHARD_DDL) into plain Python lists for the merge.

The merge is global (pairing, collapse and grouping all cross zip and shard boundaries), so
all shards are loaded at once. At 100k photos that is a few hundred MB of dicts, well within a
runner's 16 GB; the sidecars' ``raw`` JSON column (up to 64 KiB each) is deliberately *not*
loaded.

Shards from different scan configs or extractor versions describe incomparable fingerprints,
so mixing them is refused rather than silently merged.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from gpclean.schema import open_readonly
from gpclean.takeout import names
from gpclean.takeout.members import classify

log = logging.getLogger(__name__)

# sidecars_raw columns the merge uses (everything except the bulky ``raw``).
_SIDECAR_COLS = (
    "member_idx", "member", "folder", "folder_kind", "json_name", "title", "taken_ts",
    "creation_ts", "lat", "lon", "alt", "lat_exif", "lon_exif", "url", "description", "people",
    "origin_folder", "device_type", "from_shared_album", "from_partner", "favorited", "archived",
    "trashed", "err",
)


# Skip reasons of members that are library items of their own but never get an index row
# (not decoded). In a year folder they still sit on a day in Google Photos, so the merge pairs
# them with their sidecars and counts them per day next to videos.
UNINDEXED_SKIP_REASONS = ("raw", "other_image", "too_large")


class ShardMismatch(ValueError):
    """Shards that cannot be merged together (different cfg / extractor, duplicates)."""


@dataclass
class Loaded:
    """Everything the merge needs from the shards, as plain lists of dicts.

    Every image / sidecar / video dict carries the ``*_raw`` columns plus ``zipkey``,
    ``zip_name``, ``export_id`` and ``shard``; images also carry ``pack`` (their thumb pack's
    file name).
    """

    shards: list[dict] = field(default_factory=list)  # shard_info of each shard
    cfg: str | None = None
    extract_version: str | None = None
    clip_model: str | None = None
    zips: dict[str, dict] = field(default_factory=dict)  # zipkey -> {name, export_id, size}
    images: list[dict] = field(default_factory=list)
    sidecars: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # skipped_raw.reason -> count
    # Year-folder members skipped for an UNINDEXED_SKIP_REASONS reason: member_idx, member,
    # reason, folder, folder_kind, filename (+ the common keys).
    skipped_media: list[dict] = field(default_factory=list)


def pack_name_of(info: dict) -> str:
    """Thumb pack file name of a shard (shard_info ``pack_name``, else the standard name)."""
    name = info.get("pack_name")
    if name:
        return str(name)
    return f"{info['zipkey']}-{int(info.get('shard') or 0):04d}.sqlite"


def _one(values: set, what: str):
    """The single value of ``values`` (ignoring None), or raise ShardMismatch."""
    present = {v for v in values if v not in (None, "")}
    if len(present) > 1:
        raise ShardMismatch(f"shards come from different {what} values ({len(present)})")
    return next(iter(present), None)


def _skipped_media(row: dict) -> dict | None:
    """A skipped_raw row with its folder and filename, or None unless it is in a year folder.

    skipped_raw keeps only the member path; the classifier recovers folder and filename from
    it the same way the scan did (album copies are copies of year-folder items: not counted).
    """
    mc = classify(row["member"], include_albums=True)
    if mc.folder is None or mc.filename is None or mc.folder_kind != "year":
        return None
    return {**row, "folder": mc.folder, "folder_kind": mc.folder_kind, "filename": mc.filename}


def read_shard_info(path: Path) -> dict:
    """The ``shard_info`` key/value table of one meta file."""
    conn = open_readonly(path)
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM shard_info")}
    finally:
        conn.close()


def load_shards(meta_paths: list[Path]) -> Loaded:
    """Load all shard meta files. Raises ShardMismatch on mixed cfg / extract_version,
    a zipkey that names two different zips, or the same shard given twice."""
    out = Loaded()
    seen: set[tuple[str, int]] = set()
    for path in sorted(Path(p) for p in meta_paths):
        conn = open_readonly(path)
        try:
            info = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM shard_info")}
            zipkey = info.get("zipkey")
            zip_name = info.get("zip_name")
            if not zipkey or not zip_name:
                raise ShardMismatch("shard_info lacks zipkey / zip_name")
            shard = int(info.get("shard") or 0)
            if (zipkey, shard) in seen:
                raise ShardMismatch("the same shard was given twice")
            seen.add((zipkey, shard))
            export = info.get("export_id") or names.export_id(zip_name)
            known = out.zips.get(zipkey)
            if known is not None and known["name"] != zip_name:
                raise ShardMismatch("one zipkey names two different zips")
            if known is None:
                size = info.get("zip_size")
                out.zips[zipkey] = {"zipkey": zipkey, "name": zip_name, "export_id": export,
                                    "size": int(size) if size not in (None, "") else None}
            info = {**info, "zipkey": zipkey, "shard": shard, "export_id": export}
            out.shards.append(info)
            pack = pack_name_of(info)
            common = {"zipkey": zipkey, "zip_name": zip_name, "export_id": export,
                      "shard": shard}

            for r in conn.execute("SELECT * FROM items_raw ORDER BY member_idx"):
                out.images.append({**dict(r), **common, "pack": pack})
            cols = ", ".join(_SIDECAR_COLS)
            for r in conn.execute(f"SELECT {cols} FROM sidecars_raw ORDER BY member_idx"):
                out.sidecars.append({**dict(r), **common})
            for r in conn.execute("SELECT * FROM videos_raw ORDER BY member_idx"):
                out.videos.append({**dict(r), **common})
            for r in conn.execute("SELECT reason, COUNT(*) FROM skipped_raw GROUP BY reason"):
                out.skipped[r[0]] += int(r[1])
            marks = ", ".join("?" * len(UNINDEXED_SKIP_REASONS))
            for r in conn.execute(f"SELECT member_idx, member, reason FROM skipped_raw WHERE "
                                  f"reason IN ({marks}) ORDER BY member_idx",
                                  UNINDEXED_SKIP_REASONS):
                media = _skipped_media(dict(r))
                if media is not None:
                    out.skipped_media.append({**media, **common})
        finally:
            conn.close()

    out.cfg = _one({s.get("cfg") for s in out.shards}, "cfg")
    out.extract_version = _one({s.get("extract_version") for s in out.shards}, "extract_version")
    out.clip_model = _one({s.get("clip_model") for s in out.shards}, "clip_model")
    # Counts only: member names are personal data.
    log.info("loaded shards=%d zips=%d images=%d sidecars=%d videos=%d", len(out.shards),
             len(out.zips), len(out.images), len(out.sidecars), len(out.videos))
    return out
