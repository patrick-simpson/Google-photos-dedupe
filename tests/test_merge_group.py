"""Tests for gpclean.merge.group: edge checks, pigeonhole vs brute force, anti-chaining."""

from __future__ import annotations

import hashlib
import logging
import time

import numpy as np
import pytest
from shard_factory import image_cols, textured

from gpclean.config import MergeConfig
from gpclean.imaging.fingerprint import sig_distance
from gpclean.merge.group import (
    DupItem,
    aspect_ok,
    brute_candidates,
    brute_force_groups,
    capture_ok,
    chunk_bounds,
    dup_item,
    find_groups,
    group_key,
    is_graphic,
    keeper_key,
    near_candidates,
    sig_metrics_batch,
)
from gpclean.merge import group as group_mod
from gpclean.merge.group import _Checker

CFG = MergeConfig()
SIG_N = 1152


def _sig(rng: np.random.Generator, base: np.ndarray | None = None, noise: int = 0) -> bytes:
    if base is None:
        base = rng.integers(40, 216, SIG_N)
    arr = base + (rng.integers(-noise, noise + 1, SIG_N) if noise else 0)
    return np.clip(arr, 0, 255).astype(np.uint8).tobytes()


def item(uid: str, phash: int, sig: bytes, *, sha: bytes | None = None, w: int = 400,
         h: int = 300, graphic: bool = False, capture: int | None = None,
         subsec: float | None = None, url: str | None = None, rank: int = 0) -> DupItem:
    return DupItem(uid=uid, sha256=sha if sha is not None else hashlib.sha256(uid.encode()).digest(),
                   phash64=phash, sig=sig, width=w, height=h, is_graphic=graphic,
                   capture_s=capture, subsec=subsec, url=url, keeper_key=(rank, uid))


def _flip(h: int, bits) -> int:
    v = h & (2**64 - 1)
    for b in bits:
        v ^= 1 << int(b)
    return v - 2**64 if v >= 2**63 else v


def _canon(groups):
    return sorted((tuple(g.members), g.kind, g.deletable, g.key) for g in groups)


# ---------------------------------------------------------------------------------------------
# Edge checks
# ---------------------------------------------------------------------------------------------


def test_capture_time_rule():
    rng = np.random.default_rng(0)
    s = _sig(rng)

    def pair(ca, cb, sa=None, sb=None):
        return capture_ok(item("a", 0, s, capture=ca, subsec=sa),
                          item("b", 0, s, capture=cb, subsec=sb), CFG)

    assert not pair(1000, 1001)  # burst neighbour, 1 s apart
    assert not pair(1000, 1060)  # 60 s is inside the window
    assert pair(1000, 1061)  # outside the window
    assert pair(1000, 1000)  # identical capture time (a re-save)
    assert pair(1000, 1000 + 3600)  # exactly one hour: a time-zone fix of the same shot
    assert pair(1000, None) and pair(None, None)
    assert not pair(1000, 1000, 0.1, 0.4)  # same second, SubSec on both sides differs
    assert pair(1000, 1000, 0.347, 0.347)
    assert pair(1000, 1000, 0.1, None)  # SubSec on one side only: whole seconds compared
    wide = MergeConfig(capture_window_s=7200)
    assert capture_ok(item("a", 0, s, capture=0), item("b", 0, s, capture=3600), wide)
    assert not capture_ok(item("a", 0, s, capture=0), item("b", 0, s, capture=3000), wide)


def test_aspect_ok():
    s = b"\0" * SIG_N
    assert aspect_ok(item("a", 0, s, w=4000, h=3000), item("b", 0, s, w=1600, h=1200), 0.02)
    assert aspect_ok(item("a", 0, s, w=3000, h=2000), item("b", 0, s, w=2000, h=1333), 0.02)
    assert not aspect_ok(item("a", 0, s, w=2400, h=1600), item("b", 0, s, w=2160, h=1600), 0.02)
    assert not aspect_ok(item("a", 0, s, w=400, h=300), item("b", 0, s, w=300, h=400), 0.02)
    assert not aspect_ok(item("a", 0, s, w=0, h=300), item("b", 0, s), 0.02)


def test_is_graphic():
    rng = np.random.default_rng(1)
    textured_sig = _sig(rng)
    flat_sig = bytes([128] * SIG_N)
    assert not is_graphic(0.0, textured_sig, 0.1)
    assert is_graphic(0.8, textured_sig, 0.1)  # screenshot score
    assert is_graphic(0.0, flat_sig, 0.1)  # near-blank signature
    assert is_graphic(0.0, textured_sig, 0.35)  # one flat tone dominates
    assert is_graphic(0.0, None, None)  # no signature: never near-grouped


def test_keeper_order():
    base = {"width": 4000, "height": 3000, "has_camera_exif": 1, "lat": 1.0, "lon": 2.0,
            "favorited": 0, "albums_n": 0, "file_size": 100, "taken_ts": 10, "item_uid": "m"}
    variants = [
        ({"width": 2000}, "resolution"),
        ({"has_camera_exif": 0}, "camera EXIF"),
        ({"lat": None}, "GPS"),
        ({"file_size": 99}, "file size"),
        ({"taken_ts": 11}, "taken later"),
        ({"taken_ts": None}, "taken unknown"),
        ({"item_uid": "z"}, "uid"),
    ]
    for change, why in variants:
        assert keeper_key(base) < keeper_key({**base, **change}), why
    assert keeper_key({**base, "favorited": 1}) < keeper_key(base)
    assert keeper_key({**base, "albums_n": 2}) < keeper_key(base)
    # Higher levels beat lower ones: bigger file never beats camera EXIF.
    assert keeper_key(base) < keeper_key({**base, "has_camera_exif": 0, "file_size": 10**9})


# ---------------------------------------------------------------------------------------------
# Candidate search
# ---------------------------------------------------------------------------------------------


def _hashes(rng: np.random.Generator, n_random: int, n_planted: int) -> np.ndarray:
    ph = list(rng.integers(-2**63, 2**63 - 1, n_random, dtype=np.int64))
    for _ in range(n_planted):
        src = int(ph[int(rng.integers(0, len(ph)))])
        ph.append(_flip(src, rng.choice(64, int(rng.integers(0, 7)), replace=False)))
    return np.array(ph, dtype=np.int64)


@pytest.mark.parametrize("threshold", [2, 3, 4, 5])
def test_pigeonhole_finds_exactly_the_brute_force_pairs(threshold):
    rng = np.random.default_rng(threshold)
    ph = _hashes(rng, 1500, 700)
    eligible = rng.random(ph.size) > 0.1
    pairs, stats = near_candidates(ph, eligible, threshold, bucket_cap=10**6)
    want = brute_candidates(ph, eligible, threshold)
    assert pairs.shape[0] > 50
    assert np.array_equal(pairs, want)
    assert stats["skipped_buckets"] == 0


def test_chunks_cover_bits_1_to_63_and_never_bit_0():
    for t in (2, 3, 4, 5):
        bounds = chunk_bounds(t)
        assert len(bounds) == t + 1
        assert sum(w for _s, w in bounds) == 63
        # Contiguous, starting at bit 1: bit 0 (the DC term, always set) is in no chunk.
        assert [s for s, _w in bounds] == [1 + sum(w for _s, w in bounds[:k])
                                           for k in range(t + 1)]
        assert all(s >= 1 for s, _w in bounds)
        assert max(w for _s, w in bounds) - min(w for _s, w in bounds) <= 1
    with pytest.raises(ValueError):
        chunk_bounds(63)


@pytest.mark.parametrize("threshold", [2, 3, 4, 5])
def test_pigeonhole_with_constant_dc_bit_and_bit_0_flips(threshold):
    """Real pHashes have bit 0 set; pairs whose only differences include bit 0 are still found
    (the final check counts all 64 bits), and the sets equal the brute-force reference."""
    rng = np.random.default_rng(100 + threshold)
    ph = _hashes(rng, 1200, 0)
    ph = ph | np.int64(1)  # DC bit set, as for every non-black image
    extra = [_flip(int(ph[k]), [0, *rng.choice(np.arange(1, 64), threshold - 1, replace=False)])
             for k in rng.integers(0, ph.size, 300)]
    extra += [_flip(int(ph[k]), [0]) for k in rng.integers(0, ph.size, 50)]
    ph = np.concatenate([ph, np.array(extra, dtype=np.int64)])
    eligible = np.ones(ph.size, bool)
    pairs, stats = near_candidates(ph, eligible, threshold, bucket_cap=10**6)
    want = brute_candidates(ph, eligible, threshold)
    assert pairs.shape[0] >= 350
    assert np.array_equal(pairs, want)
    assert stats["skipped_buckets"] == 0 and stats["over_candidate_warn"] == 0


def test_candidate_warning_is_count_only(monkeypatch, caplog):
    monkeypatch.setattr(group_mod, "CANDIDATE_WARN_PAIRS", 10)
    ph = np.zeros(12, dtype=np.int64)
    with caplog.at_level(logging.WARNING, logger="gpclean.merge.group"):
        pairs, stats = near_candidates(ph, np.ones(12, bool), 3, bucket_cap=100)
    assert pairs.shape[0] == 66 and stats["over_candidate_warn"] == 1
    assert any("66 candidate pairs" in r.getMessage() for r in caplog.records)


def test_big_buckets_are_skipped_and_counted():
    # 12 identical hashes: every one of the 4 chunks (T=3) puts all 12 in one bucket.
    ph = np.zeros(12, dtype=np.int64)
    pairs, stats = near_candidates(ph, np.ones(12, bool), 3, bucket_cap=10)
    assert pairs.shape[0] == 0
    assert stats["skipped_buckets"] == 4 and stats["skipped_bucket_items"] == 48
    assert stats["over_candidate_warn"] == 0
    assert stats["unexamined_pairs"] == 4 * 66
    pairs, stats = near_candidates(ph, np.ones(12, bool), 3, bucket_cap=12)
    assert pairs.shape[0] == 66 and stats["skipped_buckets"] == 0


def test_near_search_scales_to_100k():
    rng = np.random.default_rng(7)
    ph = _hashes(rng, 90_000, 10_000)
    t0 = time.perf_counter()
    for t in (3, 5):
        pairs, _stats = near_candidates(ph, np.ones(ph.size, bool), t, 500)
        assert pairs.shape[0] >= 5_000
    assert time.perf_counter() - t0 < 30  # typically well under a second


# ---------------------------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------------------------


def _random_library(seed: int, n_clusters: int = 60, n_single: int = 300) -> list[DupItem]:
    """Clusters of near copies (1-6 bit flips, near-identical signatures, some burst-like
    capture times, some exact copies, some graphic) plus unrelated singles."""
    rng = np.random.default_rng(seed)
    items: list[DupItem] = []
    for c in range(n_clusters):
        base_hash = int(rng.integers(-2**63, 2**63 - 1))
        base_sig = rng.integers(40, 216, SIG_N)
        cap = int(rng.integers(10**9, 2 * 10**9)) if c % 3 == 0 else None
        prev = base_hash
        for m in range(int(rng.integers(2, 7))):
            # Walk away from the previous copy so chains longer than T appear.
            h = _flip(prev, rng.choice(64, int(rng.integers(0, 4)), replace=False))
            prev = h
            uid = f"c{c}m{m}"
            sha = hashlib.sha256(f"c{c}".encode()).digest() if m % 4 == 3 else None
            items.append(item(
                uid, h, _sig(rng, base_sig, noise=int(rng.integers(0, 4))), sha=sha,
                graphic=bool(rng.random() < 0.1), rank=int(rng.integers(0, 5)),
                capture=(cap + int(rng.integers(0, 3))) if cap is not None else None,
                url=f"u{c}{m}" if rng.random() < 0.8 else None))
    for k in range(n_single):
        items.append(item(f"s{k}", int(rng.integers(-2**63, 2**63 - 1)), _sig(rng),
                          rank=int(rng.integers(0, 5))))
    order = rng.permutation(len(items))
    return [items[i] for i in order]


@pytest.mark.parametrize("threshold", [2, 3, 4, 5])
def test_find_groups_equals_brute_force_oracle(threshold):
    for seed in range(3):
        items = _random_library(seed * 10 + threshold)
        cfg = MergeConfig(threshold=threshold)
        got, stats = find_groups(items, cfg)
        want, _ = brute_force_groups(items, cfg)
        assert _canon(got) == _canon(want)
        assert len(got) > 10
        assert stats["skipped_buckets"] == 0
        for g in got:
            # Every member is byte-identical to another member, or was verified as a near
            # copy against the seed or one of the seed's byte-identical copies.
            seed_class = [t for t in g.members if items[t].sha256 == items[g.seed].sha256]
            for k in g.members[1:]:
                other = items[k]
                if any(items[j].sha256 == other.sha256 for j in g.members if j != k):
                    continue
                assert not other.is_graphic
                assert any(not items[t].is_graphic for t in seed_class)


def test_sig_metrics_batch_equals_sig_distance_exactly():
    rng = np.random.default_rng(21)
    base = rng.integers(0, 256, SIG_N)
    sigs = [_sig(rng) for _ in range(40)]
    sigs += [_sig(rng, base, noise=int(rng.integers(0, 30))) for _ in range(40)]
    sigs += [bytes(SIG_N), bytes([255] * SIG_N)]  # extremes: every difference is 255
    mat = np.frombuffer(b"".join(sigs), dtype=np.uint8).reshape(len(sigs), SIG_N)
    a = rng.integers(0, len(sigs), 3000)
    b = rng.integers(0, len(sigs), 3000)
    a[:2], b[:2] = [len(sigs) - 2, 0], [len(sigs) - 1, 0]
    got = sig_metrics_batch(mat, a, b)
    want = np.array([sig_distance(sigs[i], sigs[j]) for i, j in zip(a.tolist(), b.tolist())])
    assert np.array_equal(got, want)  # identical, not merely close
    assert tuple(got[0]) == (255.0, 255.0, 255.0) and tuple(got[1]) == (0.0, 0.0, 0.0)


def _scalar_near(a: DupItem, b: DupItem, cfg: MergeConfig) -> str | None:
    """The reference: the scalar edge checks in order. Returns None for a near edge, else
    "skip" (not eligible / too far in pHash, not counted) or the first failing check."""
    if a.sha256 is not None and a.sha256 == b.sha256:
        return "skip"
    if a.is_graphic or b.is_graphic or a.phash64 is None or b.phash64 is None:
        return "skip"
    if group_mod.hamming64(a.phash64, b.phash64) > cfg.threshold:
        return "skip"
    if not aspect_ok(a, b, cfg.aspect_tol):
        return "aspect"
    if a.sig is None or b.sig is None:
        return "sig"
    mad, block, chroma = sig_distance(a.sig, b.sig)
    if not (mad <= cfg.sig_mad_max and block <= cfg.sig_block_max
            and chroma <= cfg.sig_chroma_max):
        return "sig"
    return None if capture_ok(a, b, cfg) else "capture"


def test_batched_verification_equals_the_scalar_checks():
    """_Checker (numpy) accepts exactly the pairs the scalar checks accept, with the same
    rejection counts by first failing check, over a library that exercises every check."""
    rng = np.random.default_rng(22)
    base = rng.integers(40, 216, SIG_N)
    t0 = 1_500_000_000
    offsets = [0, 1, 2, 60, 61, 3600, 3601, 7200, 10**6]
    items = []
    for k in range(200):
        kind = k % 8
        items.append(item(
            f"i{k}", _flip(0, rng.choice(64, int(rng.integers(0, 6)), replace=False)),
            _sig(rng, base, noise=int(rng.integers(0, 9))) if kind != 1 else _sig(rng),
            w=[400, 300, 402, 0][(k // 8) % 4] if kind == 2 else 400, h=300,
            graphic=kind == 3,
            capture=t0 + int(rng.choice(offsets)) if kind in (4, 5, 7) else None,
            subsec=[0.25, 0.5, None][k % 3] if kind in (5, 7) else None,
            sha=hashlib.sha256(b"same").digest() if kind == 6 and k % 16 == 6 else None))
    items.append(DupItem(uid="nosig", sha256=None, phash64=0, sig=None, width=400, height=300,
                         is_graphic=False, capture_s=None, subsec=None, url=None,
                         keeper_key=(0, "nosig")))
    n = len(items)
    pairs = np.array([(i, j) for i in range(n) for j in range(i + 1, n)], dtype=np.int64)
    cfg = MergeConfig(threshold=4)
    chk = _Checker(items, cfg, 4, np.arange(n))
    ei, ej = chk.verify_pairs(pairs)
    verdicts = [_scalar_near(items[i], items[j], cfg) for i, j in pairs.tolist()]
    want = [p for p, v in zip(pairs.tolist(), verdicts) if v is None]
    assert list(zip(ei.tolist(), ej.tolist())) == [tuple(p) for p in want]
    assert len(want) > 100
    counts = {r: verdicts.count(r) for r in ("aspect", "sig", "capture")}
    assert all(counts.values()), counts
    assert dict(chk.rejected) == counts
    # The seed metrics equal sig_distance; None where a side has no signature.
    got = chk.metrics(0, [1, 2, n - 1])
    assert got[:2] == [sig_distance(items[0].sig, items[k].sig) for k in (1, 2)]
    assert got[2] is None


def test_dense_phash_clusters_verify_quickly():
    """Two clusters of 400 near-identical hashes (over 120k candidate pairs, all needing a
    signature check) take seconds, not minutes."""
    rng = np.random.default_rng(23)
    items = []
    for c in range(2):
        h0 = int(rng.integers(-2**63, 2**63 - 1))
        base = rng.integers(40, 216, SIG_N)
        for m in range(400):
            items.append(item(f"c{c}m{m}", _flip(h0, rng.choice(64, int(rng.integers(0, 3)),
                                                                   replace=False)),
                              _sig(rng, base if m % 2 else None, noise=1), rank=m))
    t0 = time.perf_counter()
    groups, stats = find_groups(items, CFG)
    elapsed = time.perf_counter() - t0
    assert stats["candidate_pairs"] >= 120_000 and stats["rejected_sig"] > 0
    assert len(groups) >= 2
    assert elapsed < 30  # about a second; it was about 70 us per pair in scalar Python


def test_anti_chaining_splits_a_chain():
    rng = np.random.default_rng(3)
    s = _sig(rng)
    a = item("a", 0, s, rank=0)
    b = item("b", _flip(0, [0, 1, 2]), s, rank=1)
    c = item("c", _flip(0, [0, 1, 2, 3, 4, 5]), s, rank=2)
    groups, _ = find_groups([a, b, c], CFG)
    assert [g.members for g in groups] == [[0, 1]]  # A seeds {A, B}; C (6 bits from A) alone
    # When the middle item is the best keeper, it verifies both ends itself.
    b_best = item("b", _flip(0, [0, 1, 2]), s, rank=-1)
    groups, _ = find_groups([a, b_best, c], CFG)
    assert [g.members for g in groups] == [[1, 0, 2]]


def test_graphic_items_group_only_by_exact_sha():
    rng = np.random.default_rng(4)
    s = _sig(rng)
    same = hashlib.sha256(b"shot").digest()
    items = [item("g1", 5, s, graphic=True, sha=same), item("g2", 5, s, graphic=True, sha=same),
             item("g3", 5, s, graphic=True), item("p1", 5, s)]
    groups, _ = find_groups(items, CFG)
    assert len(groups) == 1 and sorted(groups[0].members) == [0, 1]
    assert groups[0].kind == "exact"


def test_byte_identical_twin_of_a_member_joins_even_if_graphic():
    rng = np.random.default_rng(5)
    s = _sig(rng)
    twin_sha = hashlib.sha256(b"twin").digest()
    seed = item("a", 0, s, rank=0)
    member = item("b", _flip(0, [1]), s, sha=twin_sha, rank=1)
    twin = item("c", _flip(0, [1]), s, sha=twin_sha, rank=2, graphic=True)
    groups, _ = find_groups([seed, member, twin], CFG)
    assert [sorted(g.members) for g in groups] == [[0, 1, 2]]
    assert groups[0].kind == "near"


def test_graphic_seed_verifies_near_copies_through_its_byte_identical_twin():
    """A "Screenshot_" copy S of photo P ranks first as keeper; S is graphic, so near()
    refuses it, but P (same bytes) verifies Q, a true near-duplicate of the picture."""
    rng = np.random.default_rng(5)
    s = _sig(rng)
    sha = hashlib.sha256(b"x").digest()
    shot = item("s", 0, s, sha=sha, graphic=True, rank=0)
    photo = item("p", 0, s, sha=sha, rank=1)
    near = item("q", _flip(0, [3]), s, rank=2)
    groups, _ = find_groups([shot, photo, near], CFG)
    assert [g.members for g in groups] == [[0, 1, 2]]
    assert groups[0].kind == "near"
    # Without the twin, a graphic seed still only takes exact copies.
    groups, _ = find_groups([shot, near], CFG)
    assert groups == []


def test_capture_rule_blocks_identical_looking_burst_frames():
    rng = np.random.default_rng(6)
    s = _sig(rng)
    a = item("a", 0, s, capture=5000)
    b = item("b", 0, s, capture=5001)
    groups, stats = find_groups([a, b], CFG)
    assert groups == [] and stats["rejected_capture"] == 1
    c = item("c", 0, s, capture=5000)
    groups, _ = find_groups([a, c], CFG)
    assert len(groups) == 1


def test_signature_check_blocks_different_pictures_with_same_phash():
    rng = np.random.default_rng(7)
    groups, stats = find_groups([item("a", 0, _sig(rng)), item("b", 0, _sig(rng))], CFG)
    assert groups == [] and stats["rejected_sig"] == 1


def test_deletable_needs_two_distinct_urls_and_key_is_stable():
    rng = np.random.default_rng(8)
    s = _sig(rng)
    sha = hashlib.sha256(b"same").digest()
    one_url = [item("a", 0, s, sha=sha, url="u1"), item("b", 0, s, sha=sha, url="u1"),
               item("c", 0, s, sha=sha)]
    g = find_groups(one_url, CFG)[0][0]
    assert not g.deletable and g.kind == "exact"
    two = [item("a", 0, s, sha=sha, url="u1"), item("b", 0, s, sha=sha, url="u2")]
    g = find_groups(two, CFG)[0][0]
    assert g.deletable
    assert g.key == group_key(["b", "a"]) == hashlib.sha1(b"a\nb").hexdigest()


def test_metrics_are_relative_to_the_seed():
    rng = np.random.default_rng(9)
    base = rng.integers(40, 216, SIG_N)
    a = item("a", 0, _sig(rng, base), rank=0)
    b = item("b", _flip(0, [3, 9]), _sig(rng, base, noise=2), rank=1)
    g = find_groups([a, b], CFG)[0][0]
    assert g.metrics[0] == (0, 0.0, 0.0, 0.0)
    dist, mad, block, chroma = g.metrics[1]
    assert dist == 2 and 0 < mad <= 2 and block <= 3 and chroma <= 2


def test_real_images_recompressed_group_and_different_ones_do_not():
    """End to end on pixels: the same picture at q92 and q60 (and half size) groups; another
    picture does not."""
    im = textured(11, 640, 480)
    rows = [
        {"item_uid": "a", **image_cols(im, quality=92)},
        {"item_uid": "b", **image_cols(im, quality=60)},
        {"item_uid": "c", **image_cols(im.resize((320, 240)), quality=85)},
        {"item_uid": "d", **image_cols(textured(12, 640, 480), quality=92)},
    ]
    items = [dup_item(r, is_graphic(0.0, r["sig"], r["flat_frac"])) for r in rows]
    assert not any(it.is_graphic for it in items)
    groups, _ = find_groups(items, CFG)
    assert [sorted(g.members) for g in groups] == [[0, 1, 2]]
    assert groups[0].seed == 0  # larger resolution, then larger file
