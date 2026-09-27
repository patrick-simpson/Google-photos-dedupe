"""Tests for gpclean.fixtures.generate: the fake Takeout export and its ground truth.

``expected.json`` is what the end-to-end test holds the whole pipeline to, so besides the
structural checks these tests re-derive parts of the truth independently: from the zip bytes
(keeper order, video days, key/url consistency) and, where those modules are importable,
with the real pairing and fingerprint code (every planted pair behaves as documented).
"""

from __future__ import annotations

import io
import itertools
import json
import os
import struct
import time
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pillow_heif
import pytest
from PIL import Image

from gpclean import cli
from gpclean.fixtures.generate import ZIP_NAMES, expected_view, generate, sidecar_name

pillow_heif.register_heif_opener()

ROOT = "Takeout/Google Photos/"
NY = ZoneInfo("America/New_York")
SMALL_BUDGET_S = 20.0


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> dict:
    """One --small fixture shared by every test in this module (with its generation time)."""
    out = tmp_path_factory.mktemp("fx")
    start = time.perf_counter()
    expected = generate(out, seed=0, small=True)
    elapsed = time.perf_counter() - start
    zips = {name: zipfile.ZipFile(out / name) for name in ZIP_NAMES}
    yield {"out": out, "expected": expected, "zips": zips, "elapsed": elapsed}
    for zf in zips.values():
        zf.close()


def _split(ref: str) -> tuple[str, str]:
    zip_name, member = ref.split("::", 1)
    return zip_name, member


def _read(fx: dict, ref: str) -> bytes:
    zip_name, member = _split(ref)
    return fx["zips"][zip_name].read(member)


def _names(fx: dict) -> set[str]:
    return {f"{z}::{n}" for z, zf in fx["zips"].items() for n in zf.namelist()}


# ---------------------------------------------------------------------------------------------
# Files, determinism, speed
# ---------------------------------------------------------------------------------------------


def test_writes_three_zips_and_expected_json(fx):
    out = fx["out"]
    assert sorted(p.name for p in out.iterdir()) == sorted([*ZIP_NAMES, "expected.json"])
    on_disk = json.loads((out / "expected.json").read_text(encoding="utf-8"))
    assert on_disk == fx["expected"]


def test_small_mode_is_fast(fx):
    assert fx["elapsed"] < SMALL_BUDGET_S


def test_same_seed_gives_identical_bytes(fx, tmp_path):
    # Through the real CLI entry point, which also checks the argument wiring.
    assert cli.main(["fixtures", "--out", str(tmp_path), "--seed", "0", "--small"]) == 0
    for name in [*ZIP_NAMES, "expected.json"]:
        assert (tmp_path / name).read_bytes() == (fx["out"] / name).read_bytes(), name


def test_zips_are_valid_and_mix_storage_methods(fx):
    methods = set()
    for zf in fx["zips"].values():
        assert zf.testzip() is None  # every CRC checks out
        for info in zf.infolist():
            methods.add(info.compress_type)
            assert info.date_time[0] == 2026
            assert not info.is_dir()
    assert methods == {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def test_one_member_is_forced_zip64(fx):
    """The local header of exactly one member carries a ZIP64 extra field (id 0x0001)."""
    found = []
    for name, zf in fx["zips"].items():
        raw = (fx["out"] / name).read_bytes()
        for info in zf.infolist():
            off = info.header_offset
            assert raw[off:off + 4] == b"PK\x03\x04"
            name_len, extra_len = struct.unpack("<HH", raw[off + 26:off + 30])
            extra = raw[off + 30 + name_len:off + 30 + name_len + extra_len]
            if extra[:2] == b"\x01\x00":
                found.append(f"{name}::{info.filename}")
                assert info.extract_version >= 45
    assert len(found) == 1
    assert found[0] in fx["expected"]["item_keys"]  # an image the scanner must really read


# ---------------------------------------------------------------------------------------------
# Internal consistency of expected.json
# ---------------------------------------------------------------------------------------------


def test_every_reference_points_to_an_existing_member(fx):
    e, names = fx["expected"], _names(fx)
    refs = list(e["item_keys"]) + list(e["pairings"]) + list(e["skipped"])
    refs += [r for r in e["pairings"].values() if r]
    refs += e["videos"]["members"]
    refs += [r for c in e["collapsed"] for r in c["copies"]]
    missing = [r for r in refs if r not in names]
    assert not missing


def test_every_member_is_accounted_for(fx):
    """Each zip member is exactly one of: image, video, skipped, or a .json sidecar."""
    e = fx["expected"]
    images, videos, skipped = set(e["item_keys"]), set(e["videos"]["members"]), set(e["skipped"])
    assert not (images & videos or images & skipped or videos & skipped)
    for ref in _names(fx):
        if ref in images or ref in videos or ref in skipped:
            continue
        assert ref.endswith(".json") and _split(ref)[1].startswith(ROOT), ref


def test_keys_are_consistent(fx):
    e = fx["expected"]
    keys = set(e["item_keys"].values())
    assert len(keys) == e["n_items"]
    assert set(e["pairings"]) == set(e["item_keys"])
    for ref, key in e["item_keys"].items():
        json_ref = e["pairings"][ref]
        if key.startswith("nourl:"):
            assert key == "nourl:" + ref.rsplit("/", 1)[-1]
        else:
            assert key.startswith("AF1QipFAKE")
        if json_ref:
            doc = json.loads(_read(fx, json_ref))
            if not key.startswith("nourl:"):
                assert doc["url"] == "https://photos.google.com/photo/" + key
            # A sidecar sits in the same folder as its media, possibly in another zip part
            # of the same export ("takeout-<stamp>-NNN.zip" minus the part number).
            (jzip, jmember), (mzip, mmember) = _split(json_ref), _split(ref)
            assert jzip.rsplit("-", 1)[0] == mzip.rsplit("-", 1)[0]
            assert jmember.rsplit("/", 1)[0] == mmember.rsplit("/", 1)[0]
    used = [r for r in e["pairings"].values() if r]
    assert len(used) == len(set(used))  # no sidecar serves two media members


def test_groups_bursts_and_pairs_use_known_keys(fx):
    e = fx["expected"]
    keys = set(e["item_keys"].values())
    group_of = {}
    for g in e["dup_groups"]:
        assert len(g["members"]) >= 2 and len(set(g["members"])) == len(g["members"])
        assert g["keeper"] in g["members"]
        for m in g["members"]:
            assert m in keys and m not in group_of, m
            group_of[m] = g["id"]
    for a, b in e["must_not_group"]:
        assert a in keys and b in keys and a != b
        assert not (a in group_of and group_of.get(a) == group_of.get(b)), (a, b)
    for b in e["bursts"]:
        assert b["best"] in b["members"] and set(b["members"]) <= keys
        assert not {group_of.get(m) for m in b["members"]} - {None}
    for c in e["collapsed"]:
        assert len(c["copies"]) >= 2
        assert {e["item_keys"][r] for r in c["copies"]} == {c["item_key"]}
    multi = {k for k in keys if list(e["item_keys"].values()).count(k) > 1}
    assert multi == {c["item_key"] for c in e["collapsed"]}
    assert set(e["junk"]) <= keys


def test_junk_matches_groups_bursts_and_names(fx):
    """The deterministic categories are exhaustive and follow from names and structure."""
    e = fx["expected"]
    junk = e["junk"]
    extras = {m for g in e["dup_groups"] for m in g["members"] if m != g["keeper"]}
    burst_extras = {m for b in e["bursts"] for m in b["members"] if m != b["best"]}
    for key in set(e["item_keys"].values()):
        cats = set(junk.get(key, ()))
        assert ("dup_extra" in cats) == (key in extras), key
        assert ("burst_extra" in cats) == (key in burst_extras), key
    for ref, key in e["item_keys"].items():
        fname = ref.rsplit("/", 1)[-1]
        json_ref = e["pairings"][ref]
        origin = ""
        if json_ref:
            doc = json.loads(_read(fx, json_ref))
            origin = (doc.get("googlePhotosOrigin", {}).get("mobileUpload", {})
                      .get("deviceFolder", {}).get("localFolderName", ""))
        cats = set(junk.get(key, ()))
        shot = fname.startswith("Screenshot_") or origin == "Screenshots"
        assert ("screenshot" in cats) == shot, fname
        messaging = fname.startswith(("received_", "FB_IMG_", "signal-")) or "-WA" in fname
        messaging = messaging or origin in ("WhatsApp Images", "Messenger", "Facebook", "Signal")
        assert ("messaging" in cats) == messaging, fname


def test_expected_counts_are_sane(fx):
    e = fx["expected"]
    ids = [g["id"] for g in e["dup_groups"]]
    assert ids == ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P7b", "P8", "P9", "P10", "P11",
                   "P12"]
    assert [c["id"] for c in e["collapsed"]] == ["C1", "C1b", "C2", "C2b", "C3"]
    assert [b["id"] for b in e["bursts"]] == ["N4", "N4b", "BURST"]
    assert 60 <= e["n_items"] <= 100
    assert len(e["item_keys"]) == e["n_items"] + sum(len(c["copies"]) - 1
                                                     for c in e["collapsed"])
    assert sorted(set(e["skipped"].values())) == [
        "album_video", "edited", "non_sidecar_json", "other_image", "outside_photos", "raw",
        "trash"]
    assert sum(1 for v in e["pairings"].values() if v is None) == 2  # S7 and C3's B copy
    assert e["include_albums"] is True
    assert all(k.startswith(("AF1QipFAKE", "nourl:")) for k in e["item_keys"].values())


def test_small_mode_plants_archived_and_trash(fx):
    """--small covers the archived flag (same in both exports) and a Trash folder skip."""
    e = fx["expected"]
    archived = {}
    for ref, jr in e["pairings"].items():
        if jr and json.loads(_read(fx, jr)).get("archived"):
            archived.setdefault(e["item_keys"][ref], set()).add(_split(ref)[0])
    assert len(archived) == 1
    (zips,) = archived.values()
    assert len(zips) == 2  # the C2 photo, exported twice
    assert [r for r, why in e["skipped"].items() if why == "trash"] == [
        "takeout-20260101T000000Z-001.zip::Takeout/Google Photos/Trash/IMG_0001.jpg"]


def test_view_without_albums_skips_album_photos_only(fx):
    e = fx["expected"]
    assert expected_view(e, include_albums=True) == e
    v = expected_view(e, include_albums=False)
    moved = set(e["item_keys"]) - set(v["item_keys"])
    assert moved and all("/Photos from " not in r for r in moved)
    assert {r: v["skipped"][r] for r in moved} == dict.fromkeys(moved, "album_media")
    assert set(v["pairings"]) == set(v["item_keys"])
    assert v["n_items"] == e["n_items"] == len(set(v["item_keys"].values()))
    assert [c["id"] for c in v["collapsed"]] == ["C2", "C2b", "C3"]
    assert v["dup_groups"] == e["dup_groups"] and v["junk"] == e["junk"]
    members = pytest.importorskip("gpclean.takeout.members")
    for ref in moved:
        assert members.classify(_split(ref)[1], include_albums=False).reason == "album_media"


def test_default_mode_fillers_are_distinct_items():
    """Default mode only appends N7 fillers (planted last, so --small is a strict subset).
    Built without writing zips: the full 300-filler run takes ~30 s."""
    import gpclean.fixtures.generate as gen

    b = gen._Builder(seed=0)
    gen._plant_fillers(b, 12)
    e = b.expected()
    fixed = 4  # WebP, GIF, non-ASCII name, export-B-only photo
    assert e["n_items"] == fixed + 12
    assert not e["dup_groups"] and not e["bursts"]
    assert len(e["must_not_group"]) == 11  # adjacent filler pairs
    shas = {hash(b.data_of(r)) for r in e["item_keys"]}
    assert len(shas) == len(e["item_keys"])
    assert gen.N_FILLERS_DEFAULT == 300


# ---------------------------------------------------------------------------------------------
# Truth re-derived from the bytes
# ---------------------------------------------------------------------------------------------


def _record(fx: dict, ref: str) -> dict:
    """Keeper-relevant facts of one member, read back from the zip like the scanner would."""
    e = fx["expected"]
    data = _read(fx, ref)
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        exif = im.getexif()
        # pillow-heif has already applied HEIF orientation; JPEG orientation 5-8 swaps w/h.
        if im.format != "HEIF" and exif.get(0x0112, 1) in (5, 6, 7, 8):
            w, h = h, w
        sub = exif.get_ifd(0x8769)
        camera = bool(exif.get(0x010F) or exif.get(0x0110) or sub.get(0x829A))
        gps_exif = bool(exif.get_ifd(0x8825))
        dto = sub.get(0x9003)
    doc = json.loads(_read(fx, e["pairings"][ref])) if e["pairings"][ref] else {}
    gps_side = bool(doc) and (doc["geoData"]["latitude"], doc["geoData"]["longitude"]) != (0, 0)
    if doc:
        assert gps_side == gps_exif, ref  # the docstring promises GPS in both or neither
        taken = int(doc["photoTakenTime"]["timestamp"])
    else:
        taken = int(datetime.strptime(dto, "%Y:%m:%d %H:%M:%S").replace(tzinfo=NY).timestamp())
    url = doc.get("url")
    albums = 0
    if url:
        for other, key in e["item_keys"].items():
            jr = e["pairings"][other]
            if jr and "/Photos from " not in other and json.loads(_read(fx, jr))["url"] == url:
                albums += 1
    return {"pixels": w * h, "camera": camera, "gps": gps_exif, "fav": bool(doc.get("favorited")),
            "albums": albums, "size": len(data), "taken": taken}


def test_keepers_follow_plan_keeper_order(fx):
    """PLAN deviation 5: resolution, camera EXIF, GPS, favorited/albums, larger file, earlier
    taken. Each planted keeper must be the unique best; a full tie would make it arbitrary."""
    e = fx["expected"]
    first_member = {}
    for ref, key in e["item_keys"].items():
        if "/Photos from " in ref:
            first_member.setdefault(key, ref)
    for g in e["dup_groups"]:
        ranked = []
        for key in g["members"]:
            r = _record(fx, first_member[key])
            ranked.append(((-r["pixels"], -r["camera"], -r["gps"], -r["fav"], -r["albums"],
                            -r["size"], r["taken"]), key))
        ranked.sort()
        assert ranked[0][1] == g["keeper"], g["id"]
        assert ranked[0][0] != ranked[1][0], g["id"]


def test_video_days_follow_sidecars_in_new_york_time(fx):
    e = fx["expected"]
    days: dict[str, int] = {}
    for ref in e["videos"]["members"]:
        zip_name, member = _split(ref)
        folder, fname = member.rsplit("/", 1)
        doc = json.loads(fx["zips"][zip_name].read(f"{folder}/{sidecar_name(fname)}"))
        assert doc["title"] == fname
        day = datetime.fromtimestamp(int(doc["photoTakenTime"]["timestamp"]), NY).date()
        days[day.isoformat()] = days.get(day.isoformat(), 0) + 1
    assert days == e["videos"]["by_day"]
    # The planted time-zone traps: a UTC-named Pixel video and the DST-change night.
    assert "2022-07-03" in days and "2023-03-11" in days


# Skip reasons whose year-folder files are library items without an index row (mirrors
# gpclean.merge.load.UNINDEXED_SKIP_REASONS, restated so this check stays independent).
_UNINDEXED_SKIPS = ("raw", "other_image", "too_large")


def test_unindexed_by_day_counts_videos_and_skipped_media_per_url(fx):
    """Re-derive ``unindexed_by_day`` from the zip bytes: every year-folder video, plus every
    year-folder raw / other-format / oversized skip whose own sidecar (same folder, title =
    file name) exists, once per url, on its New York day."""
    e = fx["expected"]
    media = list(e["videos"]["members"])
    media += [r for r, why in e["skipped"].items()
              if why in _UNINDEXED_SKIPS and "/Photos from " in r]
    days: dict[str, int] = {}
    urls: set[str] = set()
    for ref in media:
        zip_name, member = _split(ref)
        folder, fname = member.rsplit("/", 1)
        doc = json.loads(fx["zips"][zip_name].read(f"{folder}/{sidecar_name(fname)}"))
        assert doc["title"] == fname
        if doc["url"] in urls:
            continue
        urls.add(doc["url"])
        day = datetime.fromtimestamp(int(doc["photoTakenTime"]["timestamp"]), NY).date()
        days[day.isoformat()] = days.get(day.isoformat(), 0) + 1
    assert days == e["unindexed_by_day"]
    assert list(e["unindexed_by_day"]) == sorted(e["unindexed_by_day"])
    # Videos are a part of it; the planted .dng (2023) and .tif (2018) add the rest.
    for day, n in e["videos"]["by_day"].items():
        assert e["unindexed_by_day"][day] >= n
    extra = sum(e["unindexed_by_day"].values()) - sum(e["videos"]["by_day"].values())
    assert extra == 2 and "2018-03-03" in e["unindexed_by_day"]


def test_unindexed_by_day_is_the_same_without_albums(fx):
    e = fx["expected"]
    v = expected_view(e, include_albums=False)
    assert v["unindexed_by_day"] == e["unindexed_by_day"] and v["videos"] == e["videos"]


def test_expected_json_on_disk_has_unindexed_by_day(fx):
    on_disk = json.loads((fx["out"] / "expected.json").read_text(encoding="utf-8"))
    assert on_disk["unindexed_by_day"] == fx["expected"]["unindexed_by_day"]
    assert all(type(n) is int and n > 0 for n in on_disk["unindexed_by_day"].values())


def test_motion_photo_is_jpeg_followed_by_mp4(fx):
    ref = next(r for r in fx["expected"]["item_keys"] if r.endswith(".MP.jpg"))
    data = _read(fx, ref)
    eoi = data.index(b"\xff\xd9\x00\x00\x00\x18ftyp")
    assert data[:2] == b"\xff\xd8" and eoi > 1000
    with Image.open(io.BytesIO(data)) as im:
        im.load()


def test_display_p3_heic_embeds_a_real_icc_profile(fx):
    ref = next(r for r in fx["expected"]["item_keys"] if r.endswith("IMG_3100.HEIC"))
    with Image.open(io.BytesIO(_read(fx, ref))) as im:
        icc = im.info.get("icc_profile")
    assert icc and b"Display P3" in icc
    from PIL import ImageCms

    assert "P3" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc)))


@pytest.mark.parametrize("media, style, dup, expected", [
    ("IMG_1234.jpg", "new", None, "IMG_1234.jpg.supplemental-metadata.json"),
    ("IMG_1234.jpg", "new", 1, "IMG_1234.jpg.supplemental-metadata(1).json"),
    ("IMG_1234.jpg", "old", 1, "IMG_1234.jpg(1).json"),
    ("PXL_20230101_123456789.jpg", "new", None,
     "PXL_20230101_123456789.jpg.supplemental-metada.json"),
    ("PXL_20220402_101500123.LONG_EXPOSURE.jpg", "new", None,
     "PXL_20220402_101500123.LONG_EXPOSURE.jpg.suppl.json"),
    ("PXL_20220402_101500123.PORTRAIT.ORIGINAL.jpg", "new", None,
     "PXL_20220402_101500123.PORTRAIT.ORIGINAL.jpg.s.json"),
    ("Family reunion at the lake house summer evening.jpg", "old", None,
     "Family reunion at the lake house summer evenin.json"),
])
def test_sidecar_names_follow_takeout_truncation(media, style, dup, expected):
    assert sidecar_name(media, style=style, dup=dup) == expected
    assert len(expected) <= 51 + (len(f"({dup})") if dup else 0)


# ---------------------------------------------------------------------------------------------
# Cross-checks against the real takeout / imaging code (skipped if not importable)
# ---------------------------------------------------------------------------------------------


def test_classification_agrees_with_members_module(fx):
    members = pytest.importorskip("gpclean.takeout.members")
    e = fx["expected"]
    for ref in _names(fx):
        zip_name, name = _split(ref)
        mc = members.classify(name, include_albums=True)
        if mc.kind == "image":
            assert ref in e["item_keys"], ref
        elif mc.kind == "video":
            assert ref in e["videos"]["members"], ref
        elif mc.kind == "skip":
            assert e["skipped"].get(ref) == mc.reason, ref
        else:
            assert mc.kind == "sidecar", ref


def _exif_ts(data: bytes) -> int | None:
    with Image.open(io.BytesIO(data)) as im:
        dto = im.getexif().get_ifd(0x8769).get(0x9003)
    if not dto:
        return None
    return int((datetime.strptime(dto, "%Y:%m:%d %H:%M:%S") - datetime(1970, 1, 1))
               .total_seconds())


def test_pairings_agree_with_sidecar_engine(fx):
    """The real pairing rules find exactly the planted sidecar for every image member."""
    sidecar = pytest.importorskip("gpclean.takeout.sidecar")
    names = pytest.importorskip("gpclean.takeout.names")
    e = fx["expected"]
    media, cars = [], []
    for ref in sorted(_names(fx)):
        zip_name, member = _split(ref)
        if not member.startswith(ROOT) or member.count("/") != 3:
            continue
        folder, fname = member[len(ROOT):].split("/")
        eid = names.export_id(zip_name)
        if ref in e["item_keys"] or ref in e["videos"]["members"]:
            ts = _exif_ts(_read(fx, ref)) if ref in e["item_keys"] else None
            media.append(sidecar.MediaRef(ref, eid, folder, fname, ts))
        elif fname.endswith(".json") and fname != "metadata.json":
            p = sidecar.parse_sidecar(_read(fx, ref))
            cars.append(sidecar.SidecarRef(ref, eid, folder, fname, p["title"], p["taken_ts"]))
    result = sidecar.pair(media, cars)
    wrong = {r: (want, result[r].sidecar_key) for r, want in e["pairings"].items()
             if result[r].sidecar_key != want}
    assert not wrong
    assert all(result[r].sidecar_key for r in e["videos"]["members"])


def _capture_ok(a: dict, b: dict) -> bool:
    """PLAN capture-time rule: 0 < |dt| <= 60 s and not whole hours -> not a duplicate."""
    if not a["exif_dt"] or not b["exif_dt"]:
        return True
    fmt = "%Y:%m:%d %H:%M:%S"
    delta = abs((datetime.strptime(a["exif_dt"], fmt) - datetime.strptime(b["exif_dt"], fmt))
                .total_seconds())
    if a["exif_subsec"] and b["exif_subsec"]:
        delta = abs(delta + float("0." + a["exif_subsec"]) - float("0." + b["exif_subsec"]))
    return not (0 < delta <= 60 and abs(delta / 3600 - round(delta / 3600)) > 1e-6)


def _fingerprint_rows(fx: dict) -> dict[str, dict]:
    """One imaging row per library item (its first member), plus the PLAN ``graphic`` flag."""
    imaging = pytest.importorskip("gpclean.imaging")
    from gpclean.config import ScanConfig

    cfg = ScanConfig(clip_model="none")
    e = fx["expected"]
    rows = {}
    for ref, key in e["item_keys"].items():
        if key in rows:
            continue
        row = imaging.process_image(_read(fx, ref), cfg)[0]
        jr = e["pairings"][ref]
        doc = json.loads(_read(fx, jr)) if jr else {}
        origin = doc.get("googlePhotosOrigin", {})
        shot = ref.rsplit("/", 1)[-1].startswith("Screenshot_") or "Screenshots" in str(origin)
        # PLAN's screen-shape score (no camera EXIF + screen dimensions) is left out on
        # purpose: the fixture keeps photo-shaped no-EXIF items off it (module docstring).
        row["graphic"] = (shot or imaging.sig_luma_std(row["sig"]) < 8
                          or row["flat_frac"] >= 0.35)
        row["sidecar_taken"] = int(doc["photoTakenTime"]["timestamp"]) if doc else None
        rows[key] = row
    return rows


def _local_capture(row: dict) -> datetime | None:
    """Naive local capture time: EXIF DateTimeOriginal (+SubSec), else the sidecar in NY."""
    if row["exif_dt"]:
        t = datetime.strptime(row["exif_dt"], "%Y:%m:%d %H:%M:%S")
        if row["exif_subsec"]:
            t += timedelta(seconds=float("0." + row["exif_subsec"]))
        return t
    if row["sidecar_taken"] is not None:
        return datetime.fromtimestamp(row["sidecar_taken"], NY).replace(tzinfo=None)
    return None


def _check_fingerprints(fx: dict, rows: dict[str, dict]) -> None:
    """Positives pass every edge check at T=2; no other pair of items passes them at T=5.

    Mirrors PLAN section 5 "Duplicate grouping": exact SHA always matches; otherwise both must
    be non-graphic, pHash within T, aspect within 2%, signature verified and capture-time OK.
    """
    imaging = pytest.importorskip("gpclean.imaging")
    from gpclean.config import MergeConfig

    mcfg = MergeConfig()
    e = fx["expected"]

    def matches(ka: str, kb: str, t: int) -> bool:
        a, b = rows[ka], rows[kb]
        if a["sha256"] == b["sha256"]:
            return True
        if a["graphic"] or b["graphic"]:
            return False
        ra, rb = a["width"] / a["height"], b["width"] / b["height"]
        return (imaging.hamming(a["phash64"], b["phash64"]) <= t
                and abs(ra - rb) / max(ra, rb) <= mcfg.aspect_tol
                and imaging.sig_verify(a["sig"], b["sig"], mcfg) and _capture_ok(a, b))

    group_of = {m: g["id"] for g in e["dup_groups"] for m in g["members"]}
    for g in e["dup_groups"]:
        for m in g["members"]:
            assert matches(g["keeper"], m, 2), (g["id"], m)
    stray = [(a, b) for a, b in itertools.combinations(sorted(rows), 2)
             if not (a in group_of and group_of[a] == group_of.get(b)) and matches(a, b, 5)]
    assert not stray

    # Bursts: frames are close in pHash, share a camera model, and the planted best is the
    # sharpest. Time bursts are checked on *every* pair: frames with equal capture times may
    # come out of the merge in any order, so any two of them can end up as neighbours.
    # Filename bursts are bursts by name, whatever their pixels.
    for burst in e["bursts"]:
        frames = burst["members"]
        best = max(frames, key=lambda k: rows[k]["lap_var"])
        assert best == burst["best"], burst["id"]
        if burst["id"] != "BURST":
            for a, b in itertools.combinations(frames, 2):
                dist = imaging.hamming(rows[a]["phash64"], rows[b]["phash64"])
                assert dist <= mcfg.burst_phash_max, (burst["id"], dist)
        assert len({rows[k]["model"] for k in frames}) == 1, burst["id"]
    # N4 is the capture-time-rule case: its pixels match, only the timestamps differ.
    n4 = next(b for b in e["bursts"] if b["id"] == "N4")["members"]
    assert imaging.sig_verify(rows[n4[0]]["sig"], rows[n4[1]]["sig"], mcfg)
    assert not _capture_ok(rows[n4[0]], rows[n4[1]])

    # Planted pixel-statistics junk clears the PLAN starting gates with margin.
    gates = {"blur": lambda r: r["lap_var"] < 50, "dark": lambda r: r["luma_mean"] < 25,
             "overexposed": lambda r: r["frac_bright"] > 0.5 or r["luma_mean"] > 215,
             "tiny": lambda r: max(r["width"], r["height"]) < 480,
             "pocket": lambda r: r["has_camera_exif"] and (
                 (r["luma_mean"] < 25 and r["luma_std"] < 12) or r["flat_frac"] > 0.6
                 or r["lap_var"] < 20)}
    for key, cats in e["junk"].items():
        for cat in cats:
            if cat in gates:
                assert gates[cat](rows[key]), (key, cat)


def _check_no_unplanned_bursts(fx: dict, rows: dict[str, dict]) -> None:
    """``bursts`` is exact: no unplanned time neighbours, no unplanned filename bursts."""
    from gpclean.config import MergeConfig

    names = pytest.importorskip("gpclean.takeout.names")
    mcfg = MergeConfig()
    e = fx["expected"]
    burst_of = {m: b["id"] for b in e["bursts"] for m in b["members"]}
    group_of = {m: g["id"] for g in e["dup_groups"] for m in g["members"]}

    # Time: any two items within the burst window must already be one burst or one group
    # (the check is stricter than the burst rule, which also wants pHash and camera model).
    timed = sorted((t, k) for k, r in rows.items() if (t := _local_capture(r)) is not None)
    for (ta, a), (tb, b) in zip(timed, timed[1:]):
        if (tb - ta).total_seconds() <= mcfg.burst_window_s:
            same_burst = a in burst_of and burst_of[a] == burst_of.get(b)
            same_group = a in group_of and group_of[a] == group_of.get(b)
            assert same_burst or same_group, (a, b, ta, tb)

    # Filenames: burst_key groups per (export, folder) are exactly the planted filename bursts;
    # a lone recognised name (the Pixel RAW cover) is not a burst.
    by_key: dict[tuple, set[str]] = {}
    for ref, key in e["item_keys"].items():
        zip_name, member = _split(ref)
        folder, fname = member.rsplit("/", 1)
        bk = names.burst_key(fname)
        if bk:
            by_key.setdefault((names.export_id(zip_name), folder, bk[0]), set()).add(key)
    planted = [set(b["members"]) for b in e["bursts"]]
    lone = set()
    for frames in by_key.values():
        if len(frames) >= 2:
            assert frames in planted, frames
        else:
            lone |= frames
    assert lone and not lone & set(burst_of)


@pytest.fixture(scope="module")
def rows(fx) -> dict[str, dict]:
    return _fingerprint_rows(fx)


def test_planted_pairs_behave_as_documented_under_real_fingerprints(fx, rows):
    _check_fingerprints(fx, rows)


def test_bursts_are_exactly_the_planted_ones(fx, rows):
    _check_no_unplanned_bursts(fx, rows)


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("GPCLEAN_TEST_SLOW") != "1", reason="set GPCLEAN_TEST_SLOW=1")
def test_default_mode_fixture_under_real_fingerprints(tmp_path):
    """The full fixture (300 N7 fillers, what a full e2e run uses) keeps every promise too.
    About a minute, so opt-in."""
    expected = generate(tmp_path, seed=0, small=False)
    zips = {name: zipfile.ZipFile(tmp_path / name) for name in ZIP_NAMES}
    try:
        full = {"out": tmp_path, "expected": expected, "zips": zips}
        assert expected["n_items"] > 300
        rows_ = _fingerprint_rows(full)
        _check_fingerprints(full, rows_)
        _check_no_unplanned_bursts(full, rows_)
    finally:
        for zf in zips.values():
            zf.close()
