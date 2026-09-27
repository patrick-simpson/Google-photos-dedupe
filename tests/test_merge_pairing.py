"""Tests for gpclean.merge.load + gpclean.merge.pairing (global sidecar pairing)."""

from __future__ import annotations

import hashlib

import pytest
from shard_factory import ShardWriter, fake_url

from gpclean.merge.load import ShardMismatch, load_shards
from gpclean.merge.pairing import pair_all

NY = "America/New_York"
A1 = "takeout-20260101T000000Z-001.zip"
A2 = "takeout-20260101T000000Z-002.zip"
B1 = "takeout-20260201T000000Z-001.zip"


def _cols(name: str, **extra) -> dict:
    return {"sha256": hashlib.sha256(name.encode()).digest(), "file_size": 1000, **extra}


def _load(tmp_path, build):
    """``build(writers)`` fills writers keyed by zip name; returns the Loaded shards."""
    writers = {z: ShardWriter(tmp_path / f"{z}.meta.sqlite", zip_name=z) for z in (A1, A2, B1)}
    build(writers)
    return load_shards([w.close() for w in writers.values()])


def _by_name(paired):
    return {(r["zip_name"], r["filename"]): r for r in paired.images}


def test_cross_zip_pairing_attaches_sidecar_fields(tmp_path):
    url = fake_url()

    def build(w):
        w[A1].image("IMG_0001.jpg", _cols("a", exif_dt="2020:05:05 10:00:00"))
        w[A2].sidecar("IMG_0001.jpg.supplemental-metadata.json", title="IMG_0001.jpg",
                      taken_ts=1588687200, url=url, favorited=1, origin_folder="Camera",
                      lat=31.5, lon=-44.5)

    paired = pair_all(_load(tmp_path, build), NY)
    rec = _by_name(paired)[(A1, "IMG_0001.jpg")]
    assert rec["url"] == url and rec["match_rule"] == "R1" and rec["match_conf"] == "high"
    assert rec["favorited"] == 1 and rec["origin_folder"] == "Camera"
    assert rec["sc_taken_ts"] == 1588687200 and rec["sc_lat"] == 31.5
    assert paired.unmatched_sidecars == 0
    assert paired.rules["R1"] == 1


def test_exports_do_not_pair_across(tmp_path):
    def build(w):
        w[A1].image("IMG_0002.jpg", _cols("b"))
        w[B1].sidecar("IMG_0002.jpg.json", title="IMG_0002.jpg", url=fake_url())

    paired = pair_all(_load(tmp_path, build), NY)
    rec = _by_name(paired)[(A1, "IMG_0002.jpg")]
    assert rec["url"] is None and rec["match_rule"] == "none"
    assert paired.unmatched_sidecars == 1


def test_video_claims_its_own_sidecar(tmp_path):
    """A live photo's HEIC and MOV share a stem; the MOV must keep its own JSON."""
    photo_url, video_url = fake_url(), fake_url()

    def build(w):
        w[A1].image("IMG_1234.HEIC", _cols("c"))
        w[A1].video("IMG_1234.MOV", file_size=5000)
        w[A1].sidecar("IMG_1234.HEIC.supplemental-metadata.json", title="IMG_1234.HEIC",
                      url=photo_url, taken_ts=1600000000)
        w[A1].sidecar("IMG_1234.MOV.supplemental-metadata.json", title="IMG_1234.MOV",
                      url=video_url, taken_ts=1600000000)

    paired = pair_all(_load(tmp_path, build), NY)
    assert _by_name(paired)[(A1, "IMG_1234.HEIC")]["url"] == photo_url
    assert [v["url"] for v in paired.videos] == [video_url]
    assert paired.videos[0]["sc_taken_ts"] == 1600000000


def test_album_sidecars_give_membership_without_album_media(tmp_path):
    url = fake_url()

    def build(w):
        w[A1].image("IMG_0003.jpg", _cols("d"))
        w[A1].sidecar("IMG_0003.jpg.supplemental-metadata.json", title="IMG_0003.jpg", url=url)
        for album in ("Trip", "Best of"):
            w[A2].sidecar("IMG_0003.jpg.supplemental-metadata.json", folder=album,
                          title="IMG_0003.jpg", url=url)
        w[A2].sidecar("gone.jpg.json", folder="Trip", title="gone.jpg", url=fake_url(),
                      trashed=1)

    paired = pair_all(_load(tmp_path, build), NY)
    assert paired.albums_by_url[url] == {"Trip", "Best of"}
    assert paired.album_sidecars == 3
    assert len(paired.albums_by_url) == 1  # trashed album entries are not memberships
    assert paired.unmatched_sidecars == 0  # album sidecars are counted separately


def test_trashed_sidecar_excludes_its_photo(tmp_path):
    def build(w):
        w[A1].image("IMG_0004.jpg", _cols("e"))
        w[A1].sidecar("IMG_0004.jpg.json", title="IMG_0004.jpg", url=fake_url(), trashed=1)
        w[A1].image("IMG_0005.jpg", _cols("f"))

    paired = pair_all(_load(tmp_path, build), NY)
    assert [r["filename"] for r in paired.images] == ["IMG_0005.jpg"]
    assert paired.trashed == 1


def test_failed_image_is_dropped_but_keeps_its_sidecar(tmp_path):
    def build(w):
        w[A1].image("IMG_0006.jpg", {"file_size": 10, "err": "OSError"})
        w[A1].sidecar("IMG_0006.jpg.json", title="IMG_0006.jpg", url=fake_url())

    paired = pair_all(_load(tmp_path, build), NY)
    assert paired.images == [] and paired.image_errors == 1
    assert paired.unmatched_sidecars == 0


def test_truncated_sidecar_name_pairs_with_exif_time_in_window(tmp_path):
    """A sidecar name cut to 46 characters still pairs (EXIF, read as New York time, agrees
    with photoTakenTime)."""
    long_name = "Holiday dinner with the whole family at grandmas A.jpg"
    url = fake_url()

    def build(w):
        w[A1].image(long_name, _cols("g", exif_dt="2018:12:24 19:00:00"))
        # 19:00 New York (EST) = 00:00 UTC next day.
        w[A1].sidecar(long_name[:46] + ".json", title=long_name, url=url, taken_ts=1545609600)

    rec = _by_name(pair_all(_load(tmp_path, build), NY))[(A1, long_name)]
    assert rec["url"] == url


def test_load_refuses_mixed_configs(tmp_path):
    a = ShardWriter(tmp_path / "a.sqlite", zip_name=A1, cfg="cfgA").close()
    b = ShardWriter(tmp_path / "b.sqlite", zip_name=A2, cfg="cfgB").close()
    with pytest.raises(ShardMismatch):
        load_shards([a, b])
    c = ShardWriter(tmp_path / "c.sqlite", zip_name=B1, extract_version=999).close()
    with pytest.raises(ShardMismatch):
        load_shards([a, c])


def test_load_refuses_duplicate_shards_and_conflicting_zipkeys(tmp_path):
    a = ShardWriter(tmp_path / "a.sqlite", zip_name=A1, zipkey="k1").close()
    a2 = ShardWriter(tmp_path / "a2.sqlite", zip_name=A1, zipkey="k1").close()
    with pytest.raises(ShardMismatch):
        load_shards([a, a2])
    other = ShardWriter(tmp_path / "o.sqlite", zip_name=A2, zipkey="k1", shard=1).close()
    with pytest.raises(ShardMismatch):
        load_shards([a, other])


def test_load_keeps_zip_names_exports_and_skip_counts(tmp_path):
    w = ShardWriter(tmp_path / "a.sqlite", zip_name=A1, zipkey="k1", shard=0)
    w.skipped("Takeout/Google Photos/Photos from 2020/x-edited.jpg", "edited")
    w.skipped("Takeout/Google Photos/Photos from 2020/y.dng", "raw")
    a = w.close()
    b = ShardWriter(tmp_path / "b.sqlite", zip_name=A1, zipkey="k1", shard=1).close()
    c = ShardWriter(tmp_path / "c.sqlite", zip_name=B1, zipkey="k2").close()
    loaded = load_shards([a, b, c])
    assert loaded.zips["k1"]["name"] == A1
    assert loaded.zips["k1"]["export_id"] == "takeout-20260101T000000Z"
    assert loaded.zips["k2"]["export_id"] == "takeout-20260201T000000Z"
    assert loaded.skipped == {"edited": 1, "raw": 1}
    assert len(loaded.shards) == 3 and loaded.cfg == "testcfg000"
