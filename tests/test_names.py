"""Table-driven tests for gpclean.takeout.names (synthetic filenames only)."""

from __future__ import annotations

import pytest

from gpclean.takeout.names import (
    NON_SIDECAR_JSON,
    PHOTO_EXTS,
    MediaName,
    burst_key,
    export_id,
    ext_of,
    is_edited,
    is_screenshot_name,
    messaging_app,
    parse_media_name,
    year_of_folder,
)

LONG = "Screenshot_20200101-120000_Some Long App Name"  # 45 chars


@pytest.mark.parametrize(
    "filename, base, dup, ext",
    [
        ("IMG_1234.jpg", "IMG_1234", None, ".jpg"),
        ("IMG_1234(1).jpg", "IMG_1234", 1, ".jpg"),
        ("IMG_1234(12).JPG", "IMG_1234", 12, ".JPG"),
        ("PXL_20230101_123456789.MP.jpg", "PXL_20230101_123456789.MP", None, ".jpg"),
        ("photo(1)(2).png", "photo(1)", 2, ".png"),
        ("IMG_3000", "IMG_3000", None, ""),
        ("IMG_3000(3)", "IMG_3000", 3, ""),
        ("(1).jpg", "(1)", None, ".jpg"),  # a name that is only "(1)" keeps it
        (".hidden", ".hidden", None, ""),
        ("Screenshot 2020-01-01 at 10.00 AM", "Screenshot 2020-01-01 at 10.00 AM", None, ""),
        ("café(1).heic", "café", 1, ".heic"),
        # A "(n)" after a space was written by another program and is part of the real name.
        ("Photo (1).jpg", "Photo (1)", None, ".jpg"),
        ("WhatsApp Image 2021-03-04 at 10.11.12 AM (1).jpeg",
         "WhatsApp Image 2021-03-04 at 10.11.12 AM (1)", None, ".jpeg"),
        ("Photo (1)(2).jpg", "Photo (1)", 2, ".jpg"),
        (" (1).jpg", " (1)", None, ".jpg"),
    ],
)
def test_parse_media_name(filename, base, dup, ext):
    mn = parse_media_name(filename)
    assert mn == MediaName(base, dup, ext)
    assert mn.plain == base + ext


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("IMG.JPG", "jpg"),
        ("a.b.HeIc", "heic"),
        ("noext", ""),
        ("IMG.jpg.supplemental-metadata.json", "json"),
        ("", ""),
    ],
)
def test_ext_of(filename, expected):
    assert ext_of(filename) == expected


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("IMG_1234-edited.jpg", True),
        ("IMG_1234-EDITED.JPG", True),
        ("IMG_1234-edited(1).jpg", True),
        ("IMG_1234-edited(12).png", True),
        ("IMG_1234-bearbeitet.jpg", True),
        ("VID_1234-edited.mp4", True),
        # Truncated suffix on a name whose length is at Takeout's cut.
        (LONG + "-edi.jpg", True),
        (LONG + "-edit(1).jpg", True),
        # ...but not on shorter names, where Takeout never truncates...
        ("trip-edi.jpg", False),
        ("trip-edit.jpg", False),
        ("Family_reunion_newsletter_2020_final-edit.jpg", False),  # 45 bytes: a real name
        # ...nor on names longer than the cut, which cannot have been cut there.
        ("x" * 60 + "-edit.jpg", False),
        ("IMG_1234.jpg", False),
        ("edited.jpg", False),
        ("IMG-edited-final.jpg", False),
        ("IMG_edited.jpg", False),
    ],
)
def test_is_edited(filename, expected):
    assert is_edited(filename) is expected


@pytest.mark.parametrize(
    "zip_name, expected",
    [
        ("takeout-20260901T120000Z-001.zip", "takeout-20260901T120000Z"),
        ("takeout-20260901T120000Z-123.ZIP", "takeout-20260901T120000Z"),
        ("some/dir/takeout-20260901T120000Z-002.zip", "takeout-20260901T120000Z"),
        ("C:\\x\\takeout-20260901T120000Z-002.zip", "takeout-20260901T120000Z"),
        ("takeout-20260901T120000Z-1000.zip", "takeout-20260901T120000Z"),  # 1000+ parts
        ("my-photos.zip", "my-photos"),
        ("archive", "archive"),
        (".zip", "default"),
        ("", "default"),
    ],
)
def test_export_id(zip_name, expected):
    assert export_id(zip_name) == expected


@pytest.mark.parametrize(
    "folder, expected",
    [
        ("Photos from 2019", 2019),
        ("Photos from 1999", 1999),
        ("Photos from 19", None),
        ("photos from 2019", None),
        ("Photos from 2019 trip", None),
        ("Summer 2019", None),
        ("Trash", None),
    ],
)
def test_year_of_folder(folder, expected):
    assert year_of_folder(folder) == expected


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("IMG-20200101-WA0001.jpg", "whatsapp"),
        ("img-20200101-wa0012(1).jpeg", "whatsapp"),
        ("VID-20200101-WA0003.mp4", "whatsapp"),
        ("WhatsApp Image 2023-05-01 at 10.11.12.jpeg", "whatsapp"),
        ("received_1234567890123456.jpeg", "messenger"),
        ("FB_IMG_1577880000000.jpg", "facebook"),
        ("signal-2021-03-04-120000.jpg", "signal"),
        ("signal-2021-03-04-12-00-00-123.jpg", "signal"),
        ("photo_2022-01-02_03-04-05.jpg", "telegram"),
        ("Snapchat-1234567890.jpg", "snapchat"),
        ("RDT_20220101_1200001234567890.jpg", "reddit"),
        # Camera names and look-alikes are not messaging images.
        ("IMG_20200101_120000.jpg", None),
        ("IMG-20200101-0001.jpg", None),
        ("my_received_photo.jpg", None),
        ("photo_of_me.jpg", None),
        ("PXL_20230101_123456789.jpg", None),
    ],
)
def test_messaging_app(filename, expected):
    assert messaging_app(filename) == expected


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("Screenshot_20200101-120000.png", True),
        ("Screenshot_20200101-120000_Chrome.jpg", True),
        ("Screenshot_2019-01-01-12-00-00.png", True),
        ("Screenshot 2023-05-01 at 10.11.12.png", True),
        ("Screen Shot 2019-01-01 at 1.02.03 PM.png", True),
        ("screencapture-example-com-2020-01-01-10_00_00.png", True),
        ("Screenshot (12).png", True),
        ("IMG_1234.PNG", False),
        ("my screenshot.png", False),
        ("Screenshots.png", False),
    ],
)
def test_is_screenshot_name(filename, expected):
    assert is_screenshot_name(filename) is expected


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("IMG_20200101_120000_BURST001_COVER.jpg", ("IMG_20200101_120000", True)),
        ("IMG_20200101_120000_BURST002.jpg", ("IMG_20200101_120000", False)),
        ("IMG_20200101_120000_BURST002(1).jpg", ("IMG_20200101_120000", False)),
        ("00000IMG_00000_BURST20191104123456789_COVER.jpg", ("BURST20191104123456789", True)),
        ("00001IMG_00001_BURST20191104123456789.jpg", ("BURST20191104123456789", False)),
        ("PXL_20230101_123456789.RAW-01.MP.COVER.jpg", ("PXL_20230101_123456789", True)),
        ("PXL_20230101_123456789.RAW-01.COVER.jpg", ("PXL_20230101_123456789", True)),
        ("PXL_20230101_123456789.RAW-02.ORIGINAL.dng", ("PXL_20230101_123456789", False)),
        ("pxl_20230101_123456789.raw-02.mp.jpg", ("pxl_20230101_123456789", False)),
        ("PXL_20230101_123456789.RAW-01.MP.COVER(1).jpg", ("PXL_20230101_123456789", True)),
        ("PXL_20230101_123456789.jpg", None),
        ("PXL_20230101_123456789.MP.jpg", None),
        ("BURST001.jpg", None),
        ("IMG_1234.jpg", None),
    ],
)
def test_burst_key(filename, expected):
    assert burst_key(filename) == expected


def test_constants():
    assert "jpg" in PHOTO_EXTS and "dng" not in PHOTO_EXTS
    assert all(n == n.casefold() for n in NON_SIDECAR_JSON)
