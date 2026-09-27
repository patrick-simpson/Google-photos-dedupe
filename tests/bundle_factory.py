"""Build small, fully synthetic review bundles for tests (site, MCP, sheet, search, ...).

Usage::

    from bundle_factory import make_bundle, StubTextEmbedder
    bundle_dir = make_bundle(tmp_path)              # -> tmp_path / "bundle"
    b = Bundle(bundle_dir)

The bundle is written straight from ``schema.INDEX_DDL`` / ``schema.PACK_DDL`` (no scan or
merge involved), so it is fast (well under a second) and completely deterministic.

Content, for item_id ``i`` in 1..n (``plan_items`` returns the exact rows written):

* ``year = 2018 + i % 5``; ``local_date`` = ``year-MM-DD`` with ``MM = i % 12 + 1`` and
  ``DD = i % 28 + 1``; ``local_time = HH:MM:00`` with ``HH = i % 24``, ``MM = i % 60``.
  The LAST item has no date at all (local_date/local_time/year/taken_ts NULL).
* filename ``IMG_<yyyymmdd>_<iiii>.jpg``, except ``i % 7 == 0`` -> ``Screenshot_...png``
  (screenshot 0.95, 1080x2340) and ``i % 9 == 0`` -> ``IMG-<yyyymmdd>-WA<iiii>.jpg``
  (messaging 0.9, origin_folder "WhatsApp Images"). Screenshots win when both apply.
* ``i % 5 == 0`` -> blur score 0.4 / 0.65 / 0.9 for ``i % 3`` = 0 / 1 / 2.
* url ``https://photos.google.com/photo/AF1QipFAKE<i:030d>`` and uid ``g:AF1QipFAKE...``;
  every ``i % 11 == 0`` has no url and uid ``s:<sha256hex>:<filename>``.
* GPS (lat/lon) only when ``i % 4 == 0``; shared when ``i % 13 == 0``; favorited when
  ``i % 17 == 0``.
* Duplicate groups (members > n are dropped, groups under 2 members skipped):
  group 1 ``exact`` [2, 3] keeper 2; group 2 ``near`` [4, 5, 6] keeper 4. Non-keepers get
  ``dup_extra`` 0.9.
* Burst 1 (source ``filename``) [20, 21, 22], best 21; the others get ``burst_extra`` 0.8.
* Embeddings: every item except ``i % 15 == 0`` has an ``emb_row``. Items with
  ``i % 6 == 1`` are planted near the concept "receipt" and ``i % 6 == 4`` near "beach"
  (cosine roughly 0.5-0.95, strength varies per item); everything else is random noise
  (cosine about 0 +- 0.05 to any concept). ``StubTextEmbedder().embed("receipt")`` returns
  exactly the "receipt" concept vector, so text search is testable without CLIP.
* Thumbs: real WebP images (grid 16x12, preview 64x48, one solid colour per item) spread
  over two packs ``thumbs/fakezip00001-0000.sqlite`` and ``...-0001.sqlite``.
* ``videos_by_day``: 1 video on item 10's date, 3 videos on 2017-01-01 (a day with no photos).
* ``manifest.json`` lists every file with size and sha256; ``stats`` holds a few JSON values.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from gpclean.schema import INDEX_DDL, PACK_DDL, create_transport_db, finalize_transport_db
from gpclean.version import CODE_VERSION, EXTRACT_VERSION, INDEX_SCHEMA

ZIPKEY = "fakezip00001"
ZIP_NAME = "takeout-20260101T000000Z-001.zip"
PACKS = (f"{ZIPKEY}-0000.sqlite", f"{ZIPKEY}-0001.sqlite")
CONCEPTS = ("receipt", "beach")
VIDEO_ONLY_DAY = "2017-01-01"


def concept_vector(name: str, dim: int = 512) -> np.ndarray:
    """A deterministic unit float32 vector for ``name`` (the stub 'text embedding')."""
    seed = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "little")
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


class StubTextEmbedder:
    """Deterministic text embedder with the same API as ``clipmodel.TextEmbedder``.

    ``embed(query)`` returns ``vectors[query]`` when given, else ``concept_vector(query)``.
    ``calls`` records every query so tests can check how it was used.
    """

    def __init__(self, dim: int = 512, vectors: dict[str, np.ndarray] | None = None):
        self.dim = dim
        self.vectors = dict(vectors or {})
        self.calls: list[str] = []

    def embed(self, query: str) -> np.ndarray:
        self.calls.append(query)
        if query in self.vectors:
            return np.asarray(self.vectors[query], dtype=np.float32)
        return concept_vector(query, self.dim)


@dataclass
class Plan:
    """Everything make_bundle writes, for tests that need ground truth."""

    items: list[dict]
    scores: list[tuple[int, str, float, str]]            # (item_id, category, score, reason)
    dup_groups: list[dict] = field(default_factory=list)  # {group_id, kind, members, keeper}
    bursts: list[dict] = field(default_factory=list)      # {burst_id, members, best}
    videos_by_day: dict[str, int] = field(default_factory=dict)
    concept_of: dict[int, str] = field(default_factory=dict)   # item_id -> planted concept


def _date_parts(i: int) -> tuple[int, int, int]:
    return 2018 + i % 5, i % 12 + 1, i % 28 + 1


def plan_items(n_items: int = 60, *, seed: int = 0) -> Plan:
    """Compute (without writing) the items, scores, groups and bursts of a fake bundle."""
    if n_items < 2:
        raise ValueError("n_items must be >= 2")
    items: list[dict] = []
    scores: list[tuple[int, str, float, str]] = []
    concept_of: dict[int, str] = {}
    rng = np.random.default_rng(seed)
    for i in range(1, n_items + 1):
        year, month, day = _date_parts(i)
        ymd = f"{year}{month:02d}{day:02d}"
        undated = i == n_items
        if i % 7 == 0:
            filename, ext, fmt, w, h = f"Screenshot_{ymd}-{i:04d}.png", "png", "PNG", 1080, 2340
            scores.append((i, "screenshot", 0.95, "Screenshot_ filename"))
            origin = "Screenshots"
        elif i % 9 == 0:
            filename, ext, fmt, w, h = f"IMG-{ymd}-WA{i:04d}.jpg", "jpg", "JPEG", 1600, 1200
            scores.append((i, "messaging", 0.9, "WhatsApp filename"))
            origin = "WhatsApp Images"
        else:
            filename, ext, fmt, w, h = f"IMG_{ymd}_{i:04d}.jpg", "jpg", "JPEG", 4032, 3024
            origin = "Camera"
        if i % 5 == 0:
            scores.append((i, "blur", (0.4, 0.65, 0.9)[i % 3], "low sharpness"))
        sha = hashlib.sha256(f"fake-{seed}-{i}".encode()).digest()
        if i % 11 == 0:
            url, uid = None, f"s:{sha.hex()}:{filename}"
        else:
            fake_id = f"AF1QipFAKE{i:030d}"
            url, uid = f"https://photos.google.com/photo/{fake_id}", f"g:{fake_id}"
        if undated:
            local_date = local_time = year_v = taken_ts = None
        else:
            local_date = f"{year}-{month:02d}-{day:02d}"
            local_time = f"{i % 24:02d}:{i % 60:02d}:00"
            year_v = year
            taken_ts = int(np.datetime64(f"{local_date}T{local_time}", "s").astype(np.int64))
        items.append({
            "item_id": i, "item_uid": uid, "zip_id": 1,
            "member": f"Takeout/Google Photos/Photos from {year}/{filename}",
            "member_idx": i * 2, "folder": f"Photos from {year}", "folder_kind": "year",
            "filename": filename, "ext": ext, "format": fmt, "file_size": 100_000 + i * 1000,
            "sha256": sha, "width": w, "height": h, "orientation": 1,
            "phash64": int(rng.integers(-2**62, 2**62)), "sig": rng.bytes(1152),
            "taken_ts": taken_ts, "taken_src": "none" if undated else "sidecar",
            "local_date": local_date, "local_time": local_time, "day_uncertain": 0,
            "year": year_v, "url": url, "match_rule": "R1" if url else "none",
            "match_conf": "high" if url else "none",
            "lat": 40.0 + i / 1000 if i % 4 == 0 else None,
            "lon": -74.0 if i % 4 == 0 else None,
            "origin_folder": origin, "device_type": "ANDROID_PHONE",
            "shared": int(i % 13 == 0), "partner": 0, "favorited": int(i % 17 == 0),
            "archived": 0, "description": None, "people": None, "albums_n": 0, "albums": None,
            "make": "FakeCam", "model": "F1", "has_camera_exif": int(ext == "jpg"),
            "exif_dt": None, "exif_subsec": None, "lap_var": 100.0 + i, "luma_mean": 120.0,
            "luma_std": 50.0, "frac_dark": 0.01, "frac_bright": 0.01, "flat_frac": 0.1,
            "colorfulness": 30.0, "is_graphic": int(ext == "png"),
            "pack": PACKS[i % 2], "pack_row": i * 2,
            "emb_row": None,
        })
        if i % 6 == 1:
            concept_of[i] = "receipt"
        elif i % 6 == 4:
            concept_of[i] = "beach"

    by_id = {it["item_id"]: it for it in items}
    groups = []
    for gid, kind, members in ((1, "exact", [2, 3]), (2, "near", [4, 5, 6])):
        members = [m for m in members if m <= n_items]
        if len(members) < 2:
            continue
        groups.append({"group_id": gid, "kind": kind, "members": members, "keeper": members[0]})
        for m in members[1:]:
            scores.append((m, "dup_extra", 0.9, f"duplicate of #{members[0]}"))
        if kind == "exact":   # byte-identical copies share a sha256
            for m in members[1:]:
                by_id[m]["sha256"] = by_id[members[0]]["sha256"]
    bursts = []
    members = [m for m in (20, 21, 22) if m <= n_items]
    if len(members) >= 2:
        best = 21 if 21 in members else members[0]
        bursts.append({"burst_id": 1, "members": members, "best": best})
        for m in members:
            if m != best:
                scores.append((m, "burst_extra", 0.8, f"burst of {len(members)}; #{best} sharper"))

    videos = {VIDEO_ONLY_DAY: 3}
    if n_items >= 10 and by_id[10]["local_date"]:
        videos[by_id[10]["local_date"]] = 1
    return Plan(items=items, scores=scores, dup_groups=groups, bursts=bursts,
                videos_by_day=videos, concept_of=concept_of)


def _webp(size: tuple[int, int], item_id: int) -> bytes:
    colour = ((item_id * 37) % 256, (item_id * 91) % 256, (item_id * 53) % 256)
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="WEBP", quality=60)
    return buf.getvalue()


def _embeddings(plan: Plan, dim: int, seed: int) -> np.ndarray:
    """Assign emb_row in item order and build the (N, dim) float16 matrix."""
    rng = np.random.default_rng(seed + 1)
    rows = []
    for it in plan.items:
        i = it["item_id"]
        if i % 15 == 0:
            continue
        noise = rng.standard_normal(dim).astype(np.float32)
        noise /= np.linalg.norm(noise)
        concept = plan.concept_of.get(i)
        if concept:
            strength = 0.5 + 0.45 * ((i * 7) % 10) / 9     # 0.5 .. 0.95, varies by item
            v = strength * concept_vector(concept, dim) + np.sqrt(1 - strength**2) * noise
        else:
            v = noise
        it["emb_row"] = len(rows)
        rows.append(v / np.linalg.norm(v))
    return np.asarray(rows, dtype=np.float16).reshape(len(rows), dim)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_bundle(tmp_path: Path, n_items: int = 60, *, name: str = "bundle", seed: int = 0,
                dim: int = 512, with_embeddings: bool = True, with_thumbs: bool = True,
                with_manifest: bool = True, partial: bool = False) -> Path:
    """Write a complete synthetic bundle to ``tmp_path / name`` and return that directory.

    See the module docstring for exactly what it contains. ``with_embeddings=False`` gives a
    ``--no-clip`` style bundle (no npy file, every emb_row NULL); ``with_thumbs=False`` leaves
    out the thumbs/ folder (items still reference their packs, as in a partial download).

    The target directory must not already hold files: a second call with the same ``name``
    would otherwise leave the first call's npy / packs / manifest behind and quietly break
    "missing file" tests. Pass a different ``name=`` for each bundle in one test.
    """
    out = Path(tmp_path) / name
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} already exists; pass a different name=")
    out.mkdir(parents=True, exist_ok=True)
    plan = plan_items(n_items, seed=seed)
    emb = _embeddings(plan, dim, seed) if with_embeddings else None

    conn = create_transport_db(out / "index.sqlite", INDEX_DDL)
    meta = {
        "index_schema": str(INDEX_SCHEMA), "cfg": "fakecfg000", "extract_version":
        str(EXTRACT_VERSION), "code_version": CODE_VERSION, "created_at": "2026-01-02T03:04:05Z",
        "threshold": "3", "clip_model": "b32" if with_embeddings else "none",
        "tz_fallback": "America/New_York", "partial": "1" if partial else "0",
        "missing_shards": "1" if partial else "0",
    }
    conn.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    conn.execute("INSERT INTO zips VALUES (1, ?, ?, ?, ?)",
                 (ZIPKEY, ZIP_NAME, "takeout-20260101T000000Z", 123_456_789))
    cols = list(plan.items[0])
    conn.executemany(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(it[c] for c in cols) for it in plan.items])
    conn.execute("INSERT INTO item_aliases VALUES (1, 1, ?, 'album_copy')",
                 ("Takeout/Google Photos/Trip/" + plan.items[0]["filename"],))
    conn.executemany("INSERT INTO videos_by_day VALUES (?, ?)", plan.videos_by_day.items())
    by_id = {it["item_id"]: it for it in plan.items}
    for g in plan.dup_groups:
        key = hashlib.sha1("\n".join(sorted(by_id[m]["item_uid"] for m in g["members"]))
                           .encode()).hexdigest()
        conn.execute("INSERT INTO dup_groups VALUES (?, ?, ?, ?, ?, 1)",
                     (g["group_id"], key, g["kind"], g["keeper"], len(g["members"])))
        g["group_key"] = key
        for m in g["members"]:
            exact = g["kind"] == "exact"
            conn.execute("INSERT INTO dup_members VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (g["group_id"], m, 0 if exact else 2, 0.0 if exact else 1.5,
                          0.0 if exact else 3.0, 0.0 if exact else 2.0, int(m == g["keeper"])))
    for b in plan.bursts:
        ts = [by_id[m]["taken_ts"] or 0 for m in b["members"]]
        conn.execute("INSERT INTO bursts VALUES (?, 'filename', ?, ?, ?, ?)",
                     (b["burst_id"], b["best"], len(b["members"]), min(ts), max(ts)))
        for rank, m in enumerate(sorted(b["members"], key=lambda m: m != b["best"])):
            conn.execute("INSERT INTO burst_members VALUES (?, ?, ?, ?)",
                         (b["burst_id"], m, rank, by_id[m]["lap_var"]))
    conn.executemany("INSERT INTO scores VALUES (?, ?, ?, ?)", plan.scores)
    stats = {"skipped": {"video": 4, "raw": 1}, "unexamined_pairs": 0,
             "sidecars_unpaired": 2, "n_items": n_items}
    conn.executemany("INSERT INTO stats VALUES (?, ?)",
                     [(k, json.dumps(v)) for k, v in stats.items()])
    finalize_transport_db(conn)

    if emb is not None:
        np.save(out / "embeddings.f16.npy", emb, allow_pickle=False)
    if with_thumbs:
        for pack in PACKS:
            pconn = create_transport_db(out / "thumbs" / pack, PACK_DDL)
            pconn.executemany("INSERT INTO t VALUES (?, ?, ?)", [
                (it["pack_row"], _webp((16, 12), it["item_id"]), _webp((64, 48), it["item_id"]))
                for it in plan.items if it["pack"] == pack])
            finalize_transport_db(pconn)
    if with_manifest:
        files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "manifest.json")
        manifest = {
            "index_schema": INDEX_SCHEMA, "cfg": "fakecfg000", "extract_version": EXTRACT_VERSION,
            "code_version": CODE_VERSION, "created_at": meta["created_at"], "partial": partial,
            "missing_shards": int(partial), "clip_model": meta["clip_model"],
            "counts": {"items": n_items, "dup_groups": len(plan.dup_groups),
                       "bursts": len(plan.bursts)},
            "files": [{"path": p.relative_to(out).as_posix(), "size": p.stat().st_size,
                       "sha256": _sha_file(p)} for p in files],
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return out
