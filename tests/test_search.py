"""Tests for gpclean.search: filters, sorting, paging, queue status and CLIP similarity."""

from __future__ import annotations

import time

import numpy as np
import pytest

from bundle_factory import StubTextEmbedder, concept_vector, make_bundle, plan_items
from gpclean.bundle_read import Bundle
from gpclean.review_db import ReviewDB
from gpclean.search import SIM_THRESHOLD, SearchParams, SearchUnavailable, search

N = 60


@pytest.fixture(scope="module")
def bundle_dir(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("b"), n_items=N)


@pytest.fixture
def b(bundle_dir):
    with Bundle(bundle_dir) as bundle:
        yield bundle


@pytest.fixture
def review(tmp_path):
    with ReviewDB(tmp_path / "review.sqlite") as r:
        yield r


@pytest.fixture(scope="module")
def plan():
    return plan_items(N)


def ids(res):
    return [r["item_id"] for r in res["rows"]]


def run(b, review=None, embedder=None, **kw):
    kw.setdefault("limit", 200)
    kw.setdefault("exclude_queued", False)
    return search(b, review, SearchParams(**kw), text_embedder=embedder)


# ---------------------------------------------------------------- filters

def test_no_filters_returns_everything_by_date(b, plan):
    res = run(b)
    assert res["total"] == N and res["offset"] == 0 and res["next_offset"] is None
    dated = sorted((it for it in plan.items if it["local_date"]),
                   key=lambda it: (it["local_date"], it["local_time"], it["item_id"]))
    assert ids(res) == [it["item_id"] for it in dated] + [N]     # undated last
    row = res["rows"][0]
    assert "sig" not in row and isinstance(row["sha256"], str)
    assert {"flags", "sim", "queue", "score", "dup_group", "burst"} <= set(row)
    assert row["sim"] is None and row["queue"] is None


def test_category_and_min_score(b, plan):
    res = run(b, category="blur")                   # default min 0.5
    expected = {s[0] for s in plan.scores if s[1] == "blur" and s[2] >= 0.5}
    assert set(ids(res)) == expected and res["total"] == len(expected)
    res = run(b, category="blur", min_score=0.0)
    assert set(ids(res)) == {s[0] for s in plan.scores if s[1] == "blur"}
    # sort defaults to score for a category: descending score, ties by date.
    scores = [r["score"] for r in res["rows"]]
    assert scores == sorted(scores, reverse=True)
    res = run(b, category="any", min_score=0.9)
    assert set(ids(res)) == {s[0] for s in plan.scores if s[2] >= 0.9}
    res = run(b, min_score=0.85)                     # min_score alone means any category
    assert set(ids(res)) == {s[0] for s in plan.scores if s[2] >= 0.85}


def test_flags(b):
    res = run(b, category="screenshot")
    row = next(r for r in res["rows"] if r["item_id"] == 35)     # screenshot + blur
    assert [f["category"] for f in row["flags"]] == ["screenshot", "blur"]
    assert row["flags"][0] == {"category": "screenshot", "score": 0.95,
                               "reason": "Screenshot_ filename"}
    assert row["score"] == pytest.approx(0.95)


def test_date_range_and_year(b, plan):
    res = run(b, date_from="2019-03-01", date_to="2019-12-31")
    expected = {it["item_id"] for it in plan.items
                if it["local_date"] and "2019-03-01" <= it["local_date"] <= "2019-12-31"}
    assert set(ids(res)) == expected and expected
    res = run(b, year=2021)
    assert set(ids(res)) == {it["item_id"] for it in plan.items if it["year"] == 2021}
    res = run(b, date_to="2018-12-31", category="screenshot", min_score=0.5)
    assert all(r["local_date"] <= "2018-12-31" and r["item_id"] % 7 == 0 for r in res["rows"])


def test_filename_and_origin_casefold(b, plan):
    res = run(b, filename_contains="SCREENSHOT_")
    assert set(ids(res)) == {i for i in range(1, N + 1) if i % 7 == 0}
    res = run(b, filename_contains="-wa0")
    assert set(ids(res)) == {it["item_id"] for it in plan.items
                             if "-WA0" in it["filename"]}
    res = run(b, origin_contains="whatsapp")
    assert set(ids(res)) == {it["item_id"] for it in plan.items
                             if it["origin_folder"] == "WhatsApp Images"}
    # LIKE wildcards are plain text here.
    assert run(b, filename_contains="%")["total"] == 0
    assert run(b, filename_contains="_0001")["total"] == 1


def test_group_filter(b):
    assert set(ids(run(b, group="dup:2"))) == {4, 5, 6}
    assert set(ids(run(b, group="burst:1"))) == {20, 21, 22}
    assert run(b, group="dup:99")["total"] == 0
    rows = {r["item_id"]: r for r in run(b, group="dup:2")["rows"]}
    assert rows[4]["dup_group"] == 2 and rows[4]["is_keeper"] == 1 and rows[5]["is_keeper"] == 0
    rows = {r["item_id"]: r for r in run(b, group="burst:1")["rows"]}
    assert rows[21]["is_best"] == 1 and rows[20]["burst"] == 1


def test_has_gps(b, plan):
    with_gps = {it["item_id"] for it in plan.items if it["lat"] is not None}
    assert set(ids(run(b, has_gps=True))) == with_gps
    assert set(ids(run(b, has_gps=False))) == set(range(1, N + 1)) - with_gps


# ---------------------------------------------------------------- queue

def test_exclude_queued_and_queue_status(b, review, plan):
    uid = {it["item_id"]: it["item_uid"] for it in plan.items}
    review.propose([(uid[7], "screenshot of a chat", "screenshot")], proposer="claude:code",
                   batch_id="b1")
    review.user_add([(uid[14], "user does not want it", None), (uid[21], "dup extra", None)])
    review.mark_deleted([uid[21]], True)
    review.propose([(uid[28], "another screenshot", None)], proposer="claude:code",
                   batch_id="b1")
    review.decide([uid[28]], "reject")
    res = search(b, review, SearchParams(category="screenshot", limit=200))
    assert set(ids(res)).isdisjoint({7, 14, 21, 28})
    res = search(b, review, SearchParams(category="screenshot", exclude_queued=False, limit=200))
    q = {r["item_id"]: r["queue"] for r in res["rows"]}
    assert q[7] == "proposed" and q[14] == "approved" and q[21] == "deleted"
    assert q[28] == "rejected" and q[35] is None
    # No review DB at all: nothing excluded, queue always None.
    res = search(b, None, SearchParams(category="screenshot", limit=200))
    assert 7 in ids(res) and all(r["queue"] is None for r in res["rows"])


# ---------------------------------------------------------------- paging / sorting

def test_pagination(b):
    full = ids(run(b, sort="date"))
    pages, offset = [], 0
    while offset is not None:
        res = run(b, sort="date", limit=7, offset=offset)
        assert res["total"] == N and res["offset"] == offset
        pages += ids(res)
        offset = res["next_offset"]
    assert pages == full
    res = run(b, limit=10, offset=N + 5)
    assert res["rows"] == [] and res["next_offset"] is None and res["total"] == N


def test_sort_score_date(b):
    res = run(b, sort="score")
    scores = [r["score"] or 0.0 for r in res["rows"]]
    assert scores == sorted(scores, reverse=True)
    res = run(b, category="blur", min_score=0.0, sort="date")
    dates = [r["local_date"] or "9999" for r in res["rows"]]
    assert dates == sorted(dates)


@pytest.mark.parametrize("kw", [
    {"limit": 0}, {"limit": 201}, {"offset": -1}, {"category": "cats"},
    {"min_score": 1.5}, {"min_score": True}, {"date_from": "2020-13-01"},
    {"date_to": "01/02/2020"}, {"year": 99}, {"group": "dup:x"}, {"group": "album:1"},
    {"sort": "random"}, {"sort": "similarity"}, {"query": "x" * 201},
    {"filename_contains": 5}, {"has_gps": "yes"}, {"limit": True},
    {"exclude_queued": "false"}, {"exclude_queued": 0}, {"exclude_queued": None},
])
def test_invalid_params(b, kw):
    with pytest.raises(ValueError):
        run(b, **kw)


# ---------------------------------------------------------------- similarity

def test_text_query_similarity(b, plan):
    emb = StubTextEmbedder()
    res = run(b, embedder=emb, query="receipt")
    assert emb.calls == ["receipt"]
    planted = {i for i, c in plan.concept_of.items() if c == "receipt" and i % 15 != 0}
    assert set(ids(res)) == planted                 # noise items fall below the threshold
    sims = [r["sim"] for r in res["rows"]]
    assert sims == sorted(sims, reverse=True)       # default sort for a query
    assert all(s >= SIM_THRESHOLD for s in sims)
    # Check the similarity value against a direct float32 computation.
    e = b.embeddings()
    top = res["rows"][0]
    direct = float(e[top["emb_row"]].astype(np.float32) @ concept_vector("receipt"))
    assert top["sim"] == pytest.approx(direct, abs=1e-3)


def test_text_query_combined_with_filters(b, plan):
    emb = StubTextEmbedder()
    res = run(b, embedder=emb, query="beach", year=2020, sort="date")
    expected = {i for i, c in plan.concept_of.items()
                if c == "beach" and i % 15 != 0 and plan.items[i - 1]["year"] == 2020}
    assert set(ids(res)) == expected
    dates = [r["local_date"] for r in res["rows"]]
    assert dates == sorted(dates)
    assert all(r["sim"] is not None for r in res["rows"])


def test_text_query_uses_custom_vector_and_threshold(b):
    # A query vector equal to one item's embedding finds that item with sim ~= 1.
    e = b.embeddings()
    target = b.item(8)
    vec = e[target["emb_row"]].astype(np.float32)
    res = run(b, embedder=StubTextEmbedder(vectors={"q": vec}), query="q", limit=3)
    assert res["rows"][0]["item_id"] == 8 and res["rows"][0]["sim"] > 0.99
    # An orthogonal-ish random query matches nothing above the threshold.
    rng = np.random.default_rng(123)
    v = rng.standard_normal(512).astype(np.float32)
    res = run(b, embedder=StubTextEmbedder(vectors={"q": v / np.linalg.norm(v)}), query="q")
    assert res["total"] == 0


def test_text_query_unavailable(b, tmp_path):
    with pytest.raises(SearchUnavailable):
        run(b, query="receipt")                     # no embedder
    with pytest.raises(SearchUnavailable):
        run(b, embedder=StubTextEmbedder(dim=256), query="receipt")
    with Bundle(make_bundle(tmp_path, n_items=5, with_embeddings=False)) as nb:
        with pytest.raises(SearchUnavailable):
            run(nb, embedder=StubTextEmbedder(), query="receipt")
        assert run(nb)["total"] == 5                # plain filters still work


@pytest.mark.slow
def test_large_bundle_is_fast(tmp_path):
    """100k-scale smoke check: filters + similarity stay well under a second or two."""
    d = make_bundle(tmp_path, n_items=20_000, with_thumbs=False, with_manifest=False)
    with Bundle(d) as big:
        emb = StubTextEmbedder()
        t0 = time.perf_counter()
        res = search(big, None, SearchParams(query="receipt", limit=50), text_embedder=emb)
        res2 = search(big, None, SearchParams(category="blur", min_score=0.0, limit=50))
        elapsed = time.perf_counter() - t0
    assert res["total"] > 1000 and res2["total"] == 4000
    assert elapsed < 5.0
