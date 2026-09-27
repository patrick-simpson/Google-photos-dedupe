"""Global sidecar pairing: give every image (and video) its own Takeout JSON, if it has one.

The rules themselves live in :func:`gpclean.takeout.sidecar.pair`; this module builds its
inputs from the loaded shards and copies the paired sidecar's fields onto the records.

- Media and JSON can sit in different zip parts, so everything is paired at once, per
  (export_id, folder).
- Videos take part too, so that a live photo's MOV claims its own sidecar instead of leaving
  it for the HEIC to compete for.
- Album-folder sidecars are always present (they are tiny, and scanned even when album media
  are not). Their url tells which albums a library item is in.
- So do year-folder members the scan skipped as ``raw`` / ``other_image`` / ``too_large``:
  they are library items of their own and must claim their own sidecars.
- A photo whose sidecar says ``trashed`` is in Google Photos' trash and is dropped.
- Library items that exist in Google Photos but get no index row (year-folder images that
  failed to decode, and those skipped members) are returned as ``unindexed_images`` with
  their sidecar's url and time, so the bundle can count them per day next to videos: a day
  that looks fully reviewed must not hide an unreviewed item.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from gpclean.merge.load import Loaded
from gpclean.merge.localtime import exif_epoch
from gpclean.takeout.sidecar import MediaRef, SidecarRef, pair

log = logging.getLogger(__name__)

# Sidecar fields copied onto a paired image, as (record key, sidecars_raw column).
_ATTACH = (
    ("url", "url"), ("sc_title", "title"), ("sc_taken_ts", "taken_ts"),
    ("sc_lat", "lat"), ("sc_lon", "lon"), ("sc_lat_exif", "lat_exif"),
    ("sc_lon_exif", "lon_exif"), ("description", "description"), ("people", "people"),
    ("origin_folder", "origin_folder"), ("device_type", "device_type"),
    ("shared", "from_shared_album"), ("partner", "from_partner"),
    ("favorited", "favorited"), ("archived", "archived"),
)


@dataclass
class Paired:
    """Pairing outcome. ``images`` / ``videos`` are new record dicts (trashed ones removed)."""

    images: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)
    albums_by_url: dict[str, set[str]] = field(default_factory=dict)
    rules: Counter = field(default_factory=Counter)  # "R1".."R6"/"none" over images
    conf: Counter = field(default_factory=Counter)  # high/low/none over images
    unmatched_sidecars: int = 0  # year-folder sidecars nobody claimed
    album_sidecars: int = 0
    # Library items with no index row: {filename, folder, file_size, url, sc_taken_ts, why}.
    unindexed_images: list[dict] = field(default_factory=list)
    trashed: int = 0  # images + videos (+ skipped media) dropped: their sidecar says trashed
    image_errors: int = 0  # images that failed to decode (kept out of the index)


def _media_refs(loaded: Loaded, tz_name: str) -> list[MediaRef]:
    refs: list[MediaRef] = []
    for i, im in enumerate(loaded.images):
        # EXIF with its offset is a true instant; without one the owner's zone is the best
        # guess, and pair() allows a day plus 14 h of slack either way.
        ets = exif_epoch(im.get("exif_dt"), im.get("exif_offset"), tz_name)
        refs.append(MediaRef(("i", i), im["export_id"], im["folder"], im["filename"], ets))
    for i, v in enumerate(loaded.videos):
        refs.append(MediaRef(("v", i), v["export_id"], v["folder"], v["filename"], None))
    for i, m in enumerate(loaded.skipped_media):
        refs.append(MediaRef(("s", i), m["export_id"], m["folder"], m["filename"], None))
    return refs


def _unindexed(rec: dict, sc: dict | None, why: str) -> dict:
    return {"filename": rec["filename"], "folder": rec["folder"],
            "file_size": rec.get("file_size"), "why": why,
            "url": sc.get("url") if sc else None,
            "sc_taken_ts": sc.get("taken_ts") if sc else None}


def _sidecar_refs(loaded: Loaded) -> list[SidecarRef]:
    return [SidecarRef(j, s["export_id"], s["folder"], s["json_name"], s.get("title"),
                       s.get("taken_ts"))
            for j, s in enumerate(loaded.sidecars)]


def _attach(rec: dict, sc: dict | None, rule: str, conf: str) -> dict:
    out = dict(rec)
    for key, col in _ATTACH:
        out[key] = sc.get(col) if sc is not None else None
    for flag in ("shared", "partner", "favorited", "archived"):
        out[flag] = int(out[flag] or 0)
    out["match_rule"], out["match_conf"] = rule, conf
    return out


def pair_all(loaded: Loaded, tz_name: str) -> Paired:
    """Pair every image and video with its sidecar and attach the sidecar's fields."""
    result = pair(_media_refs(loaded, tz_name), _sidecar_refs(loaded))
    out = Paired()
    claimed: set[int] = set()

    for i, im in enumerate(loaded.images):
        p = result[("i", i)]
        sc = loaded.sidecars[p.sidecar_key] if p.sidecar_key is not None else None
        if sc is not None:
            claimed.add(p.sidecar_key)
            if sc.get("trashed"):
                out.trashed += 1
                continue
        if im.get("err") or im.get("sha256") is None:
            # No pixels, no fingerprint: nothing to review. It still took part in pairing so
            # that its sidecar could not be claimed by a sibling, and it is still a library
            # item on its day (album copies are copies of a year-folder record).
            out.image_errors += 1
            if im.get("folder_kind") == "year":
                out.unindexed_images.append(_unindexed(im, sc, "error"))
            continue
        rec = _attach(im, sc, p.rule, p.conf)
        rec["sidecar_idx"] = p.sidecar_key
        out.rules[p.rule] += 1
        out.conf[p.conf] += 1
        out.images.append(rec)

    for i, v in enumerate(loaded.videos):
        p = result[("v", i)]
        sc = loaded.sidecars[p.sidecar_key] if p.sidecar_key is not None else None
        if sc is not None:
            claimed.add(p.sidecar_key)
            if sc.get("trashed"):
                out.trashed += 1
                continue
        out.videos.append({**v, "url": sc.get("url") if sc else None,
                           "sc_taken_ts": sc.get("taken_ts") if sc else None})

    for i, m in enumerate(loaded.skipped_media):
        p = result[("s", i)]
        sc = loaded.sidecars[p.sidecar_key] if p.sidecar_key is not None else None
        if sc is not None:
            claimed.add(p.sidecar_key)
            if sc.get("trashed"):
                out.trashed += 1
                continue
        out.unindexed_images.append(_unindexed(m, sc, m["reason"]))

    albums: dict[str, set[str]] = defaultdict(set)
    for j, s in enumerate(loaded.sidecars):
        if s.get("folder_kind") == "album":
            out.album_sidecars += 1
            # Album sidecars carry the same url as the year-folder record; trashed ones are
            # not album memberships any more.
            if s.get("url") and not s.get("trashed"):
                albums[s["url"]].add(s["folder"])
        elif j not in claimed:
            out.unmatched_sidecars += 1
    out.albums_by_url = dict(albums)
    log.info("pairing: images=%d videos=%d unindexed=%d unmatched_sidecars=%d trashed=%d "
             "errors=%d", len(out.images), len(out.videos), len(out.unindexed_images),
             out.unmatched_sidecars, out.trashed, out.image_errors)
    return out
