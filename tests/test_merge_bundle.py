"""End-to-end merge tests: shards -> bundle -> gpclean.bundle_read.Bundle.

The fixture test runs the real fixture zips through a tiny scanner stand-in
(``shard_factory.shards_from_zips``) and demands an exact match with ``expected.json``:
every planted duplicate found with its keeper, nothing else grouped, collapses, pairings,
bursts, the exhaustive junk categories and the per-day counts of unindexed library items
(``unindexed_by_day``). A second copy of the check runs through the real ``gpclean.scan``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from shard_factory import (
    ShardWriter,
    fake_url,
    image_cols,
    shards_from_zips,
    textured,
)

from gpclean.bundle_read import Bundle
from gpclean.config import MergeConfig
from gpclean.fixtures.generate import expected_view, generate
from gpclean.merge.bundle import (
    ManifestError,
    analyse,
    build_bundle,
    cli_regroup,
    regroup,
)
from gpclean.merge.load import ShardMismatch

EXHAUSTIVE = ("screenshot", "messaging", "tiny", "dup_extra", "burst_extra")
PIXEL_CATS = ("blur", "dark", "overexposed", "pocket")
FLAG_MIN = 0.5

ZIP_A = "takeout-20260101T000000Z-001.zip"
ZIP_B = "takeout-20260201T000000Z-001.zip"


# ---------------------------------------------------------------------------------------------
# Comparison with the fixture's ground truth
# ---------------------------------------------------------------------------------------------


def _key_of(item: dict) -> str:
    uid = item["item_uid"]
    return uid[2:] if uid.startswith("g:") else "nourl:" + item["filename"]


def check_against_expected(bundle_dir: Path, exp: dict) -> None:
    """Assert the bundle matches ``exp`` (an expected_view) exactly; see module docstring."""
    b = Bundle(bundle_dir)
    try:
        zips = {r["zip_id"]: r["name"] for r in b.fetchall("SELECT zip_id, name FROM zips")}
        items = [dict(r) for r in b.fetchall("SELECT * FROM items")]
        by_id = {it["item_id"]: it for it in items}
        key_of_id = {i: _key_of(it) for i, it in by_id.items()}

        # Items and members (primary + aliases) <-> expected item keys.
        assert len(items) == exp["n_items"]
        got_refs = {f"{zips[it['zip_id']]}::{it['member']}": key_of_id[it["item_id"]]
                    for it in items}
        for r in b.fetchall("SELECT item_id, zip_id, member FROM item_aliases"):
            ref = f"{zips[r['zip_id']]}::{r['member']}"
            assert ref not in got_refs
            got_refs[ref] = key_of_id[r["item_id"]]
        assert got_refs == exp["item_keys"]

        # Collapses: every copy of the item, primary included.
        members_of: dict[str, set[str]] = {}
        for ref, key in got_refs.items():
            members_of.setdefault(key, set()).add(ref)
        for c in exp["collapsed"]:
            assert members_of[c["item_key"]] == set(c["copies"]), c["id"]

        # Pairings: an item's primary member has a sidecar iff expected (the url-derived key
        # already proves it is the *right* sidecar).
        for it in items:
            ref = f"{zips[it['zip_id']]}::{it['member']}"
            assert (it["match_rule"] != "none") == (exp["pairings"][ref] is not None), ref

        # Duplicate groups: exact equality (zero false positives, zero false negatives).
        groups = b.dup_groups(limit=10_000)
        got_groups = {frozenset(key_of_id[m["item_id"]] for m in g["members"]):
                      key_of_id[g["seed_item_id"]] for g in groups}
        want_groups = {frozenset(g["members"]): g["keeper"] for g in exp["dup_groups"]}
        assert got_groups == want_groups
        in_group = {k: gi for gi, ks in enumerate(got_groups) for k in ks}
        for a, c in exp["must_not_group"]:
            assert a not in in_group or in_group[a] != in_group.get(c), (a, c)

        # Bursts.
        got_bursts = {frozenset(key_of_id[m["item_id"]] for m in bu["members"]):
                      key_of_id[bu["best_item_id"]] for bu in b.bursts(limit=10_000)}
        want_bursts = {frozenset(bu["members"]): bu["best"] for bu in exp["bursts"]}
        assert got_bursts == want_bursts

        # Junk: exhaustive categories exactly; pixel categories must include what was planted.
        flagged: dict[str, set[str]] = {}
        for r in b.fetchall("SELECT item_id, category FROM scores WHERE score >= ?", (FLAG_MIN,)):
            flagged.setdefault(r["category"], set()).add(key_of_id[r["item_id"]])
        for cat in EXHAUSTIVE:
            want = {k for k, cats in exp["junk"].items() if cat in cats}
            assert flagged.get(cat, set()) == want, cat
        for cat in PIXEL_CATS:
            want = {k for k, cats in exp["junk"].items() if cat in cats}
            assert want <= flagged.get(cat, set()), cat

        # videos_by_day counts every unindexed year-folder library item (videos plus skipped
        # raw / other-format media with a sidecar), which is what unindexed_by_day records.
        assert b.videos_by_day() == exp["unindexed_by_day"]
        assert sum(exp["unindexed_by_day"].values()) > sum(exp["videos"]["by_day"].values())
        stats = b.stats()
        assert stats["skipped_buckets"] == 0
        assert stats["items"] == exp["n_items"]
    finally:
        b.close()


@pytest.fixture(scope="module")
def fixture_zips(tmp_path_factory):
    out = tmp_path_factory.mktemp("fx")
    expected = generate(out, seed=0, small=True)
    return out, expected


@pytest.mark.slow
@pytest.mark.parametrize("include_albums", [False, True])
def test_fixture_end_to_end_via_stand_in_scanner(fixture_zips, tmp_path, include_albums):
    zips, expected = fixture_zips
    metas, pack_dir = shards_from_zips(zips, tmp_path / "scan", include_albums=include_albums)
    out = tmp_path / "bundle"
    manifest = build_bundle(metas, pack_dir, out, MergeConfig(), cfg_hash="testcfg000",
                            clip_model="none")
    assert not manifest["partial"]
    check_against_expected(out, expected_view(expected, include_albums=include_albums))


@pytest.fixture(scope="module")
def full_fixture_scans(tmp_path_factory):
    """The full fixture (with N7's ~300 distinct photos) scanned with and without albums."""
    out = tmp_path_factory.mktemp("fxfull")
    zips = out / "zips"
    zips.mkdir()
    expected = generate(zips, seed=0, small=False)
    scans = {inc: shards_from_zips(zips, out / f"scan{int(inc)}", include_albums=inc)
             for inc in (False, True)}
    return zips, expected, scans


@pytest.mark.slow
@pytest.mark.parametrize("include_albums", [False, True])
@pytest.mark.parametrize("threshold", [2, 3, 4, 5])
def test_full_fixture_end_to_end_at_every_threshold(full_fixture_scans, tmp_path, threshold,
                                                    include_albums):
    """Zero false positives at scale: the distinct filler photos must stay ungrouped at every
    supported threshold, with the planted duplicates still found."""
    zips, expected, scans = full_fixture_scans
    metas, pack_dir = scans[include_albums]
    out = tmp_path / "bundle"
    build_bundle(metas, pack_dir, out, MergeConfig(threshold=threshold), cfg_hash="testcfg000",
                 clip_model="none")
    check_against_expected(out, expected_view(expected, include_albums=include_albums))


@pytest.mark.slow
def test_fixture_end_to_end_via_scan(fixture_zips, tmp_path):
    """The real pipeline: gpclean.scan.cli_run_local (skips until gpclean.scan exists)."""
    scan = pytest.importorskip("gpclean.scan")
    zips, expected = fixture_zips
    out = tmp_path / "bundle"
    rc = scan.cli_run_local(zips=zips, out=out, include_albums=False, threshold=3,
                            clip_model="none", no_clip=True, workers=1, photos_per_shard=1000)
    assert rc == 0
    check_against_expected(out, expected_view(expected, include_albums=False))


# ---------------------------------------------------------------------------------------------
# Synthetic shards
# ---------------------------------------------------------------------------------------------


def _emb(seed: int, dim: int = 512) -> bytes:
    v = np.random.default_rng(seed).normal(size=dim).astype(np.float32)
    return (v / np.linalg.norm(v)).astype("<f2").tobytes()


def _write_pack(path: Path, member_idxs: list[int]) -> None:
    from gpclean.schema import PACK_DDL, create_transport_db, finalize_transport_db
    conn = create_transport_db(path, PACK_DDL)
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)",
                     [(m, b"g%d" % m, b"p%d" % m) for m in member_idxs])
    finalize_transport_db(conn)


@pytest.fixture()
def synthetic(tmp_path):
    """Two exports: a near-duplicate pair, a photo re-exported (and in an album), a
    screenshot, a video. Returns (meta paths, pack dir, facts)."""
    urls = {k: fake_url() for k in ("a", "a2", "b", "shot", "vid")}
    im_a, im_b = textured(1, 640, 480), textured(2, 640, 480)
    cols_a = image_cols(im_a, quality=92, exif_dt="2021:07:16 10:00:00", model="FP-7",
                        has_camera_exif=1, make="Fixturephone", width=4000, height=3000,
                        emb=_emb(1))
    cols_a2 = image_cols(im_a, quality=60, width=4000, height=3000, emb=_emb(2))
    cols_b = image_cols(im_b, quality=90, exif_dt="2021:07:17 12:00:00", emb=_emb(3))
    shot = _screenshot_image()
    cols_shot = image_cols(shot, fmt="PNG", width=1080, height=2400)

    wa = ShardWriter(tmp_path / "a.meta.sqlite", zip_name=ZIP_A, zipkey="zipkeyaaaaaa")
    ia = wa.image("IMG_A.jpg", cols_a, folder="Photos from 2021")
    wa.sidecar("IMG_A.jpg.supplemental-metadata.json", folder="Photos from 2021",
               title="IMG_A.jpg", taken_ts=1626444000, url=urls["a"])
    ia2 = wa.image("IMG_A_copy.jpg", cols_a2, folder="Photos from 2021")
    wa.sidecar("IMG_A_copy.jpg.supplemental-metadata.json", folder="Photos from 2021",
               title="IMG_A_copy.jpg", taken_ts=1626444000 + 86400, url=urls["a2"])
    wa.image("IMG_B.jpg", cols_b, folder="Photos from 2021")
    wa.sidecar("IMG_B.jpg.supplemental-metadata.json", folder="Photos from 2021",
               title="IMG_B.jpg", taken_ts=1626537600, url=urls["b"])
    wa.sidecar("IMG_B.jpg.supplemental-metadata.json", folder="Trip", title="IMG_B.jpg",
               taken_ts=1626537600, url=urls["b"])
    wa.image("Screenshot_20210718-101010.png", cols_shot, folder="Photos from 2021")
    wa.sidecar("Screenshot_20210718-101010.png.supplemental-metadata.json",
               folder="Photos from 2021", title="Screenshot_20210718-101010.png",
               taken_ts=1626617410, url=urls["shot"], origin_folder="Screenshots")
    wa.video("VID_1.mp4", folder="Photos from 2021", file_size=12345)
    # 02:30 UTC on Jul 4 = Jul 3 in New York.
    wa.sidecar("VID_1.mp4.supplemental-metadata.json", folder="Photos from 2021",
               title="VID_1.mp4", taken_ts=1625365800, url=urls["vid"])
    wa.skipped("Takeout/Google Photos/Photos from 2021/IMG_A-edited.jpg", "edited")
    meta_a = wa.close()

    wb = ShardWriter(tmp_path / "b.meta.sqlite", zip_name=ZIP_B, zipkey="zipkeybbbbbb")
    wb.image("IMG_B.jpg", {**cols_b, "emb": None}, folder="Photos from 2021")
    wb.sidecar("IMG_B.jpg.supplemental-metadata.json", folder="Photos from 2021",
               title="IMG_B.jpg", taken_ts=1626537600, url=urls["b"])
    wb.video("VID_1.mp4", folder="Photos from 2021", file_size=12345)
    wb.sidecar("VID_1.mp4.supplemental-metadata.json", folder="Photos from 2021",
               title="VID_1.mp4", taken_ts=1625365800, url=urls["vid"])
    meta_b = wb.close()

    packs = tmp_path / "packs"
    packs.mkdir()
    _write_pack(packs / wa.pack_name, [ia, ia2])
    facts = {"urls": urls, "pack_a": wa.pack_name, "pack_b": wb.pack_name, "ia": ia,
             "emb_a": cols_a["emb"]}
    return [meta_a, meta_b], packs, facts


def _screenshot_image():
    """A flat chat-like screenshot: graphic by name and by flatness."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (270, 600), (236, 229, 221))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 270, 40], fill=(7, 94, 84))
    for y in range(80, 560, 60):
        d.rounded_rectangle([10, y, 200, y + 40], radius=8, fill=(255, 255, 255))
    return im


def _uid(url: str) -> str:
    return "g:" + url.rsplit("/", 1)[-1]


def _build(synthetic, tmp_path, **kw):
    metas, packs, facts = synthetic
    out = tmp_path / "bundle"
    args = {"cfg_hash": "testcfg000", "clip_model": "b32"}
    args.update(kw)
    manifest = build_bundle(metas, packs, out, MergeConfig(), **args)
    return out, manifest, facts


def test_bundle_items_groups_aliases_albums_videos(synthetic, tmp_path):
    out, manifest, facts = _build(synthetic, tmp_path)
    urls = facts["urls"]
    with Bundle(out) as b:
        assert b.count() == 4
        a = b.by_uid(_uid(urls["a"]))
        a2 = b.by_uid(_uid(urls["a2"]))
        item_b = b.by_uid(_uid(urls["b"]))
        shot = b.by_uid(_uid(urls["shot"]))
        assert a["local_date"] == "2021-07-16" and a["day_uncertain"] == 0
        assert a["taken_src"] == "sidecar" and a["match_rule"] == "R1"
        assert a["pack"] == facts["pack_a"] and a["pack_row"] == facts["ia"]
        assert item_b["albums_n"] == 1 and json.loads(item_b["albums"]) == ["Trip"]
        # IMG_B exists in both exports: the newest export's record is primary.
        assert item_b["zip_id"] == 2
        aliases = b.fetchall("SELECT item_id, reason FROM item_aliases")
        assert [(r[0], r[1]) for r in aliases] == [(item_b["item_id"], "double_export")]
        assert shot["is_graphic"] == 1 and a["is_graphic"] == 0

        groups = b.dup_groups()
        assert len(groups) == 1
        g = groups[0]
        assert g["kind"] == "near" and g["deletable"] == 1 and g["size"] == 2
        assert g["seed_item_id"] == a["item_id"]  # camera EXIF beats the q60 copy
        assert [m["item_id"] for m in g["members"]] == [a["item_id"], a2["item_id"]]
        keeper, other = g["members"]
        assert keeper["is_keeper"] == 1 and other["dist"] <= 3 and other["sig_mad"] < 4
        assert g["group_key"] == hashlib.sha1(
            "\n".join(sorted([a["item_uid"], a2["item_uid"]])).encode()).hexdigest()
        assert b.scores_for(a2["item_id"])["dup_extra"] == (1.0, f"duplicate of #{a['item_id']}")
        assert b.scores_for(shot["item_id"])["screenshot"][0] == 0.95

        # One video in two exports is one video, on its New York day.
        assert b.videos_by_day() == {"2021-07-03": 1}

        # Thumbs from the copied pack; embeddings only for items that have one.
        assert b.thumb(a["item_id"], "g") == b"g%d" % facts["ia"]
        assert b.thumb(item_b["item_id"], "g") is None
        emb = b.embeddings()
        assert emb.shape == (2, 512) and emb.dtype == np.float16
        assert a["emb_row"] is not None and item_b["emb_row"] is None
        want = np.frombuffer(facts["emb_a"], dtype="<f2")
        assert np.array_equal(np.asarray(emb[a["emb_row"]]), want)

        stats = b.stats()
        assert stats["aliases"] == 1 and stats["sidecars_unpaired"] == 0
        assert stats["pairing_rules"] == {"R1": 5}
        assert stats["skipped"] == {"edited": 1}
        assert stats["videos_distinct"] == 1 and stats["graphic_items"] == 1
        assert stats["skipped_buckets"] == 0 and stats["missing_shards"] == 0
        assert b.meta["cfg"] == "testcfg000" and b.meta["threshold"] == "3"
        assert b.meta["partial"] == "0" and b.meta["tz_fallback"] == "America/New_York"


def test_manifest_lists_every_file_with_its_hash(synthetic, tmp_path):
    out, manifest, facts = _build(synthetic, tmp_path)
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest
    paths = [f["path"] for f in manifest["files"]]
    # Pack B has no local file and no recorded hash: it is left out (and logged).
    assert paths == ["index.sqlite", "embeddings.f16.npy", f"thumbs/{facts['pack_a']}"]
    for f in manifest["files"]:
        data = (out / f["path"]).read_bytes()
        assert f["size"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()
    assert manifest["partial"] is False and manifest["missing_shards"] == 0
    assert manifest["counts"]["items"] == 4 and manifest["counts"]["dup_groups"] == 1
    assert manifest["clip_model"] == "b32" and manifest["cfg"] == "testcfg000"
    # The npy loads without pickle.
    arr = np.load(out / "embeddings.f16.npy", allow_pickle=False)
    assert arr.shape == (2, 512)


def test_partial_bundle_and_given_pack_hashes(synthetic, tmp_path):
    facts = synthetic[2]
    out, manifest, _facts = _build(synthetic, tmp_path, expected_shards=5, pack_hashes={
        facts["pack_b"]: {"sha256": "ab" * 32, "size": 77}})
    assert manifest["partial"] is True and manifest["missing_shards"] == 3
    entry = next(f for f in manifest["files"] if f["path"] == f"thumbs/{facts['pack_b']}")
    assert entry == {"path": f"thumbs/{facts['pack_b']}", "size": 77, "sha256": "ab" * 32}
    with Bundle(out) as b:
        assert b.stats()["partial"] is True and b.stats()["missing_shards"] == 3


def test_pack_hash_from_shard_info_when_pack_is_remote(tmp_path):
    w = ShardWriter(tmp_path / "a.sqlite", zip_name=ZIP_A,
                    extra_info={"pack_sha256": "cd" * 32, "pack_size": "999"})
    w.image("IMG_1.jpg", image_cols(textured(5)))
    manifest = build_bundle([w.close()], None, tmp_path / "out", MergeConfig(),
                            cfg_hash="testcfg000", clip_model="none")
    thumbs = [f for f in manifest["files"] if f["path"].startswith("thumbs/")]
    assert thumbs == [{"path": f"thumbs/{w.pack_name}", "size": 999, "sha256": "cd" * 32}]
    assert np.load(tmp_path / "out" / "embeddings.f16.npy", allow_pickle=False).shape == (0, 512)


def test_given_pack_hash_without_size_uses_shard_info_then_local_file(synthetic, tmp_path):
    """A caller that knows the hash but not the size (ci passes ``size: None``) still gets an
    int size: the shard's recorded pack_size, else the local file's."""
    facts = synthetic[2]
    local = (synthetic[1] / facts["pack_a"]).stat().st_size
    _out, manifest, _f = _build(synthetic, tmp_path, pack_hashes={
        facts["pack_a"]: {"sha256": "ef" * 32, "size": None}})
    entry = next(f for f in manifest["files"] if f["path"] == f"thumbs/{facts['pack_a']}")
    assert entry == {"path": f"thumbs/{facts['pack_a']}", "size": local, "sha256": "ef" * 32}
    assert all(type(f["size"]) is int for f in manifest["files"])


def test_pack_size_from_shard_info_when_caller_gives_none(tmp_path):
    w = ShardWriter(tmp_path / "a.sqlite", zip_name=ZIP_A,
                    extra_info={"pack_sha256": "cd" * 32, "pack_size": "999"})
    w.image("IMG_1.jpg", image_cols(textured(5)))
    manifest = build_bundle([w.close()], None, tmp_path / "out", MergeConfig(),
                            cfg_hash="testcfg000", clip_model="none",
                            pack_hashes={w.pack_name: {"sha256": "cd" * 32, "size": None}})
    thumbs = [f for f in manifest["files"] if f["path"].startswith("thumbs/")]
    assert thumbs == [{"path": f"thumbs/{w.pack_name}", "size": 999, "sha256": "cd" * 32}]


@pytest.mark.parametrize("via", ["shard_info", "pack_hashes"])
def test_pack_of_unknown_size_fails_the_merge(tmp_path, via):
    """A remote pack whose size nobody recorded must not get a ``size: null`` entry: the merge
    fails before writing anything, and no manifest vouches for the folder."""
    extra = {"pack_sha256": "cd" * 32} if via == "shard_info" else {}
    w = ShardWriter(tmp_path / "a.sqlite", zip_name=ZIP_A, extra_info=extra)
    w.image("IMG_1.jpg", image_cols(textured(5)))
    hashes = {w.pack_name: {"sha256": "cd" * 32}} if via == "pack_hashes" else None
    out = tmp_path / "out"
    with pytest.raises(ManifestError, match="size unknown"):
        build_bundle([w.close()], None, out, MergeConfig(), cfg_hash="testcfg000",
                     clip_model="none", pack_hashes=hashes)
    assert not (out / "manifest.json").exists() and not (out / "index.sqlite").exists()


@pytest.mark.parametrize("size", ["abc", "-5", "1.5"])
def test_corrupt_pack_size_fails_the_merge(tmp_path, size):
    w = ShardWriter(tmp_path / "a.sqlite", zip_name=ZIP_A,
                    extra_info={"pack_sha256": "cd" * 32, "pack_size": str(size)})
    w.image("IMG_1.jpg", image_cols(textured(5)))
    with pytest.raises(ManifestError):
        build_bundle([w.close()], None, tmp_path / "out", MergeConfig(),
                     cfg_hash="testcfg000", clip_model="none")


def test_refuses_shards_of_another_cfg(synthetic, tmp_path):
    metas, packs, _facts = synthetic
    with pytest.raises(ShardMismatch):
        build_bundle(metas, packs, tmp_path / "x", MergeConfig(), cfg_hash="othercfg00",
                     clip_model="b32")
    assert not (tmp_path / "x" / "manifest.json").exists()


def test_rebuild_removes_a_stale_manifest_first(synthetic, tmp_path, monkeypatch):
    out, _m, _f = _build(synthetic, tmp_path)
    import gpclean.merge.bundle as bundle_mod

    def boom(*a, **k):
        raise RuntimeError("crash mid-merge")

    monkeypatch.setattr(bundle_mod, "analyse", boom)
    with pytest.raises(RuntimeError):
        _build(synthetic, tmp_path)
    assert not (out / "manifest.json").exists()


def _analysis_tables(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                for t in ("dup_groups", "dup_members", "bursts", "burst_members", "scores")}
    finally:
        conn.close()


def test_regroup_reproduces_the_build_and_updates_the_manifest(synthetic, tmp_path, capsys):
    out, manifest, _facts = _build(synthetic, tmp_path)
    before = _analysis_tables(out / "index.sqlite")
    res = regroup(out, 3)
    assert res["dup_groups"] == 1 and res["near"] == 1
    assert _analysis_tables(out / "index.sqlite") == before

    assert cli_regroup(out, 2) == 0
    assert "duplicate groups" in capsys.readouterr().out
    new_manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entry = next(f for f in new_manifest["files"] if f["path"] == "index.sqlite")
    data = (out / "index.sqlite").read_bytes()
    assert entry["sha256"] == hashlib.sha256(data).hexdigest() and entry["size"] == len(data)
    assert [f["path"] for f in new_manifest["files"]] == [f["path"] for f in manifest["files"]]
    with Bundle(out) as b:
        assert b.meta["threshold"] == "2"
        assert b.count() == 4


def test_regroup_on_the_fixture_matches_a_fresh_build(fixture_zips, tmp_path):
    zips, _expected = fixture_zips
    metas, pack_dir = shards_from_zips(zips, tmp_path / "scan")
    built5 = tmp_path / "b5"
    build_bundle(metas, pack_dir, built5, MergeConfig(threshold=5), cfg_hash="testcfg000",
                 clip_model="none")
    regrouped = tmp_path / "b3"
    build_bundle(metas, pack_dir, regrouped, MergeConfig(threshold=3), cfg_hash="testcfg000",
                 clip_model="none")
    regroup(regrouped, 5)
    assert _analysis_tables(regrouped / "index.sqlite") == _analysis_tables(
        built5 / "index.sqlite")


def test_no_shards_gives_an_empty_partial_bundle(tmp_path):
    manifest = build_bundle([], None, tmp_path / "empty", MergeConfig(), cfg_hash="testcfg000",
                            clip_model="none", expected_shards=2)
    assert manifest["partial"] is True and manifest["missing_shards"] == 2
    with Bundle(tmp_path / "empty") as b:
        assert b.count() == 0 and b.dup_groups() == [] and b.embeddings() is None


def test_regroup_crash_leaves_the_old_index_and_no_temp_file(synthetic, tmp_path, monkeypatch):
    out, _m, _f = _build(synthetic, tmp_path)
    before = (out / "index.sqlite").read_bytes()
    import gpclean.merge.bundle as bundle_mod

    def boom(*a, **k):
        raise RuntimeError("crash mid-regroup")

    monkeypatch.setattr(bundle_mod, "analyse", boom)
    with pytest.raises(RuntimeError):
        regroup(out, 2)
    assert (out / "index.sqlite").read_bytes() == before
    assert not (out / "index.sqlite.tmp").exists()
    monkeypatch.undo()
    regroup(out, 2)
    assert not (out / "index.sqlite.tmp").exists()


# ---------------------------------------------------------------------------------------------
# Deletion safety: dup_extra 1.0 only for a copy that is provably another library item
# ---------------------------------------------------------------------------------------------


def _png_twins(tmp_path, *, second_folder: str):
    """Export A: X.png (u1) and a byte-identical copy under u2 in ``second_folder``; export B:
    X.png again with its sidecar missing. No EXIF anywhere (a PNG)."""
    cols = image_cols(textured(5, 640, 480), fmt="PNG")
    u1, u2 = fake_url(), fake_url()
    wa = ShardWriter(tmp_path / "a.meta.sqlite", zip_name=ZIP_A)
    wa.image("X.png", cols, folder="Photos from 2021")
    wa.sidecar("X.png.supplemental-metadata.json", folder="Photos from 2021", title="X.png",
               taken_ts=1626444000, url=u1)
    name2 = "Y.png" if second_folder == "Photos from 2021" else "X.png"
    wa.image(name2, cols, folder=second_folder)
    wa.sidecar(f"{name2}.supplemental-metadata.json", folder=second_folder, title=name2,
               taken_ts=1626444100, url=u2)
    wb = ShardWriter(tmp_path / "b.meta.sqlite", zip_name=ZIP_B)
    wb.image("X.png", cols, folder="Photos from 2021")
    out = tmp_path / "bundle"
    build_bundle([wa.close(), wb.close()], None, out, MergeConfig(), cfg_hash="testcfg000",
                 clip_model="none")
    return out, u1, u2


def test_sidecarless_copy_without_exif_collapses_into_its_item(tmp_path):
    """C3 for a PNG: B's X.png is A's X.png (same bytes and name) whose sidecar is missing.
    It must be an alias of item u1, not a 'duplicate' whose deletion deletes the keeper."""
    out, u1, u2 = _png_twins(tmp_path, second_folder="Photos from 2021")
    with Bundle(out) as b:
        assert b.count() == 2
        x = b.by_uid(_uid(u1))
        y = b.by_uid(_uid(u2))
        aliases = b.fetchall("SELECT item_id, reason FROM item_aliases")
        assert [(r[0], r[1]) for r in aliases] == [(x["item_id"], "double_export_nourl")]
        (g,) = b.dup_groups()
        assert g["kind"] == "exact" and g["deletable"] == 1
        assert g["seed_item_id"] == x["item_id"]
        assert b.scores_for(y["item_id"])["dup_extra"] == (1.0, f"duplicate of #{x['item_id']}")


def test_urlless_member_of_a_deletable_group_is_only_a_possible_duplicate(tmp_path):
    """The url-less copy could belong to either url item (ambiguous, so not collapsed). The
    group is deletable through the two urls, but the url-less member is not provably another
    item: deleting it by name could delete the keeper."""
    out, u1, u2 = _png_twins(tmp_path, second_folder="Photos from 2022")
    with Bundle(out) as b:
        assert b.count() == 3
        (g,) = b.dup_groups()
        assert g["deletable"] == 1 and g["size"] == 3
        keeper = g["seed_item_id"]
        by_id = {m["item_id"]: m for m in g["members"]}
        urls = {i: b.item(i)["url"] for i in by_id}
        assert urls[keeper] in (u1, u2)
        for item_id, url in urls.items():
            if item_id == keeper:
                continue
            score = b.scores_for(item_id)["dup_extra"]
            if url is None:
                assert score == (0.6, f"possible duplicate of #{keeper} (may be the same item)")
            else:
                assert score == (1.0, f"duplicate of #{keeper}")


def test_dup_extra_rule_is_per_member_against_the_keeper():
    """analyse() directly: a keeper without a url makes every copy uncertain; a copy with the
    keeper's own url is uncertain; only a copy with a different url is certain."""
    cols = image_cols(textured(9, 640, 480), fmt="PNG")
    base = {**cols, "folder": "Photos from 2021", "export_id": "e", "filename": "IMG.png",
            "taken_ts": None, "local_date": None, "albums_n": 0}

    def rows_for(urls, sizes):
        return [{**base, "item_id": k + 1, "item_uid": f"u{k}", "url": u, "file_size": s}
                for k, (u, s) in enumerate(zip(urls, sizes))]

    def dup_extra(rows):
        a = analyse(rows, MergeConfig())
        (g,) = a.groups
        assert g.deletable
        return g.seed, {rows[k]["item_id"]: s for k, cat, s, _r in a.scores
                        if cat == "dup_extra"}

    # The largest file is the keeper: here it has no url.
    seed, got = dup_extra(rows_for([None, "u1", "u2"], [300, 200, 100]))
    assert seed == 0 and got == {2: 0.6, 3: 0.6}
    # Keeper u1; a url-less copy and a second u1 record are uncertain; u2 is certain.
    seed, got = dup_extra(rows_for(["u1", None, "u1", "u2"], [400, 300, 200, 100]))
    assert seed == 0 and got == {2: 0.6, 3: 0.6, 4: 1.0}


# ---------------------------------------------------------------------------------------------
# Unindexed library items per day
# ---------------------------------------------------------------------------------------------


def test_unindexed_images_are_counted_per_day_next_to_videos(tmp_path):
    """Items that sit on a day in Google Photos but have no index row keep that day from
    looking complete: decode errors, raw / other-format / oversized files. Album copies,
    edited copies, trashed items and errors of an item indexed elsewhere are not counted."""
    ts = 1626444000  # 2021-07-16 14:00 UTC = 10:00 in New York
    y = "Photos from 2021"
    wa = ShardWriter(tmp_path / "a.meta.sqlite", zip_name=ZIP_A)
    wa.image("IMG_1.jpg", image_cols(textured(1)), folder=y)
    wa.sidecar("IMG_1.jpg.supplemental-metadata.json", folder=y, title="IMG_1.jpg",
               taken_ts=ts, url=fake_url())
    # A corrupt JPEG (err, no sha256) with its sidecar: one day, one url.
    err_url = fake_url()
    wa.image("IMG_2.jpg", {"err": "OSError", "file_size": 500}, folder=y)
    wa.sidecar("IMG_2.jpg.supplemental-metadata.json", folder=y, title="IMG_2.jpg",
               taken_ts=ts + 86400, url=err_url)
    # Its album copy fails too: a copy of the same item, not counted again.
    wa.image("IMG_2.jpg", {"err": "OSError", "file_size": 500}, folder="Trip")
    # A scanner TIFF and a Pixel .dng, skipped by the scan, each with its own sidecar.
    wa.skipped(f"Takeout/Google Photos/{y}/scan.tif", "other_image")
    wa.sidecar("scan.tif.supplemental-metadata.json", folder=y, title="scan.tif",
               taken_ts=ts + 2 * 86400, url=fake_url())
    wa.skipped(f"Takeout/Google Photos/{y}/PXL_1.RAW-02.ORIGINAL.dng", "raw")
    wa.sidecar("PXL_1.RAW-02.ORIGINAL.dng.supplemental-metadata.json", folder=y,
               title="PXL_1.RAW-02.ORIGINAL.dng", taken_ts=ts + 2 * 86400, url=fake_url())
    # Trashed, edited, album-folder skips: not library items on a day.
    wa.skipped(f"Takeout/Google Photos/{y}/old.tif", "other_image")
    wa.sidecar("old.tif.supplemental-metadata.json", folder=y, title="old.tif",
               taken_ts=ts, url=fake_url(), trashed=1)
    wa.skipped(f"Takeout/Google Photos/{y}/IMG_1-edited.jpg", "edited")
    wa.skipped("Takeout/Google Photos/Trip/huge.jpg", "too_large")
    # Undated: an oversized photo and a video, both without a sidecar.
    wa.skipped(f"Takeout/Google Photos/{y}/huge.jpg", "too_large")
    wa.video("VID_9.mp4", folder=y)
    meta_a = wa.close()

    # Export B decodes IMG_2 fine: that library item is reviewed through its index row.
    wb = ShardWriter(tmp_path / "b.meta.sqlite", zip_name=ZIP_B)
    wb.image("IMG_2.jpg", image_cols(textured(2)), folder=y)
    wb.sidecar("IMG_2.jpg.supplemental-metadata.json", folder=y, title="IMG_2.jpg",
               taken_ts=ts + 86400, url=err_url)
    wb.image("IMG_3.jpg", {"err": "DecompressionBombError", "file_size": 900}, folder=y)
    wb.sidecar("IMG_3.jpg.supplemental-metadata.json", folder=y, title="IMG_3.jpg",
               taken_ts=ts + 3 * 86400, url=fake_url())
    meta_b = wb.close()

    out = tmp_path / "bundle"
    build_bundle([meta_a], None, out, MergeConfig(), cfg_hash="testcfg000", clip_model="none")
    with Bundle(out) as b:
        assert b.videos_by_day() == {"2021-07-17": 1, "2021-07-18": 2}
        stats = b.stats()
        assert stats["unindexed_images"] == 4 and stats["unindexed_undated"] == 2
        assert stats["videos_distinct"] == 1 and stats["videos_undated"] == 1
        assert stats["image_errors"] == 2 and stats["trashed_excluded"] == 1
        assert stats["sidecars_unpaired"] == 0  # every skipped file claimed its own sidecar

    out2 = tmp_path / "bundle2"
    build_bundle([meta_a, meta_b], None, out2, MergeConfig(), cfg_hash="testcfg000",
                 clip_model="none")
    with Bundle(out2) as b:
        # IMG_2 is indexed from export B; IMG_3 (a decompression bomb) is counted instead.
        assert b.videos_by_day() == {"2021-07-18": 2, "2021-07-19": 1}
        assert b.by_uid(_uid(err_url))["local_date"] == "2021-07-17"
