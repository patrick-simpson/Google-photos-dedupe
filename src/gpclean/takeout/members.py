"""Read a Takeout zip's central directory and decide what each member is.

Classification is by *name only*: nothing here reads member bytes, and member names are never
used as filesystem paths (they are untrusted text from the archive).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass

from gpclean.takeout.names import (
    NON_SIDECAR_JSON,
    OTHER_IMAGE_EXTS,
    PHOTO_EXTS,
    RAW_EXTS,
    VIDEO_EXTS,
    ext_of,
    is_edited,
    year_of_folder,
)

_PHOTOS_ROOT = ("Takeout", "Google Photos")
_TRASH_FOLDERS = frozenset({"trash", "bin"})


@dataclass(frozen=True)
class Entry:
    """One central-directory record, reduced to what the scanner needs."""

    member_idx: int  # index in ZipFile.infolist()
    name: str
    header_offset: int
    compress_size: int
    file_size: int
    crc: int
    compress_type: int
    is_dir: bool


@dataclass(frozen=True)
class MemberClass:
    """What a zip member is, and where it sits in the Takeout layout."""

    kind: str  # "image" | "sidecar" | "video" | "skip"
    folder: str | None  # folder directly under "Google Photos/"
    folder_kind: str | None  # "year" | "album"
    filename: str | None
    reason: str | None  # for kind == "skip": see skipped_raw.reason in schema.py


def list_entries(zf: zipfile.ZipFile) -> list[Entry]:
    """All central-directory records of ``zf``, sorted by local-header offset.

    Offset order is the order the bytes sit in the file, which lets range reads stream through
    the archive front to back instead of seeking around.
    """
    entries = [
        Entry(
            member_idx=i,
            name=info.filename,
            header_offset=info.header_offset,
            compress_size=info.compress_size,
            file_size=info.file_size,
            crc=info.CRC,
            compress_type=info.compress_type,
            is_dir=info.is_dir(),
        )
        for i, info in enumerate(zf.infolist())
    ]
    entries.sort(key=lambda e: (e.header_offset, e.member_idx))
    return entries


def _skip(reason: str, folder=None, folder_kind=None, filename=None) -> MemberClass:
    return MemberClass("skip", folder, folder_kind, filename, reason)


def classify(
    name: str,
    *,
    include_albums: bool,
    file_size: int = 0,
    max_member_bytes: int = 200 * 1024 * 1024,
) -> MemberClass:
    """Classify one zip member by its name (and uncompressed size).

    Layout: ``Takeout/Google Photos/<folder>/<filename>``, where ``<folder>`` is a year folder
    (``Photos from 2019``) or an album. See docs/INTERFACES.md for the full rule list; the
    order of checks below matters (e.g. ``-edited`` wins over "is a photo").
    """
    # Windows-made zips may use backslashes; Takeout itself uses "/".
    norm = name.replace("\\", "/")
    if norm.endswith("/"):
        return _skip("directory")
    parts = norm.split("/")
    if tuple(parts[:2]) != _PHOTOS_ROOT:
        return _skip("outside_photos")
    if len(parts) == 3:
        # Files directly in "Google Photos/" (e.g. user-generated-memory-titles.json).
        filename = parts[2]
        if ext_of(filename) == "json":
            return _skip("non_sidecar_json", filename=filename)
        return _skip("other", filename=filename)
    if len(parts) != 4 or not parts[2] or not parts[3]:
        return _skip("other")

    folder, filename = parts[2], parts[3]
    if folder.casefold() in _TRASH_FOLDERS:
        return _skip("trash", folder=folder, filename=filename)
    # With exactly four path parts this is the year-folder rule
    # ^Takeout/Google Photos/Photos from (\d{4})/[^/]+$ from docs/INTERFACES.md.
    is_year = year_of_folder(folder) is not None
    folder_kind = "year" if is_year else "album"
    where = {"folder": folder, "folder_kind": folder_kind, "filename": filename}

    ext = ext_of(filename)
    if ext == "json":
        if filename.casefold() in NON_SIDECAR_JSON:
            return _skip("non_sidecar_json", **where)
        # Album sidecars are always read: they are tiny and give album membership by url.
        return MemberClass("sidecar", folder, folder_kind, filename, None)

    if ext in PHOTO_EXTS or ext in VIDEO_EXTS or ext in OTHER_IMAGE_EXTS:
        if is_edited(filename):
            return _skip("edited", **where)
    if ext in RAW_EXTS:
        return _skip("raw", **where)
    if ext in OTHER_IMAGE_EXTS:
        return _skip("other_image", **where)
    if ext in PHOTO_EXTS:
        if not is_year and not include_albums:
            return _skip("album_media", **where)
        if file_size > max_member_bytes:
            return _skip("too_large", **where)
        return MemberClass("image", folder, folder_kind, filename, None)
    if ext in VIDEO_EXTS:
        if not is_year:
            return _skip("album_video", **where)
        return MemberClass("video", folder, folder_kind, filename, None)
    return _skip("other", **where)
