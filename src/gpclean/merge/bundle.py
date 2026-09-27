"""Build the review bundle from shard meta files, and regroup an existing bundle.

Bundle directory::

    index.sqlite         schema.INDEX_DDL (written as a transport DB: DELETE journal, VACUUM)
    embeddings.f16.npy   (N, dim) float16, row = items.emb_row (only items with an embedding)
    thumbs/*.sqlite      thumb packs (uploaded by the scan in CI; copied here locally)
    manifest.json        written LAST: a bundle without it is incomplete

Grouping, bursts and scores are computed by :func:`analyse` from index rows alone, so
:func:`regroup` (a new threshold without rescanning) runs the very same code on the rows it
reads back from ``index.sqlite``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from gpclean.config import MergeConfig
from gpclean.merge.bursts import Burst, burst_extras, find_bursts
from gpclean.merge.collapse import Item, collapse
from gpclean.merge.group import DupGroup, dup_item, find_groups, is_graphic
from gpclean.merge.load import Loaded, ShardMismatch, load_shards, pack_name_of
from gpclean.merge.localtime import local_fields, video_day
from gpclean.merge.pairing import Paired, pair_all
from gpclean.merge.scores import junk_scores, screenshot_score
from gpclean.schema import INDEX_DDL, JUNK_CATEGORIES, create_transport_db, finalize_transport_db
from gpclean.takeout.names import year_of_folder
from gpclean.version import CODE_VERSION, EXTRACT_VERSION, INDEX_SCHEMA

log = logging.getLogger(__name__)

EMB_DIM_DEFAULT = 512
INDEX_NAME = "index.sqlite"
EMB_NAME = "embeddings.f16.npy"
MANIFEST_NAME = "manifest.json"
JUNK_SHOW_MIN = 0.5  # "flagged" in stats, same as bundle_read.JUNK_DEFAULT_MIN

# Columns copied unchanged from items_raw into items.
_RAW_COLS = ("member", "member_idx", "folder", "folder_kind", "filename", "ext", "format",
             "file_size", "sha256", "width", "height", "orientation", "phash64", "sig",
             "make", "model", "has_camera_exif", "exif_dt", "exif_subsec", "lap_var",
             "luma_mean", "luma_std", "frac_dark", "frac_bright", "flat_frac", "colorfulness")
_LOCAL_COLS = ("taken_ts", "taken_src", "local_date", "local_time", "day_uncertain", "year")
_SIDECAR_COLS = ("url", "match_rule", "match_conf", "origin_folder", "device_type", "shared",
                 "partner", "favorited", "archived", "description", "people")


# ---------------------------------------------------------------------------------------------
# Analysis (shared by build_bundle and regroup)
# ---------------------------------------------------------------------------------------------


@dataclass
class Analysis:
    """Everything derived from item rows: graphic flags, groups, bursts, scores."""

    graphic: list[bool]
    groups: list[DupGroup]
    group_stats: dict
    bursts: list[Burst]
    scores: list[tuple[int, str, float, str]]  # (row index, category, score, reason)


def analyse(rows: list[dict], mcfg: MergeConfig) -> Analysis:
    """Screenshot score -> is_graphic -> duplicate groups -> bursts -> junk scores.

    ``rows`` are items rows (with ``item_id`` set) plus ``export_id``.
    """
    shot = [screenshot_score(r) for r in rows]
    graphic = [is_graphic(s[0], r.get("sig"), r.get("flat_frac")) for s, r in zip(shot, rows)]
    groups, gstats = find_groups([dup_item(r, g) for r, g in zip(rows, graphic)], mcfg)

    dup_of = {k: gi for gi, g in enumerate(groups) for k in g.members}
    keepers = {g.seed for g in groups}
    bursts = find_bursts(rows, graphic, mcfg, dup_of, keepers)

    dup_extra: dict[int, tuple[float, str]] = {}
    for g in groups:
        keeper_id = rows[g.seed]["item_id"]
        keeper_url = rows[g.seed].get("url")
        for k in g.members[1:]:
            url = rows[k].get("url")
            # Certain only per member: this copy and the keeper must both have a url, and
            # different ones. A url-less member (or the same url) may be the keeper's own
            # library item in another export, and deleting it would delete the keeper. That
            # two *other* members have distinct urls (g.deletable) proves nothing about it.
            if g.deletable and keeper_url and url and url != keeper_url:
                dup_extra[k] = (1.0, f"duplicate of #{keeper_id}")
            else:
                dup_extra[k] = (0.6, f"possible duplicate of #{keeper_id} (may be the same item)")
    scores = junk_scores(rows, graphic=graphic, shot=shot,
                         burst_extra=burst_extras(bursts, rows), dup_extra=dup_extra)
    return Analysis(graphic=graphic, groups=groups, group_stats=gstats, bursts=bursts,
                    scores=scores)


def analysis_stats(a: Analysis) -> dict:
    """Stats keys that change with a regroup."""
    junk_flagged = Counter(cat for _k, cat, score, _r in a.scores if score >= JUNK_SHOW_MIN)
    gs = a.group_stats
    return {
        "graphic_items": sum(a.graphic),
        "dup_groups_by_kind": dict(Counter(g.kind for g in a.groups)),
        "dup_groups_deletable": sum(1 for g in a.groups if g.deletable),
        "dup_extra_members": sum(len(g.members) - 1 for g in a.groups),
        "bursts_by_source": dict(Counter(b.source for b in a.bursts)),
        "burst_frames": sum(len(b.members) for b in a.bursts),
        "junk_flagged": {c: junk_flagged.get(c, 0) for c in JUNK_CATEGORIES},
        "skipped_buckets": gs.get("skipped_buckets", 0),
        "skipped_bucket_items": gs.get("skipped_bucket_items", 0),
        "unexamined_pairs": gs.get("unexamined_pairs", 0),
        "candidate_pairs": gs.get("candidate_pairs", 0),
        "near_edges": gs.get("near_edges", 0),
        "rejected_pairs": {"aspect": gs.get("rejected_aspect", 0),
                           "sig": gs.get("rejected_sig", 0),
                           "capture": gs.get("rejected_capture", 0)},
    }


def _write_analysis(conn: sqlite3.Connection, rows: list[dict], a: Analysis) -> None:
    for gid, g in enumerate(a.groups, start=1):
        conn.execute("INSERT INTO dup_groups VALUES (?, ?, ?, ?, ?, ?)",
                     (gid, g.key, g.kind, rows[g.seed]["item_id"], len(g.members),
                      int(g.deletable)))
        conn.executemany("INSERT INTO dup_members VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (gid, rows[k]["item_id"], *(_round(v) for v in g.metrics[k]), int(k == g.seed))
            for k in g.members])
    for bid, b in enumerate(a.bursts, start=1):
        conn.execute("INSERT INTO bursts VALUES (?, ?, ?, ?, ?, ?)",
                     (bid, b.source, rows[b.best]["item_id"], len(b.members), b.start_ts,
                      b.end_ts))
        conn.executemany("INSERT INTO burst_members VALUES (?, ?, ?, ?)", [
            (bid, rows[k]["item_id"], rank, _round(b.quality[k]))
            for rank, k in enumerate(b.members)])
    conn.executemany("INSERT INTO scores VALUES (?, ?, ?, ?)",
                     [(rows[k]["item_id"], cat, score, reason)
                      for k, cat, score, reason in a.scores])
    conn.executemany("UPDATE items SET is_graphic = ? WHERE item_id = ?",
                     [(int(g), r["item_id"]) for g, r in zip(a.graphic, rows)])


def _round(v):
    return round(v, 4) if isinstance(v, float) else v


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------


def _item_row(item: Item, zip_ids: dict[str, int]) -> dict:
    r = item.rec
    row = {c: r.get(c) for c in _RAW_COLS + _LOCAL_COLS + _SIDECAR_COLS}
    row["item_uid"] = item.uid
    row["zip_id"] = zip_ids[r["zipkey"]]
    row["export_id"] = r["export_id"]
    # Location: what Google Photos shows (sidecar geoData, which includes manual edits), then
    # the sidecar's copy of the EXIF location, then the file's own EXIF.
    for lat_key, lon_key in (("sc_lat", "sc_lon"), ("sc_lat_exif", "sc_lon_exif"),
                             ("exif_lat", "exif_lon")):
        if r.get(lat_key) is not None and r.get(lon_key) is not None:
            row["lat"], row["lon"] = r[lat_key], r[lon_key]
            break
    else:
        row["lat"] = row["lon"] = None
    row["albums_n"] = len(item.albums)
    row["albums"] = json.dumps(sorted(item.albums), ensure_ascii=False) if item.albums else None
    row["pack"] = r.get("pack")
    row["pack_row"] = r["member_idx"]
    row["emb"] = r.get("emb")
    row["is_graphic"] = 0
    return row


def _order_key(row: dict) -> tuple:
    # item_ids follow the calendar (undated last), which makes id ranges meaningful to people.
    return (row["local_date"] is None, row["local_date"] or "", row["local_time"] or "",
            row["item_uid"])


@dataclass
class Unindexed:
    """Library items the index has no row for, counted per local day (``videos_by_day``).

    The review site offers "select the whole day" only on days where this count is 0, so
    every item that sits on a day in Google Photos but cannot be reviewed here must be
    counted: videos, year-folder images that failed to decode, and year-folder raw /
    other-format / oversized files. Items with no known day are counted in ``undated``; while
    that is above 0 no day is provably complete.
    """

    by_day: dict[str, int]
    videos: int  # distinct videos
    videos_undated: int
    images: int  # distinct unindexed images
    undated: int  # videos + images without a day


def _unindexed(paired: Paired, tz_name: str, indexed_urls: set[str]) -> Unindexed:
    """One count per library item: records sharing a url are one item; url-less ones by
    (kind, name, size, day). An image whose url is also indexed (it decoded in another
    export) is reviewed through that index row and not counted again."""
    seen: set = set()
    days: Counter = Counter()
    n = Counter()
    undated = Counter()
    media = [("video", v) for v in paired.videos]
    media += [("image", m) for m in paired.unindexed_images
              if not (m.get("url") and m["url"] in indexed_urls)]
    for kind, rec in media:
        day = video_day(rec.get("sc_taken_ts"), tz_name)
        ident = ("u", rec["url"]) if rec.get("url") else (
            "n", kind, rec["filename"].casefold(), rec.get("file_size"), day)
        if ident in seen:
            continue
        seen.add(ident)
        n[kind] += 1
        if day is None:
            undated[kind] += 1
        else:
            days[day] += 1
    return Unindexed(by_day=dict(sorted(days.items())), videos=n["video"],
                     videos_undated=undated["video"], images=n["image"],
                     undated=sum(undated.values()))


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, streamed."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _file_entry(root: Path, rel: str) -> dict:
    p = root / rel
    return {"path": rel, "size": p.stat().st_size, "sha256": sha256_file(p)}


def _embeddings(rows: list[dict]) -> np.ndarray:
    """Stack the embeddings of the items that have one and set their ``emb_row``."""
    vecs = []
    dim = None
    for row in rows:
        blob = row.pop("emb", None)
        row["emb_row"] = None
        if blob is None:
            continue
        vec = np.frombuffer(bytes(blob), dtype="<f2")
        if dim is None:
            dim = vec.size
        if vec.size != dim or dim == 0:
            log.warning("ignoring an embedding of unexpected size")
            continue
        row["emb_row"] = len(vecs)
        vecs.append(vec)
    if not vecs:
        return np.zeros((0, EMB_DIM_DEFAULT), dtype=np.float16)
    return np.stack(vecs).astype(np.float16)


def _pack_entries(loaded: Loaded, pack_dir: Path | None, out_dir: Path,
                  pack_hashes: dict | None) -> list[dict]:
    """Manifest entries for the thumb packs of the loaded shards (copying local packs in)."""
    thumbs = out_dir / "thumbs"
    entries = []
    by_name = {pack_name_of(s): s for s in loaded.shards}
    for name in sorted(by_name):
        info = by_name[name]
        dst = thumbs / name
        if pack_dir is not None:
            src = Path(pack_dir) / name
            if src.is_file() and (not dst.exists() or src.resolve() != dst.resolve()):
                thumbs.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        given = (pack_hashes or {}).get(name)
        if isinstance(given, str):
            given = {"sha256": given}
        if given and given.get("sha256"):
            size = given.get("size", info.get("pack_size"))
            entries.append({"path": f"thumbs/{name}", "size": int(size) if size else None,
                            "sha256": given["sha256"]})
        elif dst.is_file():
            entries.append(_file_entry(out_dir, f"thumbs/{name}"))
        elif info.get("pack_sha256"):
            # CI: the scan uploaded the pack straight into the bundle folder and recorded its
            # hash, so the merge never downloads it.
            size = info.get("pack_size")
            entries.append({"path": f"thumbs/{name}", "size": int(size) if size else None,
                            "sha256": info["pack_sha256"]})
        else:
            log.warning("thumb pack has no hash and is not present locally")
    return entries


def _write_manifest(out_dir: Path, manifest: dict) -> None:
    tmp = out_dir / (MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out_dir / MANIFEST_NAME)


# ---------------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------------


def build_bundle(meta_paths: list[Path], pack_dir: Path | None, out_dir: Path,
                 mcfg: MergeConfig, *, cfg_hash: str, clip_model: str,
                 expected_shards: int | None = None, pack_hashes: dict | None = None) -> dict:
    """Merge shard meta files into a bundle in ``out_dir``; returns the manifest dict.

    ``pack_dir`` holds the thumb packs when they are local (they are copied to
    ``out_dir/thumbs`` unless already there). ``pack_hashes`` ({pack name: {"sha256",
    "size"}} or {name: sha256}) overrides hashing. ``expected_shards`` larger than the number
    of meta files marks the bundle partial.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A stale manifest must never vouch for a half-written new bundle.
    (out_dir / MANIFEST_NAME).unlink(missing_ok=True)

    loaded = load_shards(list(meta_paths))
    if loaded.cfg is not None and cfg_hash and loaded.cfg != cfg_hash:
        raise ShardMismatch("shards were scanned with a different cfg than requested")
    tz = mcfg.tz_fallback

    paired = pair_all(loaded, tz)
    for rec in paired.images:
        rec.update(local_fields(
            exif_dt=rec.get("exif_dt"), exif_offset=rec.get("exif_offset"),
            exif_dt_any=rec.get("exif_dt_any"), sidecar_ts=rec.get("sc_taken_ts"),
            folder_year=year_of_folder(rec["folder"]), tz_name=tz))
    items = collapse(paired.images, paired.albums_by_url)

    zip_keys = sorted(loaded.zips, key=lambda z: (loaded.zips[z]["export_id"],
                                                  loaded.zips[z]["name"], z))
    zip_ids = {z: k for k, z in enumerate(zip_keys, start=1)}
    pairs = sorted(((item, _item_row(item, zip_ids)) for item in items),
                   key=lambda p: _order_key(p[1]))
    rows = [row for _item, row in pairs]
    for item_id, row in enumerate(rows, start=1):
        row["item_id"] = item_id
    emb = _embeddings(rows)

    analysis = analyse(rows, mcfg)
    unindexed = _unindexed(paired, tz, {r["url"] for r in rows if r.get("url")})

    n_shards = len(loaded.shards)
    missing = max(0, int(expected_shards) - n_shards) if expected_shards is not None else 0
    extract_version = _int_or(loaded.extract_version, EXTRACT_VERSION)
    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "index_schema": str(INDEX_SCHEMA), "cfg": cfg_hash,
        "extract_version": str(extract_version), "code_version": CODE_VERSION,
        "created_at": created_at, "threshold": str(mcfg.threshold), "clip_model": clip_model,
        "tz_fallback": tz, "partial": "1" if missing else "0", "missing_shards": str(missing),
    }
    stats = {
        "n_items": len(rows),
        "aliases": sum(len(i.aliases) for i in items),
        "alias_reasons": dict(Counter(reason for i in items for _r, reason in i.aliases)),
        "image_records": len(paired.images),
        "image_errors": paired.image_errors,
        "trashed_excluded": paired.trashed,
        "sidecars": len(loaded.sidecars),
        "sidecars_unpaired": paired.unmatched_sidecars,
        "album_sidecars": paired.album_sidecars,
        "pairing_rules": dict(sorted(paired.rules.items())),
        "pairing_conf": dict(sorted(paired.conf.items())),
        "items_with_url": sum(1 for r in rows if r.get("url")),
        "items_with_embeddings": int(emb.shape[0]),
        "skipped": dict(sorted(loaded.skipped.items())),
        "video_records": len(paired.videos),
        "videos_distinct": unindexed.videos,
        "videos_undated": unindexed.videos_undated,
        # Not in the index but on a day in Google Photos (counted in videos_by_day).
        "unindexed_images": unindexed.images,
        # Videos + unindexed images with no known day: while > 0, no day is provably complete.
        "unindexed_undated": unindexed.undated,
        "shards": n_shards,
        "missing_shards": missing,
        **analysis_stats(analysis),
    }

    index_path = out_dir / INDEX_NAME
    conn = create_transport_db(index_path, INDEX_DDL)
    try:
        conn.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
        conn.executemany("INSERT INTO zips VALUES (?, ?, ?, ?, ?)", [
            (zip_ids[z], z, loaded.zips[z]["name"], loaded.zips[z]["export_id"],
             loaded.zips[z]["size"]) for z in zip_keys])
        cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
        conn.executemany(
            f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [tuple(row.get(c) for c in cols) for row in rows])
        alias_rows = []
        for item, row in pairs:
            for rec, reason in item.aliases:
                alias_rows.append((row["item_id"], zip_ids[rec["zipkey"]], rec["member"], reason))
        conn.executemany("INSERT INTO item_aliases VALUES (?, ?, ?, ?)", sorted(alias_rows))
        # Despite its name the table counts every unindexed library item per day (see
        # Unindexed): it is what the site checks before offering a whole-day selection.
        conn.executemany("INSERT INTO videos_by_day VALUES (?, ?)", unindexed.by_day.items())
        _write_analysis(conn, rows, analysis)
        conn.executemany("INSERT INTO stats VALUES (?, ?)",
                         [(k, json.dumps(v, sort_keys=True)) for k, v in sorted(stats.items())])
        finalize_transport_db(conn)
    except BaseException:
        conn.close()
        raise

    np.save(out_dir / EMB_NAME, emb, allow_pickle=False)
    files = [_file_entry(out_dir, INDEX_NAME), _file_entry(out_dir, EMB_NAME)]
    files += _pack_entries(loaded, pack_dir, out_dir, pack_hashes)
    manifest = {
        "index_schema": INDEX_SCHEMA, "cfg": cfg_hash, "extract_version": extract_version,
        "code_version": CODE_VERSION, "created_at": created_at, "partial": bool(missing),
        "missing_shards": missing, "clip_model": clip_model,
        "counts": _counts(stats, analysis, len(unindexed.by_day)),
        "files": files,
    }
    _write_manifest(out_dir, manifest)  # last: its presence means the bundle is complete
    log.info("bundle: items=%d groups=%d bursts=%d partial=%d", len(rows),
             len(analysis.groups), len(analysis.bursts), int(bool(missing)))
    return manifest


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _counts(stats: dict, a: Analysis, video_days: int) -> dict:
    return {"items": stats["n_items"], "aliases": stats["aliases"],
            "dup_groups": len(a.groups), "bursts": len(a.bursts), "scores": len(a.scores),
            "videos": stats["videos_distinct"], "video_days": video_days,
            "embeddings": stats["items_with_embeddings"]}


# ---------------------------------------------------------------------------------------------
# Regroup
# ---------------------------------------------------------------------------------------------

_ANALYSIS_TABLES = ("dup_members", "dup_groups", "burst_members", "bursts", "scores")


def _read_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT i.*, z.export_id AS export_id FROM items i JOIN zips z ON z.zip_id = i.zip_id"
        " ORDER BY i.item_id")]
    conn.row_factory = None
    return rows


def regroup(bundle: Path, threshold: int) -> dict:
    """Recompute duplicate groups, bursts and scores from ``index.sqlite`` alone.

    Rewrites the analysis tables, ``items.is_graphic``, ``meta.threshold`` and the affected
    ``stats``, then updates the index's size and sha256 in ``manifest.json``. Returns
    ``{"dup_groups", "exact", "near", "bursts", "scores"}``.

    The work happens on a copy that then replaces the index in one step: the review site and
    the MCP server open the index as immutable, so it must never change under them, and a
    crash midway must leave the old index (still matching its manifest entry) in place. Stop
    ``serve`` / ``mcp`` before regrouping; on Windows the replace fails loudly while they
    hold the file open.
    """
    bundle = Path(bundle)
    index_path = bundle / INDEX_NAME
    if not index_path.is_file():
        raise FileNotFoundError("bundle has no index.sqlite")
    tmp_path = bundle / (INDEX_NAME + ".tmp")
    tmp_path.unlink(missing_ok=True)
    shutil.copyfile(index_path, tmp_path)
    try:
        analysis = _regroup_db(tmp_path, int(threshold))
        os.replace(tmp_path, index_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    manifest_path = bundle / MANIFEST_NAME
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = _file_entry(bundle, INDEX_NAME)
        files = [f for f in manifest.get("files", []) if f.get("path") != INDEX_NAME]
        manifest["files"] = [entry] + files
        counts = manifest.setdefault("counts", {})
        counts.update(dup_groups=len(analysis.groups), bursts=len(analysis.bursts),
                      scores=len(analysis.scores))
        _write_manifest(bundle, manifest)
    kinds = Counter(g.kind for g in analysis.groups)
    return {"dup_groups": len(analysis.groups), "exact": kinds.get("exact", 0),
            "near": kinds.get("near", 0), "bursts": len(analysis.bursts),
            "scores": len(analysis.scores)}


def _regroup_db(path: Path, threshold: int) -> Analysis:
    """Re-run :func:`analyse` on the rows of the index at ``path`` and rewrite it there."""
    base = MergeConfig()
    conn = sqlite3.connect(str(path))
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        mcfg = dataclasses.replace(base, threshold=threshold,
                                   tz_fallback=meta.get("tz_fallback") or base.tz_fallback)
        rows = _read_rows(conn)
        analysis = analyse(rows, mcfg)
        conn.execute("PRAGMA journal_mode = DELETE")
        with conn:
            for table in _ANALYSIS_TABLES:
                conn.execute(f"DELETE FROM {table}")  # constant table names
            _write_analysis(conn, rows, analysis)
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('threshold', ?)",
                         (str(mcfg.threshold),))
            conn.executemany("INSERT OR REPLACE INTO stats VALUES (?, ?)",
                             [(k, json.dumps(v, sort_keys=True))
                              for k, v in sorted(analysis_stats(analysis).items())])
        conn.execute("VACUUM")
    finally:
        conn.close()
    return analysis


def cli_regroup(bundle, threshold) -> int:
    """``gpclean regroup --bundle DIR --threshold N``: prints a one-line summary.

    Stop ``gpclean serve`` / ``gpclean mcp`` on this bundle first (see :func:`regroup`).
    """
    res = regroup(Path(bundle), int(threshold))
    print(f"regrouped at threshold {int(threshold)}: {res['dup_groups']} duplicate groups "
          f"({res['exact']} exact, {res['near']} near), {res['bursts']} bursts, "
          f"{res['scores']} scores")
    return 0
