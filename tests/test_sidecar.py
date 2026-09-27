"""Tests for gpclean.takeout.sidecar: parsing, url validation and pairing rules R1-R6.

All names, titles and urls are synthetic. Scenarios S1-S11 refer to docs/PLAN.md section 11.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata

import pytest

from gpclean.schema import SHARD_DDL
from gpclean.takeout.sidecar import (
    NO_PAIRING,
    MediaRef,
    Pairing,
    SidecarRef,
    pair,
    parse_sidecar,
    validate_photos_url,
)

EXP = "takeout-20260101T000000Z"
EXP_B = "takeout-20260201T000000Z"
YEAR = "Photos from 2020"
T0 = 1_600_000_000  # EXIF wall-clock time read as UTC
TZ = 4 * 3600  # photoTakenTime (real UTC) = EXIF local time + 4 h (New York summer)
FAKE_ID = "AF1QipFAKE0123456789abcdefXYZ"

# ---------------------------------------------------------------------------------------------
# validate_photos_url
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (f"https://photos.google.com/photo/{FAKE_ID}", f"https://photos.google.com/photo/{FAKE_ID}"),
        (f"https://photos.google.com/photo/{FAKE_ID}/", f"https://photos.google.com/photo/{FAKE_ID}"),
        (f"https://photos.google.com/photo/{FAKE_ID}?authuser=1",
         f"https://photos.google.com/photo/{FAKE_ID}"),
        (f"https://photos.google.com/photo/{FAKE_ID}#x", f"https://photos.google.com/photo/{FAKE_ID}"),
        ("https://photos.google.com/photo/AF1Qip_-9x", "https://photos.google.com/photo/AF1Qip_-9x"),
        ("https://photos.google.com/photo/short", None),  # id < 10 chars
        ("https://photos.google.com/photo/" + "a" * 201, None),
        (f"http://photos.google.com/photo/{FAKE_ID}", None),
        (f"https://evil.example/photo/{FAKE_ID}", None),
        (f"https://photos.google.com.evil.example/photo/{FAKE_ID}", None),
        (f"https://user@photos.google.com/photo/{FAKE_ID}", None),
        (f"https://photos.google.com:8443/photo/{FAKE_ID}", None),
        (f"https://photos.google.com/album/{FAKE_ID}", None),
        (f"https://photos.google.com/photo/{FAKE_ID}/extra", None),
        ("https://photos.google.com/photo/AF1Qip\nFAKE0123456", None),
        (f" https://photos.google.com/photo/{FAKE_ID}", None),
        (f"https://photos.google.com/photo/{FAKE_ID}?q=<script>", f"https://photos.google.com/photo/{FAKE_ID}"),
        (f"https://photos.google.com/photo/{FAKE_ID}?q=a b", None),
        ("javascript:alert(1)", None),
        ("", None),
        (None, None),
        (12345, None),
    ],
)
def test_validate_photos_url(url, expected):
    assert validate_photos_url(url) == expected


# ---------------------------------------------------------------------------------------------
# parse_sidecar
# ---------------------------------------------------------------------------------------------

FULL = {
    "title": "IMG_20200101_120000.jpg",
    "description": "A synthetic caption",
    "imageViews": "3",
    "creationTime": {"timestamp": "1600000100", "formatted": "..."},
    "photoTakenTime": {"timestamp": "1600000000", "formatted": "..."},
    "geoData": {"latitude": 40.5, "longitude": -73.25, "altitude": 10.0,
                "latitudeSpan": 0.0, "longitudeSpan": 0.0},
    "geoDataExif": {"latitude": 40.25, "longitude": -73.5, "altitude": 11.0},
    "people": [{"name": "Person A"}, {"name": "Persona B"}, {"x": 1}, "junk"],
    "url": f"https://photos.google.com/photo/{FAKE_ID}",
    "googlePhotosOrigin": {
        "mobileUpload": {"deviceFolder": {"localFolderName": "WhatsApp Images"},
                         "deviceType": "ANDROID_PHONE"},
    },
    "favorited": True,
    "archived": False,
}

KEYS = {
    "title", "taken_ts", "creation_ts", "lat", "lon", "alt", "lat_exif", "lon_exif", "url",
    "description", "people", "origin_folder", "device_type", "from_shared_album", "from_partner",
    "favorited", "archived", "trashed", "raw", "err",
}


def test_parse_full():
    out = parse_sidecar(json.dumps(FULL).encode())
    assert set(out) == KEYS
    assert out["title"] == "IMG_20200101_120000.jpg"
    assert out["taken_ts"] == 1_600_000_000 and out["creation_ts"] == 1_600_000_100
    assert (out["lat"], out["lon"], out["alt"]) == (40.5, -73.25, 10.0)
    assert (out["lat_exif"], out["lon_exif"]) == (40.25, -73.5)
    assert out["url"] == f"https://photos.google.com/photo/{FAKE_ID}"
    assert out["description"] == "A synthetic caption"
    assert json.loads(out["people"]) == ["Person A", "Persona B"]
    assert out["origin_folder"] == "WhatsApp Images"
    assert out["device_type"] == "ANDROID_PHONE"
    assert (out["from_shared_album"], out["from_partner"]) == (0, 0)
    assert (out["favorited"], out["archived"], out["trashed"]) == (1, 0, 0)
    assert json.loads(out["raw"]) == FULL
    assert out["err"] is None


def test_parse_minimal_and_defaults():
    out = parse_sidecar(b'{"title": "a.jpg", "description": "", "people": []}')
    assert out["title"] == "a.jpg"
    for k in ("taken_ts", "creation_ts", "lat", "lon", "alt", "url", "description", "people",
              "origin_folder", "device_type", "err"):
        assert out[k] is None, k
    assert (out["favorited"], out["archived"], out["trashed"]) == (0, 0, 0)


def test_parse_shared_partner_trashed():
    doc = {"title": "x.jpg", "trashed": True,
           "googlePhotosOrigin": {"fromSharedAlbum": {}, "fromPartnerSharing": {}}}
    out = parse_sidecar(json.dumps(doc).encode())
    assert (out["from_shared_album"], out["from_partner"], out["trashed"]) == (1, 1, 1)


@pytest.mark.parametrize(
    "geo, expected",
    [
        ({"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}, (None, None, None)),
        ({"latitude": 0.0, "longitude": 5.0, "altitude": 1.0}, (0.0, 5.0, 1.0)),
        ({"latitude": 95.0, "longitude": 5.0}, (None, None, None)),
        ({"latitude": "40", "longitude": 5.0}, (None, None, None)),
        ({"latitude": True, "longitude": 5.0}, (None, None, None)),
        ("not a dict", (None, None, None)),
    ],
)
def test_parse_geo(geo, expected):
    out = parse_sidecar(json.dumps({"geoData": geo}).encode())
    assert (out["lat"], out["lon"], out["alt"]) == expected


@pytest.mark.parametrize(
    "ts, expected",
    [("1600000000", 1_600_000_000), (1600000000, 1_600_000_000), ("0", None), ("abc", None),
     ("²", None), (True, None), (None, None), ("-86400", -86400), ({"x": 1}, None)],
)
def test_parse_timestamps(ts, expected):
    out = parse_sidecar(json.dumps({"photoTakenTime": {"timestamp": ts}}).encode())
    assert out["taken_ts"] == expected
    assert out["err"] is None


def test_parse_bad_url_dropped():
    out = parse_sidecar(json.dumps({"url": "https://evil.example/photo/AF1QipFAKE123"}).encode())
    assert out["url"] is None and out["err"] is None


@pytest.mark.parametrize(
    "data, err",
    [
        (b"{not json", "JSONDecodeError"),
        (b"", "JSONDecodeError"),
        (b"[1, 2]", "TypeError"),
        (b'"text"', "TypeError"),
        (b"\xff\xfe\x00", "UnicodeDecodeError"),
        (b"[" * 100_000 + b"]" * 100_000, "RecursionError"),
    ],
)
def test_parse_never_raises(data, err):
    out = parse_sidecar(data)
    assert set(out) == KEYS
    assert out["err"] == err
    assert all(v is None for k, v in out.items() if k != "err")


def test_parse_bom_and_raw_truncation():
    doc = {"title": "é.jpg", "description": "ü" * 70_000}
    out = parse_sidecar(b"\xef\xbb\xbf" + json.dumps(doc, ensure_ascii=False).encode())
    assert out["err"] is None and out["title"] == "é.jpg"
    assert len(out["raw"].encode("utf-8")) <= 64 * 1024
    assert out["raw"].startswith('{"title": "é.jpg"')


@pytest.mark.parametrize("ts", [99999999999999999999999, 1e300, -1e300, 10**12, "9" * 13])
def test_parse_out_of_range_timestamps(ts):
    out = parse_sidecar(json.dumps({"photoTakenTime": {"timestamp": ts},
                                    "creationTime": {"timestamp": ts}}).encode())
    assert out["taken_ts"] is None and out["creation_ts"] is None and out["err"] is None


def test_parse_output_fits_sidecars_raw_table():
    # One malformed sidecar must not crash the scan when its row is written.
    doc = {"title": "t" * 5000, "description": "d" * 5_000_000,
           "photoTakenTime": {"timestamp": 99999999999999999999999},
           "creationTime": {"timestamp": 1e300},
           "people": [{"name": "p" * 1000}] * 500,
           "googlePhotosOrigin": {"mobileUpload": {"deviceType": "x" * 5000,
                                                   "deviceFolder": {"localFolderName": "f" * 5000}}}}
    out = parse_sidecar(json.dumps(doc).encode())
    assert out["err"] is None
    assert len(out["title"].encode()) <= 1024
    assert len(out["description"].encode()) <= 4096
    assert len(out["origin_folder"].encode()) <= 1024 and len(out["device_type"].encode()) <= 1024
    people = json.loads(out["people"])
    assert len(people) == 200 and all(len(p) <= 200 for p in people)

    con = sqlite3.connect(":memory:")
    con.executescript(SHARD_DDL)
    row = {"member_idx": 1, "member": "m", "folder": "f", "folder_kind": "year",
           "json_name": "x.json", **out}
    cols = ", ".join(row)
    con.execute(f"INSERT INTO sidecars_raw ({cols}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()))
    assert con.execute("SELECT taken_ts, creation_ts FROM sidecars_raw").fetchone() == (None, None)
    con.close()


# ---------------------------------------------------------------------------------------------
# pairing helpers
# ---------------------------------------------------------------------------------------------


def M(filename, exif=T0, *, folder=YEAR, export=EXP, key=None):
    return MediaRef(key=key or (export, folder, filename), export_id=export, folder=folder,
                    filename=filename, exif_ts=exif)


def S(json_name, title, taken=T0 + TZ, *, folder=YEAR, export=EXP, key=None):
    return SidecarRef(key=key or (export, folder, json_name), export_id=export, folder=folder,
                      json_name=json_name, title=title, taken_ts=taken)


def run(media, sidecars):
    """pair() with results as {media filename: (json name | None, rule, conf)}."""
    res = pair(media, sidecars)
    assert set(res) == {m.key for m in media}
    names = {s.key: s.json_name for s in sidecars}
    return {k[2] if isinstance(k, tuple) else k: (names.get(p.sidecar_key), p.rule, p.conf)
            for k, p in res.items()}


NONE = (None, "none", "none")
SUPP = ".supplemental-metadata"


def trunc_json(media_name: str, total: int = 51, dup: str = "") -> str:
    """How Takeout names a sidecar when the whole name is capped at ``total`` characters."""
    room = total - len(".json") - len(dup)
    core = (media_name + SUPP)[:room]
    return core + dup + ".json"


# WhatsApp names its own "(1)" copies with a space; Takeout's index never has one. Saved in the
# same second and without EXIF, so time cannot tell them apart.
WA_A = "WhatsApp Image 2021-03-04 at 10.11.12 AM.jpeg"  # 45 chars
WA_B = "WhatsApp Image 2021-03-04 at 10.11.12 AM (1).jpeg"  # 49 chars
WA_SB = trunc_json(WA_B)  # "WhatsApp Image 2021-03-04 at 10.11.12 AM (1).j.json"
WA_SA = trunc_json(WA_A, total=52)  # "...AM.jpeg.s.json"

# Two ordinary long names where one is a prefix of the other (not cut by Takeout at all).
HOL_A = "IMG_20200101_120000_holiday_in_the_mountains.jpg"
HOL_B = "IMG_20200101_120000_holiday_in_the_mountains_2.jpg"

CAFE_NFC = unicodedata.normalize("NFC", "Café_1.jpg")
CAFE_NFD = unicodedata.normalize("NFD", "Café_1.jpg")
assert CAFE_NFC != CAFE_NFD

# A 57-char screenshot-style name whose first 46 characters are shared by two different files.
LONG_P = "Screenshot_20200101-120000_Samsung Internet Browser_"  # 52 chars
assert len(LONG_P) == 52


# ---------------------------------------------------------------------------------------------
# Scenario table: (id, media, sidecars, expected {filename: (json, rule, conf)})
# ---------------------------------------------------------------------------------------------

CASES = [
    (
        "S1 new style",
        [M("IMG_1.jpg")],
        [S("IMG_1.jpg.supplemental-metadata.json", "IMG_1.jpg")],
        {"IMG_1.jpg": ("IMG_1.jpg.supplemental-metadata.json", "R1", "high")},
    ),
    (
        "S2 old style",
        [M("IMG_2.jpg")],
        [S("IMG_2.jpg.json", "IMG_2.jpg")],
        {"IMG_2.jpg": ("IMG_2.jpg.json", "R1", "high")},
    ),
    (
        "S3 IMG.jpg and IMG(1).jpg are different photos, new style",
        [M("IMG.jpg"), M("IMG(1).jpg", T0 + 500)],
        [S("IMG.jpg.supplemental-metadata.json", "IMG.jpg"),
         S("IMG.jpg.supplemental-metadata(1).json", "IMG.jpg", T0 + 500 + TZ)],
        {"IMG.jpg": ("IMG.jpg.supplemental-metadata.json", "R1", "high"),
         "IMG(1).jpg": ("IMG.jpg.supplemental-metadata(1).json", "R2", "high")},
    ),
    (
        "S3 IMG.jpg and IMG(1).jpg, old style",
        [M("IMG.jpg"), M("IMG(1).jpg", T0 + 500)],
        [S("IMG.jpg(1).json", "IMG.jpg", T0 + 500 + TZ), S("IMG.jpg.json", "IMG.jpg")],
        {"IMG.jpg": ("IMG.jpg.json", "R1", "high"),
         "IMG(1).jpg": ("IMG.jpg(1).json", "R2", "high")},
    ),
    (
        "S3 (1) kept on the media name inside the sidecar name",
        [M("IMG_7(1).jpg")],
        [S("IMG_7(1).jpg.supplemental-metadata.json", "IMG_7.jpg")],
        {"IMG_7(1).jpg": ("IMG_7(1).jpg.supplemental-metadata.json", "R1", "high")},
    ),
    (
        "S4 truncated .supplemental-metada",
        [M("PXL_20230615_101112345.jpg")],
        [S("PXL_20230615_101112345.jpg.supplemental-metada.json", "PXL_20230615_101112345.jpg")],
        {"PXL_20230615_101112345.jpg":
         ("PXL_20230615_101112345.jpg.supplemental-metada.json", "R3", "high")},
    ),
    (
        "S4 truncated .suppl and .s",
        [M("Screenshot_20200101-120000_Chrome Browser.jpg"),
         M("Screenshot_20200101-120000_Samsung Internet.jpg", T0 + 99)],
        [S("Screenshot_20200101-120000_Chrome Browser.jpg.suppl.json",
           "Screenshot_20200101-120000_Chrome Browser.jpg"),
         S("Screenshot_20200101-120000_Samsung Internet.jpg.s.json",
           "Screenshot_20200101-120000_Samsung Internet.jpg", T0 + 99 + TZ)],
        {"Screenshot_20200101-120000_Chrome Browser.jpg":
         ("Screenshot_20200101-120000_Chrome Browser.jpg.suppl.json", "R3", "high"),
         "Screenshot_20200101-120000_Samsung Internet.jpg":
         ("Screenshot_20200101-120000_Samsung Internet.jpg.s.json", "R3", "high")},
    ),
    (
        "S4 truncated with (1) relocated",
        [M("PXL_20230615_101112345.jpg"), M("PXL_20230615_101112345(1).jpg", T0 + 3)],
        [S(trunc_json("PXL_20230615_101112345.jpg"), "PXL_20230615_101112345.jpg"),
         S(trunc_json("PXL_20230615_101112345.jpg", dup="(1)"), "PXL_20230615_101112345.jpg",
           T0 + 3 + TZ)],
        {"PXL_20230615_101112345.jpg": (trunc_json("PXL_20230615_101112345.jpg"), "R3", "high"),
         "PXL_20230615_101112345(1).jpg":
         (trunc_json("PXL_20230615_101112345.jpg", dup="(1)"), "R3", "high")},
    ),
    (
        "S4 .supplemental-metadata dropped entirely",
        [M("Screenshot_20200101-120000_Some Long Application.png")],
        [S("Screenshot_20200101-120000_Some Long Application.png.json",
           "Screenshot_20200101-120000_Some Long Application.png")],
        {"Screenshot_20200101-120000_Some Long Application.png":
         ("Screenshot_20200101-120000_Some Long Application.png.json", "R1", "high")},
    ),
    (
        "S4 old 46-char name: media part truncated in the sidecar",
        [M(LONG_P + "x1.jpg")],
        [S((LONG_P + "x1.jpg")[:46] + ".json", LONG_P + "x1.jpg")],
        {LONG_P + "x1.jpg": ((LONG_P + "x1.jpg")[:46] + ".json", "R3", "high")},
    ),
    (
        "S4 old export: media name truncated too, title keeps the full name",
        [M(LONG_P[:43] + ".jpg")],
        [S(LONG_P[:46] + ".json", LONG_P + "full.jpg")],
        {LONG_P[:43] + ".jpg": (LONG_P[:46] + ".json", "R3", "low")},
    ),
    (
        "S5 shared truncated prefix resolved by title",
        [M(LONG_P + "Alpha.jpg"), M(LONG_P + "Bravo.jpg", T0 + 2)],
        [S(LONG_P[:46] + ".json", LONG_P + "Bravo.jpg", T0 + 2 + TZ),
         S(LONG_P[:46] + "(1).json", LONG_P + "Alpha.jpg")],
        {LONG_P + "Alpha.jpg": (LONG_P[:46] + "(1).json", "R3", "high"),
         LONG_P + "Bravo.jpg": (LONG_P[:46] + ".json", "R3", "high")},
    ),
    (
        "S5 shared truncated prefix resolved by time (titles only prefix-agree)",
        [M(LONG_P[:43] + ".jpg", T0), M(LONG_P[:44] + ".jpg", T0 + 37)],
        [S(LONG_P[:46] + ".json", LONG_P + "one.jpg", T0 + TZ),
         S(LONG_P[:46] + "(1).json", LONG_P + "two.jpg", T0 + 37 + TZ)],
        {LONG_P[:43] + ".jpg": (LONG_P[:46] + ".json", "R3", "low"),
         LONG_P[:44] + ".jpg": (LONG_P[:46] + "(1).json", "R3", "low")},
    ),
    (
        "S5 shared truncated prefix, nothing to tell them apart -> unpaired",
        [M(LONG_P[:43] + ".jpg", None), M(LONG_P[:44] + ".jpg", None)],
        [S(LONG_P[:46] + ".json", LONG_P + "one.jpg"),
         S(LONG_P[:46] + "(1).json", LONG_P + "two.jpg")],
        {LONG_P[:43] + ".jpg": NONE, LONG_P[:44] + ".jpg": NONE},
    ),
    (
        "S5 shared prefix, titles do not name either file -> unpaired",
        [M(LONG_P + "Alpha.jpg"), M(LONG_P + "Bravo.jpg")],
        [S(LONG_P[:46] + ".json", "Something else.jpg"),
         S(LONG_P[:46] + "(1).json", "Other.jpg")],
        {LONG_P + "Alpha.jpg": NONE, LONG_P + "Bravo.jpg": NONE},
    ),
    (
        "S5 a lone truncated (1) sidecar belongs to a missing NAME(1).ext, not to NAME.ext",
        [M(LONG_P + "Alpha.jpg")],
        [S(LONG_P[:46] + "(1).json", LONG_P + "Alpha.jpg")],
        {LONG_P + "Alpha.jpg": NONE},
    ),
    (
        "S5 truncated (1) with a same-titled sibling is a real duplicate name",
        [M(LONG_P + "Alpha.jpg")],
        [S(LONG_P[:46] + ".json", LONG_P + "Alpha.jpg"),
         S(LONG_P[:46] + "(1).json", LONG_P + "Alpha.jpg")],
        {LONG_P + "Alpha.jpg": (LONG_P[:46] + ".json", "R3", "high")},
    ),
    (
        "WhatsApp 'NAME (1).jpeg' is a real name: its truncated sidecar pairs by R3",
        [M(WA_A, None), M(WA_B, None)],
        [S(WA_SB, WA_B)],
        {WA_A: NONE, WA_B: (WA_SB, "R3", "high")},
    ),
    (
        "WhatsApp 'NAME (1).jpeg' missing: its sidecar is never given to NAME.jpeg",
        [M(WA_A, None)],
        [S(WA_SB, WA_B)],
        {WA_A: NONE},
    ),
    (
        "WhatsApp pair, each with its own truncated sidecar",
        [M(WA_A, None), M(WA_B, None)],
        [S(WA_SA, WA_A), S(WA_SB, WA_B)],
        {WA_A: (WA_SA, "R3", "high"), WA_B: (WA_SB, "R3", "high")},
    ),
    (
        "Windows 'Photo (1).jpg' keeps its whole name",
        [M("Photo.jpg"), M("Photo (1).jpg")],
        [S("Photo (1).jpg.supplemental-metadata.json", "Photo (1).jpg")],
        {"Photo.jpg": NONE,
         "Photo (1).jpg": ("Photo (1).jpg.supplemental-metadata.json", "R1", "high")},
    ),
    (
        "long name without its own sidecar, sibling's cut sidecar via R3, times unknown -> none",
        [M(HOL_A, None)],
        [S(trunc_json(HOL_B), HOL_B)],
        {HOL_A: NONE},
    ),
    (
        "long name, sibling's cut sidecar via R3, times not a tz offset apart -> none",
        [M(HOL_A, T0)],
        [S(trunc_json(HOL_B), HOL_B, T0 + TZ + 37)],
        {HOL_A: NONE},
    ),
    (
        "long name, sibling's sidecar via R6, times unknown -> none",
        [M(HOL_A, None)],
        [S("weird.json", HOL_B)],
        {HOL_A: NONE},
    ),
    (
        "long name, sibling's sidecar via R6, times agree exactly: low-confidence prefix match",
        [M(HOL_A, T0)],
        [S("weird.json", HOL_B, T0 + TZ)],
        {HOL_A: ("weird.json", "R6", "low")},
    ),
    (
        "prefix-only title naming a file that is present is not given to the shorter name",
        [M(HOL_A), M(HOL_B)],
        [S(HOL_B + ".json", HOL_B), S("weird.json", HOL_B)],
        {HOL_A: NONE, HOL_B: (HOL_B + ".json", "R1", "high")},
    ),
    (
        "NFD title matches an NFC member name",
        [M(CAFE_NFC)],
        [S(CAFE_NFC + ".json", CAFE_NFD)],
        {CAFE_NFC: (CAFE_NFC + ".json", "R1", "high")},
    ),
    (
        "NFD sidecar name matches an NFC member name",
        [M(CAFE_NFC)],
        [S(CAFE_NFD + ".supplemental-metadata.json", CAFE_NFD)],
        {CAFE_NFC: (CAFE_NFD + ".supplemental-metadata.json", "R1", "high")},
    ),
    (
        "S7 missing sidecar",
        [M("IMG_7.jpg"), M("IMG_8.jpg")],
        [S("IMG_8.jpg.json", "IMG_8.jpg")],
        {"IMG_7.jpg": NONE, "IMG_8.jpg": ("IMG_8.jpg.json", "R1", "high")},
    ),
    (
        "S8 -edited copy gets nothing, the original keeps its sidecar",
        [M("IMG_5-edited.jpg"), M("IMG_5.jpg")],
        [S("IMG_5.jpg.supplemental-metadata.json", "IMG_5.jpg")],
        {"IMG_5-edited.jpg": NONE,
         "IMG_5.jpg": ("IMG_5.jpg.supplemental-metadata.json", "R1", "high")},
    ),
    (
        "S8 -edited copy alone does not take the original's sidecar",
        [M("IMG_5-edited.jpg")],
        [S("IMG_5.jpg.supplemental-metadata.json", "IMG_5.jpg"),
         S("IMG_5-edited.jpg.json", "IMG_5-edited.jpg")],
        {"IMG_5-edited.jpg": NONE},
    ),
    (
        "S9 extension case differs (IMG.JPG vs title img.jpg)",
        [M("IMG_9.JPG"), M("img_10.jpg")],
        [S("IMG_9.jpg.supplemental-metadata.json", "img_9.jpg"),
         S("IMG_10.JPG.json", "IMG_10.JPG")],
        {"IMG_9.JPG": ("IMG_9.jpg.supplemental-metadata.json", "R5", "high"),
         "img_10.jpg": ("IMG_10.JPG.json", "R5", "high")},
    ),
    (
        "S9 casefold with relocated (1)",
        [M("IMG_9(1).JPG")],
        [S("IMG_9.jpg.supplemental-metadata(1).json", "IMG_9.jpg")],
        {"IMG_9(1).JPG": ("IMG_9.jpg.supplemental-metadata(1).json", "R5", "high")},
    ),
    (
        "S9 exact name beats casefold",
        [M("IMG_9.JPG")],
        [S("IMG_9.JPG.json", "IMG_9.JPG")],
        {"IMG_9.JPG": ("IMG_9.JPG.json", "R1", "high")},
    ),
    (
        "S10 extension-less JSON",
        [M("IMG_3000.jpg"), M("IMG_3001(2).jpg")],
        [S("IMG_3000.json", "IMG_3000.jpg"), S("IMG_3001(2).json", "IMG_3001.jpg")],
        {"IMG_3000.jpg": ("IMG_3000.json", "R4", "high"),
         "IMG_3001(2).jpg": ("IMG_3001(2).json", "R4", "high")},
    ),
    (
        "S10 extension-less JSON of a live photo goes to the file its title names",
        [M("IMG_3002.HEIC"), M("IMG_3002.MOV")],
        [S("IMG_3002.json", "IMG_3002.MOV")],
        {"IMG_3002.HEIC": NONE, "IMG_3002.MOV": ("IMG_3002.json", "R4", "high")},
    ),
    (
        "S10 extension-less JSON titled for the video is not given to the photo",
        [M("IMG_3003.HEIC")],
        [S("IMG_3003.json", "IMG_3003.MOV")],
        {"IMG_3003.HEIC": NONE},
    ),
    (
        "S10 extension-less JSON without a title is not given to a live photo's HEIC",
        [M("IMG_3002.HEIC")],
        [S("IMG_3002.json", None)],
        {"IMG_3002.HEIC": NONE},
    ),
    (
        "S11 title-only match",
        [M("IMG_20200101_120000.jpg")],
        [S("unrelated-name.json", "IMG_20200101_120000.jpg")],
        {"IMG_20200101_120000.jpg": ("unrelated-name.json", "R6", "high")},
    ),
    (
        "S11 title-only match on a truncated media name is low confidence",
        [M(LONG_P[:43] + ".jpg")],
        [S("weird.json", LONG_P + "full.jpg")],
        {LONG_P[:43] + ".jpg": ("weird.json", "R6", "low")},
    ),
    (
        "S11 title-only needs the same (n): IMG.jpg(1).json is never IMG.jpg's",
        [M("IMG.jpg")],
        [S("IMG.jpg(1).json", "IMG.jpg")],
        {"IMG.jpg": NONE},
    ),
    (
        "title disagrees -> unpaired",
        [M("IMG_1.jpg")],
        [S("IMG_1.jpg.json", "IMG_2.jpg")],
        {"IMG_1.jpg": NONE},
    ),
    (
        "time disagrees by more than a day + 14 h -> unpaired",
        [M("IMG_1.jpg", T0)],
        [S("IMG_1.jpg.json", "IMG_1.jpg", T0 + 86400 + 14 * 3600 + 1)],
        {"IMG_1.jpg": NONE},
    ),
    (
        "time within the slack window still pairs",
        [M("IMG_1.jpg", T0)],
        [S("IMG_1.jpg.json", "IMG_1.jpg", T0 - 86400 - 14 * 3600)],
        {"IMG_1.jpg": ("IMG_1.jpg.json", "R1", "high")},
    ),
    (
        "missing times do not block",
        [M("IMG_1.jpg", None), M("IMG_2.jpg")],
        [S("IMG_1.jpg.json", "IMG_1.jpg"), S("IMG_2.jpg.json", "IMG_2.jpg", None)],
        {"IMG_1.jpg": ("IMG_1.jpg.json", "R1", "high"),
         "IMG_2.jpg": ("IMG_2.jpg.json", "R1", "high")},
    ),
    (
        "no title: exact-name rule pairs with low confidence, title rule cannot",
        [M("IMG_1.jpg"), M("IMG_2.jpg")],
        [S("IMG_1.jpg.json", None), S("other.json", None)],
        {"IMG_1.jpg": ("IMG_1.jpg.json", "R1", "low"), "IMG_2.jpg": NONE},
    ),
    (
        "same-format extension spellings agree (title .jpeg, file .jpg)",
        [M("IMG_1.jpg")],
        [S("IMG_1.jpg.json", "IMG_1.jpeg")],
        {"IMG_1.jpg": ("IMG_1.jpg.json", "R1", "high")},
    ),
    (
        "two equally good sidecars -> ambiguous -> unpaired",
        [M("IMG_1.jpg")],
        [S("IMG_1.jpg.json", "IMG_1.jpg"),
         S("IMG_1.jpg.supplemental-metadata.json", "IMG_1.jpg")],
        {"IMG_1.jpg": NONE},
    ),
    (
        "the same media name twice (two zip parts) -> ambiguous -> unpaired",
        [M("IMG_1.jpg", key="a"), M("IMG_1.jpg", key="b")],
        [S("IMG_1.jpg.json", "IMG_1.jpg")],
        {"a": NONE, "b": NONE},
    ),
    (
        "non-sidecar JSON is never paired, even with a matching title",
        [M("IMG_1.jpg"), M("metadata")],
        [S("metadata.json", "IMG_1.jpg"), S("print-subscriptions.json", "metadata"),
         S("IMG_1.jpg.txt", "IMG_1.jpg")],
        {"IMG_1.jpg": NONE, "metadata": NONE},
    ),
    (
        "burst frames with long names each find their own truncated sidecar",
        [M("PXL_20230101_123456789.RAW-01.MP.COVER.jpg"),
         M("PXL_20230101_123456789.RAW-02.ORIGINAL.jpg", T0 + 1),
         M("PXL_20230101_123456789.jpg", T0 + 1)],
        [S(trunc_json("PXL_20230101_123456789.RAW-01.MP.COVER.jpg"),
           "PXL_20230101_123456789.RAW-01.MP.COVER.jpg"),
         S(trunc_json("PXL_20230101_123456789.RAW-02.ORIGINAL.jpg"),
           "PXL_20230101_123456789.RAW-02.ORIGINAL.jpg", T0 + 1 + TZ)],
        {"PXL_20230101_123456789.RAW-01.MP.COVER.jpg":
         (trunc_json("PXL_20230101_123456789.RAW-01.MP.COVER.jpg"), "R3", "high"),
         "PXL_20230101_123456789.RAW-02.ORIGINAL.jpg":
         (trunc_json("PXL_20230101_123456789.RAW-02.ORIGINAL.jpg"), "R3", "high"),
         "PXL_20230101_123456789.jpg": NONE},
    ),
]


@pytest.mark.parametrize("case_id, media, sidecars, expected", CASES, ids=[c[0] for c in CASES])
def test_pairing_cases(case_id, media, sidecars, expected):
    assert run(media, sidecars) == expected


def test_pairing_is_order_independent():
    for _case_id, media, sidecars, expected in CASES:
        assert run(list(reversed(media)), list(reversed(sidecars))) == expected


# ---------------------------------------------------------------------------------------------
# Scoping: cross-zip, cross-export, album vs year, trash
# ---------------------------------------------------------------------------------------------


def test_s6_cross_zip_same_export():
    # Media in part 001, sidecar in part 003: keys carry the zip, pairing ignores it.
    media = [M("IMG_6.jpg", key=("001.zip", "IMG_6.jpg"))]
    side = [S("IMG_6.jpg.supplemental-metadata.json", "IMG_6.jpg", key=("003.zip", "IMG_6.json"))]
    res = pair(media, side)
    assert res[("001.zip", "IMG_6.jpg")] == Pairing(("003.zip", "IMG_6.json"), "R1", "high")


def test_cross_export_isolation():
    media = [M("IMG_1.jpg", export=EXP), M("IMG_1.jpg", export=EXP_B), M("IMG_2.jpg", export=EXP_B)]
    side = [S("IMG_1.jpg.json", "IMG_1.jpg", export=EXP),
            S("IMG_1.jpg.json", "IMG_1.jpg", export=EXP_B),
            S("IMG_2.jpg.json", "IMG_2.jpg", export=EXP)]  # only in the other export
    res = pair(media, side)
    assert res[(EXP, YEAR, "IMG_1.jpg")].sidecar_key == (EXP, YEAR, "IMG_1.jpg.json")
    assert res[(EXP_B, YEAR, "IMG_1.jpg")].sidecar_key == (EXP_B, YEAR, "IMG_1.jpg.json")
    assert res[(EXP_B, YEAR, "IMG_2.jpg")] == NO_PAIRING


def test_album_vs_year_folder():
    album = "Beach Trip"
    media = [M("IMG_1.jpg"), M("IMG_1.jpg", folder=album), M("IMG_2.jpg")]
    side = [S("IMG_1.jpg.json", "IMG_1.jpg"), S("IMG_1.jpg.json", "IMG_1.jpg", folder=album),
            S("IMG_2.jpg.json", "IMG_2.jpg", folder=album)]  # sidecar only in the album
    res = pair(media, side)
    assert res[(EXP, YEAR, "IMG_1.jpg")].sidecar_key == (EXP, YEAR, "IMG_1.jpg.json")
    assert res[(EXP, album, "IMG_1.jpg")].sidecar_key == (EXP, album, "IMG_1.jpg.json")
    assert res[(EXP, YEAR, "IMG_2.jpg")] == NO_PAIRING


def test_trash_folder_sidecar_not_used():
    media = [M("IMG_1.jpg")]
    side = [S("IMG_1.jpg.json", "IMG_1.jpg", folder="Trash")]
    assert pair(media, side)[(EXP, YEAR, "IMG_1.jpg")] == NO_PAIRING


def test_empty_inputs():
    assert pair([], []) == {}
    assert pair([], [S("IMG_1.jpg.json", "IMG_1.jpg")]) == {}
    assert pair([M("IMG_1.jpg")], []) == {(EXP, YEAR, "IMG_1.jpg"): NO_PAIRING}


def test_large_folder_is_fast():
    n = 5000
    media = [M(f"IMG_{i:05d}.jpg", T0 + i) for i in range(n)]
    media += [M(f"{LONG_P}{i:05d}.jpg", T0 + i) for i in range(n)]
    side = [S(f"IMG_{i:05d}.jpg.supplemental-metadata.json", f"IMG_{i:05d}.jpg", T0 + i + TZ)
            for i in range(n)]
    side += [S(trunc_json(f"{LONG_P}{i:05d}.jpg", total=62), f"{LONG_P}{i:05d}.jpg", T0 + i + TZ)
             for i in range(n)]
    start = time.perf_counter()
    res = pair(media, side)
    elapsed = time.perf_counter() - start
    assert sum(1 for p in res.values() if p.rule == "R1") == n
    assert sum(1 for p in res.values() if p.rule == "R3") == n
    assert elapsed < 10.0


def test_shared_long_prefix_stays_linear():
    # Thousands of files sharing one long prefix: every cut sidecar stem prefixes every name,
    # so candidates must come from the title, not from the prefix.
    n = 3000
    pre = "Very long shared prefix for many files abcdefgh"
    assert len(pre) == 47
    media = [M(f"{pre}_{i:05d}.jpg", T0 + i * 60) for i in range(n)]
    side = [S(pre[:46] + (f"({i})" if i else "") + ".json", f"{pre}_{i:05d}.jpg",
              T0 + i * 60 + TZ) for i in range(n)]
    start = time.perf_counter()
    res = pair(media, side)
    elapsed = time.perf_counter() - start
    assert all(res[m.key].rule == "R3" for m in media)
    assert all(res[m.key].sidecar_key == sc.key for m, sc in zip(media, side))
    assert elapsed < 5.0
