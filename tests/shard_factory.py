"""Synthetic shard meta files (schema.SHARD_DDL) for merge tests.

Two ways to make shards without gpclean.scan:

* :class:`ShardWriter` writes rows you describe (images, sidecars, videos, skips). Pixel
  columns (phash64, sig, features, sha256) come from real small images run through
  :func:`gpclean.imaging.process_image`, so their values are realistic. Build images with
  :func:`textured` (a distinct, photo-like picture per seed).
* :func:`shards_from_zips` is a minimal single-process stand-in for the scanner: it classifies
  every member of every zip, fingerprints images, parses sidecars and writes one shard + thumb
  pack per zip. The merge's end-to-end fixture test uses it until ``gpclean.scan`` lands.

Everything is synthetic; url ids look like ``AF1QipFAKE...``.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from gpclean.config import ScanConfig
from gpclean.imaging import process_image
from gpclean.schema import PACK_DDL, SHARD_DDL, create_transport_db, finalize_transport_db
from gpclean.takeout.members import classify, list_entries
from gpclean.takeout.names import export_id as export_id_of
from gpclean.takeout.sidecar import parse_sidecar
from gpclean.version import CODE_VERSION, EXTRACT_VERSION

ROOT = "Takeout/Google Photos"
TEST_CFG = "testcfg000"
_SCAN_CFG = ScanConfig(clip_model="none")
_url_counter = itertools.count(1)


def fake_url_id(n: int | None = None) -> str:
    """A fake Google Photos item id (never a real one: ``AF1QipFAKE`` + digits)."""
    n = next(_url_counter) if n is None else n
    return f"AF1QipFAKE{n:028d}"


def fake_url(n: int | None = None) -> str:
    return "https://photos.google.com/photo/" + fake_url_id(n)


# ---------------------------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------------------------


def textured(seed: int, w: int = 320, h: int = 240) -> Image.Image:
    """A distinct photo-like RGB image: smooth colour noise plus random shapes."""
    rng = np.random.default_rng(seed)
    small = rng.random((6, 8, 3)) * 255
    im = Image.fromarray(small.astype(np.uint8), "RGB").resize((w, h), Image.Resampling.BICUBIC)
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        r = rng.uniform(0.05, 0.2) * min(w, h)
        colour = tuple(int(c) for c in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            d.ellipse([x - r, y - r, x + r, y + r], fill=colour)
        else:
            d.rectangle([x - r, y - r * 0.6, x + r, y + r * 0.6], fill=colour)
    return im


def encode(im: Image.Image, fmt: str = "JPEG", **params) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format=fmt, **params)
    return buf.getvalue()


def pixel_cols(data: bytes) -> dict:
    """items_raw pixel/EXIF columns of encoded image bytes (via gpclean.imaging)."""
    row, _g, _p, _w = process_image(data, _SCAN_CFG)
    return row


def image_cols(im: Image.Image, *, quality: int = 90, fmt: str = "JPEG", **overrides) -> dict:
    """Columns for an image row: fingerprints of ``im`` encoded as ``fmt``, plus overrides
    (e.g. ``width``/``height`` to pretend a larger original, ``exif_dt``, ``model``)."""
    params = {"quality": quality} if fmt == "JPEG" else {}
    data = encode(im, fmt, **params)
    cols = pixel_cols(data)
    cols["file_size"] = len(data)
    cols.update(overrides)
    return cols


# ---------------------------------------------------------------------------------------------
# Writing shards
# ---------------------------------------------------------------------------------------------


def _folder_kind(folder: str) -> str:
    return "year" if folder.startswith("Photos from ") else "album"


class ShardWriter:
    """Write one shard meta file row by row. Call :meth:`close` to write shard_info."""

    def __init__(self, path: Path, *, zip_name: str, zipkey: str | None = None, shard: int = 0,
                 cfg: str = TEST_CFG, extract_version: int | str = EXTRACT_VERSION,
                 clip_model: str = "none", export_id: str | None = None,
                 pack_name: str | None = None, extra_info: dict | None = None):
        self.path = Path(path)
        self.zip_name = zip_name
        self.zipkey = zipkey or hashlib.sha256(zip_name.encode()).hexdigest()[:12]
        self.shard = shard
        self.info = {
            "zipkey": self.zipkey, "zip_name": zip_name,
            "export_id": export_id or export_id_of(zip_name), "shard": str(shard),
            "cfg": cfg, "extract_version": str(extract_version), "code_version": CODE_VERSION,
            "clip_model": clip_model,
            "pack_name": pack_name or f"{self.zipkey}-{shard:04d}.sqlite",
            **(extra_info or {}),
        }
        self.conn = create_transport_db(self.path, SHARD_DDL)
        self._idx = itertools.count(shard * 100_000)
        self.counts = {"items": 0, "sidecars": 0, "videos": 0, "skipped": 0}

    @property
    def pack_name(self) -> str:
        return self.info["pack_name"]

    def image(self, filename: str, cols: dict, *, folder: str = "Photos from 2020",
              member_idx: int | None = None) -> int:
        """Add an image row; ``cols`` usually from :func:`image_cols`. Returns member_idx."""
        idx = next(self._idx) if member_idx is None else member_idx
        row = {"member_idx": idx, "member": f"{ROOT}/{folder}/{filename}", "folder": folder,
               "folder_kind": _folder_kind(folder), "filename": filename,
               "ext": filename.rsplit(".", 1)[-1].lower() if "." in filename else "",
               "crc32": 0, **cols}
        keys = list(row)
        self.conn.execute(f"INSERT INTO items_raw ({', '.join(keys)}) VALUES "
                          f"({', '.join('?' * len(keys))})", [row[k] for k in keys])
        self.counts["items"] += 1
        return idx

    def sidecar(self, json_name: str, *, folder: str = "Photos from 2020", title: str | None,
                taken_ts: int | None = None, url: str | None = None,
                member_idx: int | None = None, **cols) -> int:
        """Add a sidecar row (only the columns given; the rest NULL / 0)."""
        idx = next(self._idx) if member_idx is None else member_idx
        row = {"member_idx": idx, "member": f"{ROOT}/{folder}/{json_name}", "folder": folder,
               "folder_kind": _folder_kind(folder), "json_name": json_name, "title": title,
               "taken_ts": taken_ts, "url": url, "from_shared_album": 0, "from_partner": 0,
               "favorited": 0, "archived": 0, "trashed": 0, **cols}
        keys = list(row)
        self.conn.execute(f"INSERT INTO sidecars_raw ({', '.join(keys)}) VALUES "
                          f"({', '.join('?' * len(keys))})", [row[k] for k in keys])
        self.counts["sidecars"] += 1
        return idx

    def video(self, filename: str, *, folder: str = "Photos from 2020",
              file_size: int = 1000, member_idx: int | None = None) -> int:
        idx = next(self._idx) if member_idx is None else member_idx
        self.conn.execute("INSERT INTO videos_raw VALUES (?, ?, ?, ?, ?, ?)",
                          (idx, f"{ROOT}/{folder}/{filename}", folder, _folder_kind(folder),
                           filename, file_size))
        self.counts["videos"] += 1
        return idx

    def skipped(self, member: str, reason: str, member_idx: int | None = None) -> int:
        idx = next(self._idx) if member_idx is None else member_idx
        self.conn.execute("INSERT INTO skipped_raw VALUES (?, ?, ?)", (idx, member, reason))
        self.counts["skipped"] += 1
        return idx

    def close(self) -> Path:
        info = {**self.info, "n_items": str(self.counts["items"]),
                "n_sidecars": str(self.counts["sidecars"]),
                "n_videos": str(self.counts["videos"]), "n_skipped": str(self.counts["skipped"])}
        self.conn.executemany("INSERT INTO shard_info VALUES (?, ?)", sorted(info.items()))
        finalize_transport_db(self.conn)
        return self.path


# ---------------------------------------------------------------------------------------------
# A tiny scanner stand-in
# ---------------------------------------------------------------------------------------------


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def shards_from_zips(zip_dir: Path, work_dir: Path, *,
                     include_albums: bool = False) -> tuple[list[Path], Path]:
    """Scan every ``*.zip`` in ``zip_dir`` into one shard + thumb pack each (no pool, no CLIP).

    Returns ``(meta paths, pack dir)``. Mirrors what gpclean.scan stores, closely enough for
    merge tests: classification by name, images through ``process_image``, sidecars through
    ``parse_sidecar``, videos from the central directory only.
    """
    work_dir = Path(work_dir)
    pack_dir = work_dir / "thumbs"
    pack_dir.mkdir(parents=True, exist_ok=True)
    metas = []
    for zpath in sorted(Path(zip_dir).glob("*.zip")):
        zipkey = hashlib.sha256(f"{zpath.name}|{zpath.stat().st_size}".encode()).hexdigest()[:12]
        pack_name = f"{zipkey}-0000.sqlite"
        pack = create_transport_db(pack_dir / pack_name, PACK_DDL)
        writer = ShardWriter(work_dir / "work" / zipkey / "0000.meta.sqlite",
                             zip_name=zpath.name, zipkey=zipkey, pack_name=pack_name)
        with zipfile.ZipFile(zpath) as zf:
            infos = zf.infolist()
            for e in list_entries(zf):
                mc = classify(e.name, include_albums=include_albums, file_size=e.file_size)
                if mc.kind == "skip":
                    writer.skipped(e.name, mc.reason, member_idx=e.member_idx)
                elif mc.kind == "video":
                    writer.video(mc.filename, folder=mc.folder, file_size=e.file_size,
                                 member_idx=e.member_idx)
                elif mc.kind == "sidecar":
                    fields = parse_sidecar(zf.read(infos[e.member_idx]))
                    title = fields.pop("title")
                    taken = fields.pop("taken_ts")
                    url = fields.pop("url")
                    writer.sidecar(mc.filename, folder=mc.folder, title=title, taken_ts=taken,
                                   url=url, member_idx=e.member_idx, **fields)
                else:
                    data = zf.read(infos[e.member_idx])
                    try:
                        row, grid, preview, _work = process_image(data, _SCAN_CFG)
                        pack.execute("INSERT INTO t VALUES (?, ?, ?)",
                                     (e.member_idx, grid, preview))
                    except OSError as exc:
                        row = {"err": type(exc).__name__}
                    row.update(file_size=e.file_size, crc32=e.crc)
                    writer.image(mc.filename, row, folder=mc.folder, member_idx=e.member_idx)
        finalize_transport_db(pack)
        writer.info["pack_sha256"] = _sha_file(pack_dir / pack_name)
        writer.info["pack_size"] = str((pack_dir / pack_name).stat().st_size)
        metas.append(writer.close())
    return metas, pack_dir
