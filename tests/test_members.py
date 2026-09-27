"""Tests for gpclean.takeout.members (synthetic zips and names only)."""

from __future__ import annotations

import io
import zipfile

import pytest

from gpclean.takeout.members import Entry, MemberClass, classify, list_entries

Y = "Takeout/Google Photos/Photos from 2019/"
A = "Takeout/Google Photos/Beach Trip/"
MB = 1024 * 1024


@pytest.mark.parametrize(
    "name, include_albums, size, kind, folder, folder_kind, reason",
    [
        # images
        (Y + "IMG_1.jpg", False, 10, "image", "Photos from 2019", "year", None),
        (Y + "IMG_1.HEIC", False, 10, "image", "Photos from 2019", "year", None),
        (A + "IMG_1.jpg", True, 10, "image", "Beach Trip", "album", None),
        (A + "IMG_1.jpg", False, 10, "skip", "Beach Trip", "album", "album_media"),
        (Y + "IMG_1.jpg", False, 300 * MB, "skip", "Photos from 2019", "year", "too_large"),
        # edited copies win over "is a photo", and are skipped for video too
        (Y + "IMG_1-edited.jpg", False, 10, "skip", "Photos from 2019", "year", "edited"),
        (Y + "IMG_1-edited(1).jpg", False, 10, "skip", "Photos from 2019", "year", "edited"),
        (A + "IMG_1-edited.jpg", True, 10, "skip", "Beach Trip", "album", "edited"),
        (Y + "VID_1-edited.mp4", False, 10, "skip", "Photos from 2019", "year", "edited"),
        # RAW and other images
        (Y + "IMG_1.dng", False, 10, "skip", "Photos from 2019", "year", "raw"),
        (Y + "IMG_1.CR2", False, 10, "skip", "Photos from 2019", "year", "raw"),
        (Y + "scan.tiff", False, 10, "skip", "Photos from 2019", "year", "other_image"),
        (Y + "pic.bmp", False, 10, "skip", "Photos from 2019", "year", "other_image"),
        # videos
        (Y + "VID_1.mp4", False, 10, "video", "Photos from 2019", "year", None),
        (Y + "IMG_1.MOV", False, 10, "video", "Photos from 2019", "year", None),
        (Y + "VID_1.mp4", False, 5000 * MB, "video", "Photos from 2019", "year", None),
        (A + "VID_1.mp4", True, 10, "skip", "Beach Trip", "album", "album_video"),
        # sidecars, in year and album folders (albums always: membership by url)
        (Y + "IMG_1.jpg.supplemental-metadata.json", False, 10, "sidecar", "Photos from 2019",
         "year", None),
        (Y + "IMG_1.jpg.json", False, 10, "sidecar", "Photos from 2019", "year", None),
        (A + "IMG_1.jpg.JSON", False, 10, "sidecar", "Beach Trip", "album", None),
        (Y + "VID_1.mp4.json", False, 10, "sidecar", "Photos from 2019", "year", None),
        # non-sidecar JSON
        (A + "metadata.json", True, 10, "skip", "Beach Trip", "album", "non_sidecar_json"),
        (A + "Metadata.JSON", True, 10, "skip", "Beach Trip", "album", "non_sidecar_json"),
        (A + "shared_album_comments.json", True, 10, "skip", "Beach Trip", "album",
         "non_sidecar_json"),
        ("Takeout/Google Photos/print-subscriptions.json", False, 10, "skip", None, None,
         "non_sidecar_json"),
        ("Takeout/Google Photos/user-generated-memory-titles.json", False, 10, "skip", None, None,
         "non_sidecar_json"),
        # trash, any case, any content
        ("Takeout/Google Photos/Trash/IMG_1.jpg", True, 10, "skip", "Trash", None, "trash"),
        ("Takeout/Google Photos/bin/IMG_1.jpg.json", True, 10, "skip", "bin", None, "trash"),
        ("Takeout/Google Photos/TRASH/VID.mp4", True, 10, "skip", "TRASH", None, "trash"),
        # layout
        ("Takeout/Google Photos/Photos from 2019/", False, 0, "skip", None, None, "directory"),
        ("Takeout/Google Photos/", False, 0, "skip", None, None, "directory"),
        ("Takeout/YouTube/video.mp4", False, 10, "skip", None, None, "outside_photos"),
        ("Takeout/archive_browser.html", False, 10, "skip", None, None, "outside_photos"),
        ("Google Photos/Photos from 2019/IMG_1.jpg", False, 10, "skip", None, None,
         "outside_photos"),
        ("Takeout/Google Photos/Album/sub/IMG_1.jpg", True, 10, "skip", None, None, "other"),
        ("Takeout/Google Photos/stray.jpg", True, 10, "skip", None, None, "other"),
        (Y + "notes.txt", False, 10, "skip", "Photos from 2019", "year", "other"),
        (Y + "noext", False, 10, "skip", "Photos from 2019", "year", "other"),
        # an album that merely looks like a year folder is still an album
        ("Takeout/Google Photos/Photos from 2019 trip/IMG_1.jpg", False, 10, "skip",
         "Photos from 2019 trip", "album", "album_media"),
        # backslash separators (zip written on Windows)
        ("Takeout\\Google Photos\\Photos from 2020\\IMG_2.jpg", False, 10, "image",
         "Photos from 2020", "year", None),
    ],
)
def test_classify(name, include_albums, size, kind, folder, folder_kind, reason):
    mc = classify(name, include_albums=include_albums, file_size=size)
    assert mc.kind == kind
    assert mc.folder == folder
    assert mc.reason == reason
    assert mc.folder_kind == folder_kind
    if kind != "skip":
        assert mc.filename == name.replace("\\", "/").rsplit("/", 1)[-1]


def test_classify_every_reason_is_in_schema():
    from gpclean.schema import SHARD_DDL

    reasons = {
        classify(n, include_albums=False, file_size=s).reason
        for n, s in [
            (Y + "a-edited.jpg", 1), (Y + "a.dng", 1), (Y + "a.bmp", 1), (Y + "a.jpg", 10**12),
            ("Takeout/Google Photos/Trash/a.jpg", 1), (A + "metadata.json", 1), (A + "a.jpg", 1),
            (A + "a.mp4", 1), ("x/a.jpg", 1), ("Takeout/Google Photos/x/", 0), (Y + "a.txt", 1),
        ]
    }
    assert len(reasons) == 11
    for r in reasons:
        assert r in SHARD_DDL


def test_classify_custom_max_bytes():
    assert classify(Y + "a.jpg", include_albums=False, file_size=11, max_member_bytes=10).reason \
        == "too_large"
    assert classify(Y + "a.jpg", include_albums=False, file_size=10, max_member_bytes=10).kind \
        == "image"


def test_classify_returns_dataclass():
    mc = classify(Y + "IMG_1.jpg", include_albums=False)
    assert mc == MemberClass("image", "Photos from 2019", "year", "IMG_1.jpg", None)


def test_list_entries_sorted_by_offset():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(Y + "b.jpg", b"b" * 100, compress_type=zipfile.ZIP_STORED)
        zf.writestr(Y + "a.jpg.json", b'{"title": "a.jpg"}' * 20,
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr(zipfile.ZipInfo(Y), b"")  # directory entry
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        # Shuffle the central directory order so sorting by offset is observable.
        zf.filelist.reverse()
        entries = list_entries(zf)
        infos = zf.infolist()
    assert [e.header_offset for e in entries] == sorted(e.header_offset for e in entries)
    assert [e.name for e in entries] == [Y + "b.jpg", Y + "a.jpg.json", Y]
    for e in entries:
        assert isinstance(e, Entry)
        info = infos[e.member_idx]
        assert info.filename == e.name
        assert (e.file_size, e.compress_size, e.crc, e.compress_type) == (
            info.file_size, info.compress_size, info.CRC, info.compress_type)
    assert [e.is_dir for e in entries] == [False, False, True]
    assert entries[0].compress_type == zipfile.ZIP_STORED
    assert entries[1].compress_type == zipfile.ZIP_DEFLATED
    assert entries[1].compress_size < entries[1].file_size
