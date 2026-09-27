"""Read-only access to a downloaded review bundle (used by the review site and MCP server).

A bundle directory holds ``index.sqlite`` (the merged library index), ``embeddings.f16.npy``
(CLIP image embeddings, optional), ``manifest.json`` (written last) and ``thumbs/*.sqlite``
(thumbnail packs). Nothing here ever writes to the bundle: the index and packs are opened
``mode=ro&immutable=1`` and the embeddings are memory-mapped read-only.

Items come back as plain dicts with the ``items`` columns, except that ``sig`` (an internal
1 KiB fingerprint) is left out and ``sha256`` is a hex string instead of raw bytes.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np

from gpclean.schema import JUNK_CATEGORIES, open_readonly

log = logging.getLogger(__name__)

THUMB_SIZES = ("g", "p")        # grid (160 px) / preview (640 px)
JUNK_DEFAULT_MIN = 0.5          # "counts at default thresholds" in stats()
DUP_KINDS = ("exact", "near")
_CHUNK = 500
# Pack names come from our own index, but they are still turned into a file path, so only
# plain file names are accepted (no separators, no "..").
_PACK_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,120}$")


def _casefold(value: object) -> object:
    """SQL function ``gp_casefold``: Unicode-aware lower-casing (SQLite's lower() is ASCII)."""
    return value.casefold() if isinstance(value, str) else value


def _check_page(offset: object, limit: object, max_limit: int = 10_000) -> None:
    if not (isinstance(offset, int) and offset >= 0):
        raise ValueError("offset must be a non-negative int")
    if not (isinstance(limit, int) and 1 <= limit <= max_limit):
        raise ValueError(f"limit must be 1..{max_limit}")


def _chunks(seq: Sequence, n: int = _CHUNK) -> Iterable[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Bundle:
    """A bundle directory opened read-only. Thread-safe (all SQLite use is serialised)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        index = self.path / "index.sqlite"
        if not index.is_file():
            raise FileNotFoundError("bundle has no index.sqlite")
        self._db = open_readonly(index)
        self._db.create_function("gp_casefold", 1, _casefold, deterministic=True)
        self._lock = threading.RLock()
        self._packs: dict[str, sqlite3.Connection | None] = {}
        self._emb: np.ndarray | None = None
        self._emb_loaded = False
        self.meta: dict = {r["key"]: r["value"] for r in self._db.execute("SELECT * FROM meta")}
        self.manifest: dict = self._load_manifest()
        # Select every item column except the fingerprint blob, which nobody downstream needs.
        cols = [r["name"] for r in self._db.execute("PRAGMA table_info(items)")]
        self._item_cols = ", ".join(f"i.{c}" for c in cols if c != "sig")

    def _load_manifest(self) -> dict:
        path = self.path / "manifest.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("unreadable manifest.json (%s)", type(exc).__name__)
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        """Close the index, every open pack and drop the embeddings map."""
        with self._lock:
            for conn in self._packs.values():
                if conn is not None:
                    conn.close()
            self._packs.clear()
            self._emb = None
            self._emb_loaded = False
            self._db.close()

    def __enter__(self) -> "Bundle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetchall(self, sql: str, params: Iterable[object] = ()) -> list[sqlite3.Row]:
        """Run a read-only query on the index (for gpclean.search; the DB cannot be written).

        The SQL function ``gp_casefold(text)`` is available for case-insensitive matching.
        """
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    def _item_dict(self, row: sqlite3.Row, extra_skip: Iterable[str] = ()) -> dict:
        d = dict(row)
        d.pop("sig", None)
        for k in extra_skip:
            d.pop(k, None)
        sha = d.get("sha256")
        if isinstance(sha, bytes):
            d["sha256"] = sha.hex()
        return d

    # ------------------------------------------------------------------ items

    def item(self, item_id: int) -> dict | None:
        """One item by id, or None."""
        rows = self.fetchall(f"SELECT {self._item_cols} FROM items i WHERE i.item_id = ?",
                             (int(item_id),))
        return self._item_dict(rows[0]) if rows else None

    def items(self, ids: list[int]) -> list[dict]:
        """Items for ``ids`` in the given order; unknown ids are skipped."""
        ids = [int(i) for i in ids]
        found: dict[int, dict] = {}
        for part in _chunks(list(dict.fromkeys(ids))):
            marks = ",".join("?" * len(part))
            for row in self.fetchall(
                    f"SELECT {self._item_cols} FROM items i WHERE i.item_id IN ({marks})", part):
                found[row["item_id"]] = self._item_dict(row)
        return [found[i] for i in ids if i in found]

    def by_uid(self, uid: str) -> dict | None:
        """One item by item_uid, or None."""
        rows = self.fetchall(f"SELECT {self._item_cols} FROM items i WHERE i.item_uid = ?",
                             (str(uid),))
        return self._item_dict(rows[0]) if rows else None

    def uids_to_ids(self, uids: list[str]) -> dict[str, int]:
        """Map item_uids to item_ids (uids not in this bundle are absent)."""
        out: dict[str, int] = {}
        for part in _chunks(list(dict.fromkeys(str(u) for u in uids))):
            marks = ",".join("?" * len(part))
            for row in self.fetchall(
                    f"SELECT item_uid, item_id FROM items WHERE item_uid IN ({marks})", part):
                out[row[0]] = row[1]
        return out

    def count(self) -> int:
        """Number of items in the index."""
        return int(self.fetchall("SELECT COUNT(*) FROM items")[0][0])

    # ------------------------------------------------------------------ thumbs / embeddings

    def _pack(self, name: str) -> sqlite3.Connection | None:
        """Open (once) and cache the thumb pack ``name``; None if it is missing or bad."""
        if name in self._packs:
            return self._packs[name]
        conn = None
        if _PACK_RE.match(name) and ".." not in name:
            file = name if name.endswith(".sqlite") else name + ".sqlite"
            path = self.path / "thumbs" / file
            if path.is_file():
                try:
                    conn = open_readonly(path)
                except sqlite3.Error as exc:
                    log.warning("cannot open thumb pack (%s)", type(exc).__name__)
            else:
                log.info("thumb pack not present in bundle")
        else:
            log.warning("ignoring invalid thumb pack name in index")
        self._packs[name] = conn
        return conn

    def thumb(self, item_id: int, size: str) -> bytes | None:
        """WebP bytes of the grid ("g", 160 px) or preview ("p", 640 px) thumbnail, or None."""
        if size not in THUMB_SIZES:
            raise ValueError("size must be 'g' or 'p'")
        with self._lock:
            row = self._db.execute("SELECT pack, pack_row FROM items WHERE item_id = ?",
                                   (int(item_id),)).fetchone()
            if row is None or row["pack"] is None or row["pack_row"] is None:
                return None
            conn = self._pack(row["pack"])
            if conn is None:
                return None
            # The column name is one of two constants (validated above), never user text.
            got = conn.execute(f"SELECT {size} FROM t WHERE member_idx = ?",
                               (row["pack_row"],)).fetchone()
            return bytes(got[0]) if got else None

    def embeddings(self) -> np.ndarray | None:
        """The (N, dim) float16 embedding matrix, memory-mapped read-only; None if absent."""
        with self._lock:
            if not self._emb_loaded:
                self._emb_loaded = True
                path = self.path / "embeddings.f16.npy"
                if path.is_file():
                    try:
                        emb = np.load(path, mmap_mode="r", allow_pickle=False)
                    except (OSError, ValueError, EOFError) as exc:
                        # EOFError: zero-byte or truncated file left by an interrupted copy.
                        log.warning("unreadable embeddings (%s)", type(exc).__name__)
                        emb = None
                    if emb is not None and emb.ndim == 2 and emb.shape[0] > 0 \
                            and emb.dtype == np.float16:
                        self._emb = emb
                    elif emb is not None:
                        # Anything else (float64, strings, 1-D, empty) is not what the
                        # extractor writes; treat the bundle as having no embeddings.
                        log.warning("ignoring embeddings with unexpected shape or dtype")
            return self._emb

    # ------------------------------------------------------------------ scores

    def scores_for(self, item_id: int) -> dict[str, tuple[float, str]]:
        """{category: (score, reason)} for one item."""
        rows = self.fetchall("SELECT category, score, reason FROM scores WHERE item_id = ?",
                             (int(item_id),))
        return {r[0]: (float(r[1]), r[2]) for r in rows}

    def scores_many(self, ids: list[int]) -> dict[int, dict[str, tuple[float, str]]]:
        """``scores_for`` for many items at once (items without scores are absent)."""
        out: dict[int, dict[str, tuple[float, str]]] = {}
        for part in _chunks(list(dict.fromkeys(int(i) for i in ids))):
            marks = ",".join("?" * len(part))
            for r in self.fetchall("SELECT item_id, category, score, reason FROM scores"
                                   f" WHERE item_id IN ({marks})", part):
                out.setdefault(r[0], {})[r[1]] = (float(r[2]), r[3])
        return out

    def junk(self, category: str, *, min_score: float, year: int | None = None,
             offset: int = 0, limit: int = 50) -> tuple[list[dict], int]:
        """Items scoring >= min_score in ``category``: (page, total).

        Ordered by score (highest first), then date (undated last), then item_id. Each item
        dict also carries ``score`` and ``reason`` for this category.
        """
        if category not in JUNK_CATEGORIES:
            raise ValueError("unknown junk category")
        _check_page(offset, limit)
        where = "s.category = ? AND s.score >= ?"
        params: list[object] = [category, float(min_score)]
        if year is not None:
            where += " AND i.year = ?"
            params.append(int(year))
        total = self.fetchall(
            f"SELECT COUNT(*) FROM scores s JOIN items i ON i.item_id = s.item_id WHERE {where}",
            params)[0][0]
        rows = self.fetchall(
            f"SELECT {self._item_cols}, s.score AS score, s.reason AS reason"
            f" FROM scores s JOIN items i ON i.item_id = s.item_id WHERE {where}"
            " ORDER BY s.score DESC, i.local_date IS NULL, i.local_date, i.local_time, i.item_id"
            " LIMIT ? OFFSET ?", (*params, limit, offset))
        return [self._item_dict(r) for r in rows], int(total)

    # ------------------------------------------------------------------ groups / bursts

    def _dup_members(self, group_ids: list[int]) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {g: [] for g in group_ids}
        for part in _chunks(group_ids):
            marks = ",".join("?" * len(part))
            for r in self.fetchall(
                    f"SELECT m.group_id AS _gid, {self._item_cols}, m.dist AS dist,"
                    " m.sig_mad AS sig_mad, m.sig_block AS sig_block,"
                    " m.sig_chroma AS sig_chroma, m.is_keeper AS is_keeper"
                    " FROM dup_members m JOIN items i ON i.item_id = m.item_id"
                    f" WHERE m.group_id IN ({marks})"
                    " ORDER BY m.group_id, m.is_keeper DESC, i.item_id", part):
                out[r["_gid"]].append(self._item_dict(r, ("_gid",)))
        return out

    def _groups_with_members(self, rows: list[sqlite3.Row]) -> list[dict]:
        groups = [dict(r) for r in rows]
        members = self._dup_members([g["group_id"] for g in groups])
        for g in groups:
            g["members"] = members[g["group_id"]]
        return groups

    def dup_groups(self, *, kind: str | None = None, offset: int = 0,
                   limit: int = 50) -> list[dict]:
        """Duplicate groups ordered by group_id, each with ``members`` (keeper first).

        A member is an item dict plus ``dist``, ``sig_mad``, ``sig_block``, ``sig_chroma``
        and ``is_keeper``.
        """
        if kind is not None and kind not in DUP_KINDS:
            raise ValueError("kind must be 'exact' or 'near'")
        _check_page(offset, limit)
        where, params = ("WHERE kind = ?", [kind]) if kind else ("", [])
        rows = self.fetchall(f"SELECT * FROM dup_groups {where} ORDER BY group_id"
                             " LIMIT ? OFFSET ?", (*params, limit, offset))
        return self._groups_with_members(rows)

    def dup_group(self, group_id: int) -> dict | None:
        """One duplicate group (with members) by id, or None."""
        rows = self.fetchall("SELECT * FROM dup_groups WHERE group_id = ?", (int(group_id),))
        return self._groups_with_members(rows)[0] if rows else None

    def dup_group_of(self, item_id: int) -> dict | None:
        """The duplicate group containing ``item_id`` (with members), or None."""
        rows = self.fetchall("SELECT g.* FROM dup_groups g JOIN dup_members m"
                             " ON m.group_id = g.group_id WHERE m.item_id = ?", (int(item_id),))
        return self._groups_with_members(rows[:1])[0] if rows else None

    def n_dup_groups(self, kind: str | None = None) -> int:
        """Number of duplicate groups (optionally of one kind), for paging."""
        if kind is not None and kind not in DUP_KINDS:
            raise ValueError("kind must be 'exact' or 'near'")
        where, params = ("WHERE kind = ?", (kind,)) if kind else ("", ())
        return int(self.fetchall(f"SELECT COUNT(*) FROM dup_groups {where}", params)[0][0])

    def _bursts_with_members(self, rows: list[sqlite3.Row]) -> list[dict]:
        bursts = [dict(r) for r in rows]
        by_id = {b["burst_id"]: b for b in bursts}
        for b in bursts:
            b["members"] = []
        ids = list(by_id)
        for part in _chunks(ids):
            marks = ",".join("?" * len(part))
            for r in self.fetchall(
                    f"SELECT m.burst_id AS _bid, {self._item_cols}, m.rank AS rank,"
                    " m.quality AS quality FROM burst_members m JOIN items i"
                    f" ON i.item_id = m.item_id WHERE m.burst_id IN ({marks})"
                    " ORDER BY m.burst_id, m.rank, i.item_id", part):
                b = by_id[r["_bid"]]
                member = self._item_dict(r, ("_bid",))
                member["is_best"] = int(member["item_id"] == b["best_item_id"])
                b["members"].append(member)
        return bursts

    def bursts(self, *, offset: int = 0, limit: int = 50) -> list[dict]:
        """Bursts ordered by burst_id, each with ``members`` (item dicts + rank, quality,
        is_best) in rank order."""
        _check_page(offset, limit)
        rows = self.fetchall("SELECT * FROM bursts ORDER BY burst_id LIMIT ? OFFSET ?",
                             (limit, offset))
        return self._bursts_with_members(rows)

    def burst(self, burst_id: int) -> dict | None:
        """One burst (with members) by id, or None."""
        rows = self.fetchall("SELECT * FROM bursts WHERE burst_id = ?", (int(burst_id),))
        return self._bursts_with_members(rows)[0] if rows else None

    def burst_of(self, item_id: int) -> dict | None:
        """The burst containing ``item_id`` (with members), or None."""
        rows = self.fetchall("SELECT b.* FROM bursts b JOIN burst_members m"
                             " ON m.burst_id = b.burst_id WHERE m.item_id = ?", (int(item_id),))
        return self._bursts_with_members(rows[:1])[0] if rows else None

    def n_bursts(self) -> int:
        """Number of bursts, for paging."""
        return int(self.fetchall("SELECT COUNT(*) FROM bursts")[0][0])

    def memberships(self, ids: list[int]) -> dict[int, dict]:
        """{item_id: {"dup_group", "is_keeper", "burst", "is_best"}} for items in a group
        or burst (others absent). Cheap enough to call for every result page."""
        out: dict[int, dict] = {}
        for part in _chunks(list(dict.fromkeys(int(i) for i in ids))):
            marks = ",".join("?" * len(part))
            for r in self.fetchall("SELECT item_id, group_id, is_keeper FROM dup_members"
                                   f" WHERE item_id IN ({marks})", part):
                out.setdefault(r[0], {}).update(dup_group=r[1], is_keeper=r[2])
            for r in self.fetchall(
                    "SELECT m.item_id, m.burst_id, b.best_item_id FROM burst_members m"
                    f" JOIN bursts b ON b.burst_id = m.burst_id WHERE m.item_id IN ({marks})",
                    part):
                out.setdefault(r[0], {}).update(burst=r[1], is_best=int(r[0] == r[2]))
        return out

    # ------------------------------------------------------------------ summaries

    def videos_by_day(self) -> dict[str, int]:
        """{local_date: number of videos} (videos are never downloaded, only counted)."""
        return {r[0]: int(r[1]) for r in self.fetchall(
            "SELECT local_date, n FROM videos_by_day ORDER BY local_date")}

    def stats(self) -> dict:
        """Stored merge statistics (JSON-decoded) merged with live counts from the index.

        Live keys: items, items_with_embeddings, items_undated, years {year: n},
        dup_groups {exact, near, deletable, total}, dup_members, bursts, videos,
        junk {category: n with score >= JUNK_DEFAULT_MIN}, partial, missing_shards.
        Live values win over stored ones of the same name.
        """
        out: dict = {}
        for r in self.fetchall("SELECT key, value FROM stats"):
            try:
                out[r[0]] = json.loads(r[1])
            except (TypeError, ValueError):
                out[r[0]] = r[1]
        row = self.fetchall("SELECT COUNT(*), COUNT(emb_row), SUM(local_date IS NULL)"
                            " FROM items")[0]
        out["items"] = int(row[0])
        out["items_with_embeddings"] = int(row[1])
        out["items_undated"] = int(row[2] or 0)
        out["years"] = {int(r[0]): int(r[1]) for r in self.fetchall(
            "SELECT year, COUNT(*) FROM items WHERE year IS NOT NULL GROUP BY year ORDER BY year")}
        groups = {k: 0 for k in DUP_KINDS}
        for r in self.fetchall("SELECT kind, COUNT(*) FROM dup_groups GROUP BY kind"):
            groups[r[0]] = int(r[1])
        groups["deletable"] = int(self.fetchall(
            "SELECT COUNT(*) FROM dup_groups WHERE deletable = 1")[0][0])
        groups["total"] = sum(groups[k] for k in DUP_KINDS)
        out["dup_groups"] = groups
        out["dup_members"] = int(self.fetchall("SELECT COUNT(*) FROM dup_members")[0][0])
        out["bursts"] = self.n_bursts()
        out["videos"] = int(self.fetchall("SELECT COALESCE(SUM(n), 0) FROM videos_by_day")[0][0])
        junk = {c: 0 for c in JUNK_CATEGORIES}
        for r in self.fetchall("SELECT category, COUNT(*) FROM scores WHERE score >= ?"
                               " GROUP BY category", (JUNK_DEFAULT_MIN,)):
            junk[r[0]] = int(r[1])
        out["junk"] = junk
        out["partial"] = self.meta.get("partial") in ("1", "true", "True")
        try:
            out["missing_shards"] = int(self.meta.get("missing_shards") or 0)
        except ValueError:
            out["missing_shards"] = 0
        return out
