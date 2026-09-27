"""Tests for gpclean.bundle_read.Bundle against a synthetic bundle (tests/bundle_factory.py)."""

from __future__ import annotations

import io
import json
import sqlite3

import numpy as np
import pytest
from PIL import Image

from bundle_factory import PACKS, VIDEO_ONLY_DAY, make_bundle, plan_items
from gpclean.bundle_read import Bundle

N = 60


@pytest.fixture(scope="module")
def bundle_dir(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("b"), n_items=N)


@pytest.fixture
def b(bundle_dir):
    with Bundle(bundle_dir) as bundle:
        yield bundle


@pytest.fixture(scope="module")
def plan():
    return plan_items(N)


def test_meta_and_manifest(b):
    assert b.meta["cfg"] == "fakecfg000"
    assert b.meta["partial"] == "0"
    assert b.manifest["counts"]["items"] == N
    paths = {f["path"] for f in b.manifest["files"]}
    assert {"index.sqlite", "embeddings.f16.npy", f"thumbs/{PACKS[0]}"} <= paths


def test_missing_manifest_and_index(tmp_path):
    d = make_bundle(tmp_path, n_items=5, with_manifest=False)
    with Bundle(d) as b:
        assert b.manifest == {}
    with pytest.raises(FileNotFoundError):
        Bundle(tmp_path / "nothing")


def test_index_is_read_only(b):
    with pytest.raises(sqlite3.OperationalError):
        b.fetchall("DELETE FROM items")
    assert b.count() == N


def test_item_dicts(b, plan):
    it = b.item(3)
    truth = plan.items[2]
    assert it["item_uid"] == truth["item_uid"]
    assert it["filename"] == truth["filename"]
    assert "sig" not in it
    assert isinstance(it["sha256"], str) and len(it["sha256"]) == 64
    assert it["sha256"] == b.item(2)["sha256"]          # exact dup group shares the sha
    assert b.item(9999) is None
    assert [x["item_id"] for x in b.items([5, 1, 9999, 5])] == [5, 1, 5]
    assert b.by_uid(truth["item_uid"])["item_id"] == 3
    assert b.by_uid("g:nope") is None
    m = b.uids_to_ids([truth["item_uid"], plan.items[10]["item_uid"], "g:nope"])
    assert m == {truth["item_uid"]: 3, plan.items[10]["item_uid"]: 11}
    assert plan.items[10]["item_uid"].startswith("s:")  # i % 11 == 0 has no url


def test_thumbs(b):
    g = b.thumb(1, "g")
    p = b.thumb(2, "p")          # item 2 lives in the other pack
    assert Image.open(io.BytesIO(g)).size == (16, 12)
    assert Image.open(io.BytesIO(p)).size == (64, 48)
    assert Image.open(io.BytesIO(g)).format == "WEBP"
    assert b.thumb(9999, "g") is None
    for bad in ("x", "G", "../p", "g; drop"):
        with pytest.raises(ValueError):
            b.thumb(1, bad)
    assert len(b._packs) == 2    # one cached connection per pack


def test_thumbs_missing_packs(tmp_path):
    d = make_bundle(tmp_path, n_items=5, with_thumbs=False)
    with Bundle(d) as b:
        assert b.thumb(1, "g") is None


def test_bad_pack_name_is_not_used_as_path(tmp_path):
    d = make_bundle(tmp_path, n_items=5)
    # Rewrite one pack name to a traversal attempt (the index is ours, but be defensive).
    conn = sqlite3.connect(d / "index.sqlite")
    conn.execute("UPDATE items SET pack = '../../evil' WHERE item_id = 1")
    conn.commit()
    conn.close()
    with Bundle(d) as b:
        assert b.thumb(1, "g") is None
        assert b.thumb(2, "g") is not None


def test_embeddings(b, plan, tmp_path):
    emb = b.embeddings()
    n_with = sum(1 for it in plan.items if it["item_id"] % 15 != 0)
    assert emb.shape == (n_with, 512) and emb.dtype == np.float16
    assert isinstance(emb, np.memmap)
    assert not emb.flags.writeable
    assert b.embeddings() is emb                         # cached
    norms = np.linalg.norm(emb.astype(np.float32), axis=1)
    assert np.allclose(norms, 1.0, atol=1e-2)
    d = make_bundle(tmp_path, n_items=5, with_embeddings=False)
    with Bundle(d) as nb:
        assert nb.embeddings() is None
        assert nb.item(1)["emb_row"] is None


def test_embeddings_refuse_pickles(tmp_path):
    d = make_bundle(tmp_path, n_items=5)
    np.save(d / "embeddings.f16.npy", np.array([{"a": 1}], dtype=object), allow_pickle=True)
    with Bundle(d) as b:
        assert b.embeddings() is None


@pytest.mark.parametrize("content", [
    b"",                                        # zero-byte file from an interrupted copy
    b"\x93NUMPY\x01\x00",                        # truncated header
    np.full((3, 512), "a"),                     # string dtype
    np.zeros((3, 512), dtype=np.float64),       # wrong float width
    np.zeros((0, 512), dtype=np.float16),       # empty
    np.zeros(512, dtype=np.float16),            # 1-D
], ids=["empty-file", "truncated", "str", "float64", "no-rows", "1d"])
def test_bad_embeddings_are_ignored(tmp_path, content):
    from bundle_factory import StubTextEmbedder
    from gpclean.search import SearchParams, SearchUnavailable, search

    d = make_bundle(tmp_path, n_items=5)
    path = d / "embeddings.f16.npy"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        np.save(path, content, allow_pickle=False)
    with Bundle(d) as b:
        # Same answer on every call: the first failure must not differ from later ones.
        for _ in range(2):
            assert b.embeddings() is None
            with pytest.raises(SearchUnavailable):
                search(b, None, SearchParams(query="receipt", exclude_queued=False),
                       text_embedder=StubTextEmbedder())


def test_make_bundle_refuses_to_reuse_a_directory(tmp_path):
    make_bundle(tmp_path, n_items=3)
    with pytest.raises(ValueError, match="already exists"):
        make_bundle(tmp_path, n_items=3, with_embeddings=False)
    assert make_bundle(tmp_path, n_items=3, name="other").name == "other"


def test_scores(b):
    assert b.scores_for(7) == {"screenshot": (0.95, "Screenshot_ filename")}
    assert b.scores_for(1) == {}
    many = b.scores_many([7, 9, 1])
    assert set(many) == {7, 9}
    assert many[9]["messaging"][0] == pytest.approx(0.9)


def test_junk(b, plan):
    rows, total = b.junk("screenshot", min_score=0.5)
    expected = [it["item_id"] for it in plan.items if it["item_id"] % 7 == 0]
    assert total == len(expected)
    assert sorted(r["item_id"] for r in rows) == expected
    assert all(r["score"] == pytest.approx(0.95) and r["reason"] for r in rows)
    assert all("sig" not in r and isinstance(r["sha256"], str) for r in rows)
    # Ordering: score desc, then date.
    rows, total = b.junk("blur", min_score=0.0, limit=100)
    keys = [(-r["score"], r["local_date"] or "9999") for r in rows]
    assert keys == sorted(keys)
    hi, n_hi = b.junk("blur", min_score=0.6)
    assert n_hi == sum(1 for s in plan.scores if s[1] == "blur" and s[2] >= 0.6)
    page, n = b.junk("blur", min_score=0.0, offset=2, limit=3)
    assert n == total and [r["item_id"] for r in page] == [r["item_id"] for r in rows[2:5]]
    rows, n = b.junk("screenshot", min_score=0.5, year=2020)
    assert n == len(rows) and all(r["year"] == 2020 for r in rows) and n > 0
    with pytest.raises(ValueError):
        b.junk("nonsense", min_score=0.5)
    with pytest.raises(ValueError):
        b.junk("blur", min_score=0.5, limit=0)


def test_dup_groups(b):
    groups = b.dup_groups()
    assert [g["group_id"] for g in groups] == [1, 2]
    g1, g2 = groups
    assert g1["kind"] == "exact" and g1["size"] == 2 and len(g1["group_key"]) == 40
    assert [m["item_id"] for m in g1["members"]] == [2, 3]
    assert g1["members"][0]["is_keeper"] == 1 and g1["members"][1]["is_keeper"] == 0
    assert [m["item_id"] for m in g2["members"]] == [4, 5, 6]
    m = g2["members"][1]
    assert m["dist"] == 2 and m["sig_mad"] == pytest.approx(1.5)
    assert {"sig_block", "sig_chroma", "filename", "item_uid"} <= set(m)
    assert "sig" not in m and isinstance(m["sha256"], str)
    assert [g["group_id"] for g in b.dup_groups(kind="near")] == [2]
    assert [g["group_id"] for g in b.dup_groups(offset=1, limit=5)] == [2]
    assert b.n_dup_groups() == 2 and b.n_dup_groups("exact") == 1
    assert b.dup_group_of(5)["group_id"] == 2
    assert b.dup_group_of(1) is None
    assert b.dup_group(1)["members"][0]["item_id"] == 2
    assert b.dup_group(99) is None
    with pytest.raises(ValueError):
        b.dup_groups(kind="fuzzy")


def test_bursts(b):
    bursts = b.bursts()
    assert len(bursts) == 1
    burst = bursts[0]
    assert burst["best_item_id"] == 21 and burst["size"] == 3
    assert [m["item_id"] for m in burst["members"]][0] == 21
    assert [m["is_best"] for m in burst["members"]] == [1, 0, 0]
    assert [m["rank"] for m in burst["members"]] == [0, 1, 2]
    assert b.burst_of(22)["burst_id"] == 1
    assert b.burst_of(1) is None
    assert b.burst(1)["size"] == 3 and b.n_bursts() == 1


def test_memberships(b):
    m = b.memberships([2, 3, 21, 22, 1])
    assert m[2] == {"dup_group": 1, "is_keeper": 1}
    assert m[3]["is_keeper"] == 0
    assert m[21] == {"burst": 1, "is_best": 1}
    assert m[22]["is_best"] == 0
    assert 1 not in m


def test_videos_by_day(b, plan):
    v = b.videos_by_day()
    assert v[VIDEO_ONLY_DAY] == 3
    assert v[plan.items[9]["local_date"]] == 1


def test_stats(b, plan):
    s = b.stats()
    assert s["items"] == N
    assert s["skipped"] == {"video": 4, "raw": 1}           # stored JSON value decoded
    assert s["unexamined_pairs"] == 0
    assert s["dup_groups"] == {"exact": 1, "near": 1, "deletable": 2, "total": 2}
    assert s["dup_members"] == 5 and s["bursts"] == 1 and s["videos"] == 4
    assert s["junk"]["screenshot"] == sum(1 for it in plan.items if it["item_id"] % 7 == 0)
    assert s["junk"]["pocket"] == 0
    assert s["items_undated"] == 1
    assert sum(s["years"].values()) == N - 1
    assert s["items_with_embeddings"] == sum(1 for i in range(1, N + 1) if i % 15)
    assert s["partial"] is False and s["missing_shards"] == 0
    json.dumps(s)   # must be JSON-serialisable for the MCP / site


def test_partial_bundle_stats(tmp_path):
    with Bundle(make_bundle(tmp_path, n_items=5, partial=True)) as b:
        s = b.stats()
        assert s["partial"] is True and s["missing_shards"] == 1
        # A tiny bundle keeps only the groups that fit and has no bursts; accessors cope.
        assert [g["group_id"] for g in b.dup_groups()] == [1, 2]
        assert b.bursts() == [] and b.burst_of(1) is None


def test_casefold_sql_function(b):
    rows = b.fetchall("SELECT COUNT(*) FROM items WHERE instr(gp_casefold(filename), ?) > 0",
                      ("screenshot_",))
    assert rows[0][0] == sum(1 for i in range(1, N + 1) if i % 7 == 0)


def test_threads_share_bundle(b):
    from concurrent.futures import ThreadPoolExecutor

    def work(i):
        return (b.item(i % N + 1)["item_id"], len(b.thumb(i % N + 1, "g")))

    with ThreadPoolExecutor(8) as ex:
        out = list(ex.map(work, range(200)))
    assert len(out) == 200
