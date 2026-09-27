"""Duplicate grouping with zero false positives by construction (PLAN section 5).

1. **Exact:** items with equal sha256 are always duplicates (graphic or not).
2. **Near candidates** (non-graphic items only): pairs of pHashes within Hamming distance T,
   found with the pigeonhole principle. Split the 64 bits into T+1 contiguous chunks; two
   hashes that differ in at most T bits agree *exactly* on at least one chunk, so bucketing by
   (chunk index, chunk value) and comparing only within buckets finds every such pair.
   Buckets larger than ``bucket_cap`` are skipped (and counted) so one pathological cluster
   cannot turn the search quadratic.
3. **Edge checks**, all required for a near pair: Hamming <= T, aspect ratio within
   ``aspect_tol``, :func:`gpclean.imaging.fingerprint.sig_verify`, and the capture-time rule
   (two EXIF capture times 0 < |dt| <= 60 s apart, not a whole number of hours, are two
   *different* shots of a burst, however similar).
4. **Anti-chaining:** union-find over the edges gives components; inside each component the
   best item by keeper order is the seed, and only members that pass every edge check
   *against the seed* (or against a byte-identical copy of it; or are byte-identical to the
   seed or to an absorbed member) join its
   group. The rest are grouped again the same way. So A~B and B~C never put A and C together
   unless A~C.

:func:`brute_force_groups` runs the same procedure over all O(n^2) pairs; tests assert both
give identical groups.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from gpclean.config import MergeConfig
from gpclean.imaging.fingerprint import sig_distance, sig_luma_std
from gpclean.merge.localtime import parse_exif_dt, subsec_fraction, wall_seconds

log = logging.getLogger(__name__)

GRAPHIC_SHOT_SCORE = 0.8
GRAPHIC_SIG_STD = 8.0
GRAPHIC_FLAT_FRAC = 0.35



# ---------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------


def is_graphic(shot_score: float, sig: bytes | None, flat_frac: float | None) -> bool:
    """Screenshots, documents and near-blank frames: pHash is unreliable on them, so they are
    grouped by exact sha256 only (a missing signature counts as graphic, to be safe)."""
    if shot_score >= GRAPHIC_SHOT_SCORE:
        return True
    if sig is None or sig_luma_std(bytes(sig)) < GRAPHIC_SIG_STD:
        return True
    return flat_frac is not None and flat_frac >= GRAPHIC_FLAT_FRAC


def keeper_key(row: dict) -> tuple:
    """Sort key, best keeper first (PLAN deviation 5).

    Resolution, then camera EXIF, then GPS, then favorited, then in more albums, then larger
    file, then earlier taken (unknown last), then item_uid for a total order.
    """
    w, h = int(row.get("width") or 0), int(row.get("height") or 0)
    has_gps = row.get("lat") is not None and row.get("lon") is not None
    taken = row.get("taken_ts")
    return (-(w * h), -int(bool(row.get("has_camera_exif"))), -int(has_gps),
            -int(bool(row.get("favorited"))), -int(row.get("albums_n") or 0),
            -int(row.get("file_size") or 0), taken is None, taken if taken is not None else 0,
            row.get("item_uid") or "")


@dataclass(frozen=True)
class DupItem:
    """What grouping needs to know about one item."""

    uid: str
    sha256: bytes | None
    phash64: int | None
    sig: bytes | None
    width: int
    height: int
    is_graphic: bool
    capture_s: int | None  # EXIF DateTimeOriginal wall clock, seconds (zone cancels out)
    subsec: float | None  # SubSecTimeOriginal fraction when present
    url: str | None
    keeper_key: tuple


def dup_item(row: dict, graphic: bool) -> DupItem:
    """Build a :class:`DupItem` from an index ``items`` row."""
    dt = parse_exif_dt(row.get("exif_dt"))
    return DupItem(
        uid=row.get("item_uid") or "",
        sha256=bytes(row["sha256"]) if row.get("sha256") is not None else None,
        phash64=int(row["phash64"]) if row.get("phash64") is not None else None,
        sig=bytes(row["sig"]) if row.get("sig") is not None else None,
        width=int(row.get("width") or 0),
        height=int(row.get("height") or 0),
        is_graphic=bool(graphic),
        capture_s=wall_seconds(dt) if dt is not None else None,
        subsec=subsec_fraction(row.get("exif_subsec")) if dt is not None else None,
        url=row.get("url"),
        keeper_key=keeper_key(row),
    )


# ---------------------------------------------------------------------------------------------
# Edge checks
# ---------------------------------------------------------------------------------------------


def hamming64(a: int, b: int) -> int:
    """Differing bits between two signed/unsigned 64-bit hashes."""
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def aspect_ok(a: DupItem, b: DupItem, tol: float) -> bool:
    """Post-rotation aspect ratios within ``tol`` (relative)."""
    if min(a.width, a.height, b.width, b.height) <= 0:
        return False
    ra, rb = a.width / a.height, b.width / b.height
    return abs(ra - rb) / max(ra, rb) <= tol


def capture_ok(a: DupItem, b: DupItem, cfg: MergeConfig) -> bool:
    """The capture-time rule: False when both EXIF capture times are known and differ by
    0 < |dt| <= capture_window_s without being a whole number of hours (a burst neighbour,
    not a copy). SubSec is used only when both sides have it."""
    if a.capture_s is None or b.capture_s is None:
        return True
    if a.subsec is not None and b.subsec is not None:
        delta = abs((a.capture_s + a.subsec) - (b.capture_s + b.subsec))
    else:
        delta = float(abs(a.capture_s - b.capture_s))
    if delta < 1e-6 or delta > cfg.capture_window_s:
        return True
    hours = delta / 3600.0
    return abs(hours - round(hours)) < 1e-6 and round(hours) >= 1


class _Checker:
    """Edge checks with a cache (anti-chaining re-tests pairs the union-find already saw)."""

    def __init__(self, items: list[DupItem], cfg: MergeConfig, threshold: int):
        self.items = items
        self.cfg = cfg
        self.t = threshold
        self._sig: dict[tuple[int, int], tuple[float, float, float]] = {}
        self.rejected: dict[str, int] = defaultdict(int)

    def sig_metrics(self, i: int, j: int) -> tuple[float, float, float] | None:
        a, b = self.items[i], self.items[j]
        if a.sig is None or b.sig is None:
            return None
        key = (i, j) if i < j else (j, i)
        if key not in self._sig:
            self._sig[key] = sig_distance(a.sig, b.sig)
        return self._sig[key]

    def exact(self, i: int, j: int) -> bool:
        a, b = self.items[i], self.items[j]
        return a.sha256 is not None and a.sha256 == b.sha256

    def near(self, i: int, j: int, *, count: bool = False) -> bool:
        """All near-duplicate edge checks (not the exact-sha shortcut)."""
        a, b = self.items[i], self.items[j]
        if a.is_graphic or b.is_graphic or a.phash64 is None or b.phash64 is None:
            return False
        if hamming64(a.phash64, b.phash64) > self.t:
            return False
        reason = None
        if not aspect_ok(a, b, self.cfg.aspect_tol):
            reason = "aspect"
        else:
            m = self.sig_metrics(i, j)
            if m is None or not (m[0] <= self.cfg.sig_mad_max and m[1] <= self.cfg.sig_block_max
                                 and m[2] <= self.cfg.sig_chroma_max):
                reason = "sig"
            elif not capture_ok(a, b, self.cfg):
                reason = "capture"
        if reason is not None:
            if count:
                self.rejected[reason] += 1
            return False
        return True

    def edge(self, i: int, j: int) -> bool:
        return self.exact(i, j) or self.near(i, j)


# ---------------------------------------------------------------------------------------------
# Candidate search
# ---------------------------------------------------------------------------------------------


def chunk_bounds(threshold: int) -> list[tuple[int, int]]:
    """(start bit, width) of the T+1 contiguous chunks of a 64-bit hash."""
    parts = np.array_split(np.arange(64), threshold + 1)
    return [(int(p[0]), int(p.size)) for p in parts]


def _as_u64(phash: np.ndarray) -> np.ndarray:
    return np.asarray(phash, dtype=np.int64).view(np.uint64)


def _unique_pairs(lo_parts: list[np.ndarray], hi_parts: list[np.ndarray], n: int) -> np.ndarray:
    if not lo_parts:
        return np.zeros((0, 2), dtype=np.int64)
    a = np.concatenate(lo_parts).astype(np.int64)
    b = np.concatenate(hi_parts).astype(np.int64)
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    codes = np.unique(lo * n + hi)
    return np.stack([codes // n, codes % n], axis=1)


def near_candidates(phash: np.ndarray, eligible: np.ndarray, threshold: int,
                    bucket_cap: int) -> tuple[np.ndarray, dict]:
    """All pairs (i < j) of eligible items with Hamming(phash) <= threshold, via pigeonhole.

    Returns ``(pairs[k, 2] int64 sorted, stats)``; stats count skipped buckets, the items in
    them and the pairs left unexamined there. Vectorised: for each chunk the hashes are sorted
    by chunk value, and the d-th neighbour inside every bucket is compared for d = 1, 2, ...,
    so the work is proportional to the number of within-bucket pairs.
    """
    n = int(phash.shape[0])
    idx = np.flatnonzero(np.asarray(eligible, dtype=bool))
    hv = _as_u64(phash)[idx]
    stats = {"skipped_buckets": 0, "skipped_bucket_items": 0, "unexamined_pairs": 0}
    lo_parts: list[np.ndarray] = []
    hi_parts: list[np.ndarray] = []
    m = hv.size
    if m < 2:
        return np.zeros((0, 2), dtype=np.int64), stats
    positions = np.arange(m)
    for start, width in chunk_bounds(threshold):
        vals = (hv >> np.uint64(start)) & np.uint64((1 << width) - 1)
        order = np.argsort(vals, kind="stable")
        sv = vals[order]
        cuts = np.flatnonzero(sv[1:] != sv[:-1]) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [m]))
        sizes = ends - starts
        big = sizes > bucket_cap
        if big.any():
            stats["skipped_buckets"] += int(big.sum())
            stats["skipped_bucket_items"] += int(sizes[big].sum())
            stats["unexamined_pairs"] += int((sizes[big] * (sizes[big] - 1) // 2).sum())
        usable = np.repeat(~big & (sizes >= 2), sizes)
        run_end = np.repeat(ends, sizes)
        active = positions[usable & (run_end - positions > 1)]
        d = 1
        while active.size:
            a = order[active]
            b = order[active + d]
            close = np.bitwise_count(hv[a] ^ hv[b]) <= threshold
            if close.any():
                lo_parts.append(idx[a[close]])
                hi_parts.append(idx[b[close]])
            d += 1
            active = active[run_end[active] - active > d]
    return _unique_pairs(lo_parts, hi_parts, n), stats


def brute_candidates(phash: np.ndarray, eligible: np.ndarray, threshold: int) -> np.ndarray:
    """The O(n^2) reference for :func:`near_candidates` (tests and the oracle only)."""
    n = int(phash.shape[0])
    idx = np.flatnonzero(np.asarray(eligible, dtype=bool))
    hv = _as_u64(phash)[idx]
    lo_parts, hi_parts = [], []
    for k in range(idx.size - 1):
        close = np.flatnonzero(np.bitwise_count(hv[k] ^ hv[k + 1:]) <= threshold)
        if close.size:
            lo_parts.append(np.full(close.size, idx[k]))
            hi_parts.append(idx[k + 1 + close])
    return _unique_pairs(lo_parts, hi_parts, n)


# ---------------------------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------------------------


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class DupGroup:
    """One emitted duplicate group. ``members`` are item indices, seed (keeper) first."""

    members: list[int]
    kind: str  # exact | near
    deletable: bool
    key: str  # sha1 hex of the sorted member uids
    # item index -> (hamming to seed, sig mad, sig block max, sig chroma max); None if unknown
    metrics: dict[int, tuple[int | None, float | None, float | None, float | None]] = \
        field(default_factory=dict)

    @property
    def seed(self) -> int:
        return self.members[0]


def group_key(uids: list[str]) -> str:
    """sha1 hex of the sorted member uids (stable id for review decisions)."""
    return hashlib.sha1("\n".join(sorted(uids)).encode("utf-8")).hexdigest()


def _anti_chain(comp: list[int], items: list[DupItem], chk: _Checker) -> list[list[int]]:
    """Split one union-find component into seed-verified groups (see module docstring)."""
    remaining = sorted(comp, key=lambda k: items[k].keeper_key)
    groups: list[list[int]] = []
    while len(remaining) >= 2:
        seed = remaining[0]
        # The seed's byte-identical copies have the same pixels and EXIF, so a near check
        # against any of them is as strict as one against the seed. It matters when the seed
        # itself is graphic only by name (a "Screenshot_" copy of a photo ranked first):
        # near() refuses graphic items, and the photo's true near-duplicates would be lost.
        sha = items[seed].sha256
        seed_class = [k for k in remaining if sha is not None and items[k].sha256 == sha]
        seed_class = seed_class or [seed]
        group, rest = [seed], []
        for k in remaining[1:]:
            ok = chk.exact(seed, k) or any(chk.near(t, k) for t in seed_class if t != k)
            (group if ok else rest).append(k)
        # A byte-identical copy of an absorbed member is as close to the seed as that member;
        # it can only miss the seed check when it is graphic by name (e.g. a "Screenshot_"
        # copy of a photo), and it belongs with its twin.
        shas = {items[k].sha256 for k in group if items[k].sha256 is not None}
        twins = [k for k in rest if items[k].sha256 in shas]
        if twins:
            group = sorted(group + twins, key=lambda k: items[k].keeper_key)
            rest = [k for k in rest if items[k].sha256 not in shas]
        if len(group) >= 2:
            groups.append(group)
        remaining = rest
    return groups


def _emit(members: list[int], items: list[DupItem], chk: _Checker) -> DupGroup:
    seed = members[0]
    shas = {items[k].sha256 for k in members}
    urls = {items[k].url for k in members if items[k].url}
    metrics = {}
    for k in members:
        a, b = items[seed], items[k]
        dist = (hamming64(a.phash64, b.phash64)
                if a.phash64 is not None and b.phash64 is not None else None)
        sm = (0.0, 0.0, 0.0) if k == seed else chk.sig_metrics(seed, k)
        metrics[k] = (dist, *(sm if sm is not None else (None, None, None)))
    return DupGroup(
        members=list(members),
        kind="exact" if len(shas) == 1 and None not in shas else "near",
        deletable=len(urls) >= 2,
        key=group_key([items[k].uid for k in members]),
        metrics=metrics,
    )


def _group_from_pairs(items: list[DupItem], pairs: np.ndarray, cfg: MergeConfig,
                      threshold: int) -> tuple[list[DupGroup], dict]:
    n = len(items)
    chk = _Checker(items, cfg, threshold)
    uf = _UnionFind(n)
    by_sha: dict[bytes, list[int]] = defaultdict(list)
    for k, it in enumerate(items):
        if it.sha256 is not None:
            by_sha[it.sha256].append(k)
    exact_pairs = 0
    for ks in by_sha.values():
        for k in ks[1:]:
            uf.union(ks[0], k)
            exact_pairs += 1
    verified = 0
    for i, j in pairs.tolist():
        if chk.exact(i, j):
            continue  # already joined
        if chk.near(i, j, count=True):
            uf.union(i, j)
            verified += 1

    comps: dict[int, list[int]] = defaultdict(list)
    for k in range(n):
        comps[uf.find(k)].append(k)
    groups: list[DupGroup] = []
    for comp in comps.values():
        if len(comp) >= 2:
            groups.extend(_emit(g, items, chk) for g in _anti_chain(comp, items, chk))
    # Deterministic order: by seed index (item order), then key.
    groups.sort(key=lambda g: (g.seed, g.key))
    stats = {
        "candidate_pairs": int(pairs.shape[0]),
        "near_edges": verified,
        "exact_links": exact_pairs,
        "rejected_aspect": chk.rejected["aspect"],
        "rejected_sig": chk.rejected["sig"],
        "rejected_capture": chk.rejected["capture"],
        "components": sum(1 for c in comps.values() if len(c) >= 2),
    }
    return groups, stats


def _arrays(items: list[DupItem]) -> tuple[np.ndarray, np.ndarray]:
    phash = np.array([it.phash64 if it.phash64 is not None else 0 for it in items],
                     dtype=np.int64)
    eligible = np.array([not it.is_graphic and it.phash64 is not None and it.sig is not None
                         for it in items], dtype=bool)
    return phash, eligible


def find_groups(items: list[DupItem], cfg: MergeConfig,
                threshold: int | None = None) -> tuple[list[DupGroup], dict]:
    """Duplicate groups over ``items`` using the pigeonhole candidate search."""
    t = cfg.threshold if threshold is None else threshold
    phash, eligible = _arrays(items)
    pairs, cstats = near_candidates(phash, eligible, t, cfg.bucket_cap)
    groups, stats = _group_from_pairs(items, pairs, cfg, t)
    stats.update(cstats)
    stats["near_eligible"] = int(eligible.sum())
    stats["graphic_items"] = sum(1 for it in items if it.is_graphic)
    log.info("grouping: groups=%d candidates=%d skipped_buckets=%d", len(groups),
             stats["candidate_pairs"], stats["skipped_buckets"])
    return groups, stats


def brute_force_groups(items: list[DupItem], cfg: MergeConfig,
                       threshold: int | None = None) -> tuple[list[DupGroup], dict]:
    """The oracle: the same procedure over every pair (O(n^2); tests only)."""
    t = cfg.threshold if threshold is None else threshold
    phash, eligible = _arrays(items)
    return _group_from_pairs(items, brute_candidates(phash, eligible, t), cfg, t)
