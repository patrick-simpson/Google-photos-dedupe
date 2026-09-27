"""Filename knowledge about Google Takeout exports and the phones/apps that made the photos.

Everything here is pure string work on member *names* (never on file contents), so it is cheap
and easy to test. The patterns encode Takeout behaviour verified in Sept 2026 (see
docs/PLAN.md, "Research findings"); where Google's behaviour is fuzzy we err on the side of
*not* matching, because a wrong match is worse than a missed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PHOTO_EXTS: frozenset[str] = frozenset({"jpg", "jpeg", "png", "heic", "heif", "webp", "gif"})
VIDEO_EXTS: frozenset[str] = frozenset(
    {"mp4", "mov", "3gp", "m4v", "mkv", "avi", "webm", "mts", "m2ts", "wmv", "mpg", "mpeg", "mp"}
)
RAW_EXTS: frozenset[str] = frozenset(
    {"dng", "cr2", "cr3", "nef", "arw", "rw2", "orf", "raf", "srw", "pef"}
)
OTHER_IMAGE_EXTS: frozenset[str] = frozenset({"bmp", "tif", "tiff", "avif", "jxl"})

# JSON files that Takeout writes next to photos but that describe albums / the export itself,
# not a single media item. Compared against the casefolded basename.
NON_SIDECAR_JSON: frozenset[str] = frozenset(
    {
        "metadata.json",
        "print-subscriptions.json",
        "shared_album_comments.json",
        "user-generated-memory-titles.json",
    }
)

# A trailing "(N)" that Takeout appends to make names unique inside a folder. Takeout writes
# it directly after the name ("NAME(1).jpg"); a "(1)" after a space ("Photo (1).jpg", WhatsApp's
# "... AM (1).jpeg") was made by another program and is part of the real name, so a non-space
# character must precede it. Greedy base: "photo(1)(2)" -> base "photo(1)", index 2.
_DUP_RE = re.compile(r"^(?P<base>.*\S)\((?P<n>\d{1,6})\)$")
# Extensions are short and alphanumeric; anything else after the last dot ("10.00 AM") is part
# of the name, not an extension.
_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


@dataclass(frozen=True)
class MediaName:
    """A media filename split into ``base`` + optional ``(dup)`` + ``ext``."""

    base: str  # name without dup index and without extension
    dup: int | None  # N from a trailing "(N)" before the extension
    ext: str  # extension including the dot, original case ("" if none)

    @property
    def plain(self) -> str:
        """The filename without its ``(N)`` duplicate index, e.g. ``IMG(1).jpg`` -> ``IMG.jpg``."""
        return self.base + self.ext


def _split_ext(filename: str) -> tuple[str, str]:
    """Split ``name.ext`` into (``name``, ``.ext``); ("name", "") when there is no extension."""
    stem, dot, tail = filename.rpartition(".")
    if not dot or not stem or not _EXT_RE.match(tail):
        return filename, ""
    return stem, "." + tail


def parse_media_name(filename: str) -> MediaName:
    """Parse a media basename such as ``IMG_1234(2).JPG`` into its parts.

    The ``(N)`` is only recognised directly before the extension, which is where Takeout puts
    it on media files (``NAME(1).jpg``), and only without a space before it (``Photo (1).jpg``
    is a real name). A name that is *only* ``(N)`` keeps it as its base.
    """
    stem, ext = _split_ext(filename)
    m = _DUP_RE.match(stem)
    if m:
        return MediaName(base=m.group("base"), dup=int(m.group("n")), ext=ext)
    return MediaName(base=stem, dup=None, ext=ext)


def ext_of(filename: str) -> str:
    """Lowercase extension without the dot (``"IMG.JPG"`` -> ``"jpg"``), ``""`` if none."""
    return _split_ext(filename)[1][1:].lower()


# "-edited" is what Google Photos appends to the edited copy of a photo; the edited copy never
# has its own sidecar and is not the library item, so we skip it. Localised exports use the
# UI language's word; the common ones are listed (casefolded).
_EDITED_WORDS = ("edited", "bearbeitet", "modifié", "editado", "modificato", "bewerkt", "redigerad")
# When the whole name hits Takeout's length limit the suffix itself can be cut ("-edi"). A cut
# name sits *at* that limit, so the truncated forms are only believed on names whose length
# (without the "(N)") is in the window where Takeout's cut lands: roughly 47-51 bytes plus room
# for a longer extension. The exact limit is unverified (docs/PLAN.md, open questions); outside
# the window a name ending in "-edit" is somebody's real file name and must still be scanned.
_TRUNC_MIN_BYTES = 47
_TRUNC_MAX_BYTES = 56
_EDITED_TRUNC = ("-edi", "-edit", "-edite")


def is_edited(filename: str) -> bool:
    """True for Google's edited copies: ``IMG-edited.jpg``, ``IMG-edited(1).jpg``, and the
    truncated form (``VERY_LONG_NAME-edi.jpg``) on names whose length is at Takeout's cut."""
    mn = parse_media_name(filename)
    stem = mn.base.casefold()
    if any(stem.endswith("-" + w) for w in _EDITED_WORDS):
        return True
    if _TRUNC_MIN_BYTES <= len(mn.plain.encode("utf-8")) <= _TRUNC_MAX_BYTES:
        return any(stem.endswith(t) for t in _EDITED_TRUNC)
    return False


# Part numbers are zero-padded to 3 digits and simply grow past 999 on very large exports.
_EXPORT_RE = re.compile(r"^(?P<id>.+?)-\d{3,}\.zip$", re.IGNORECASE)


def export_id(zip_name: str) -> str:
    """Identify which Takeout export a zip part belongs to.

    ``takeout-20260901T120000Z-001.zip`` -> ``takeout-20260901T120000Z``: every part of one
    export shares the timestamp, and the ``-NNN`` (or longer) part number is dropped. Other names fall back
    to their stem so a hand-made zip is its own export. Never returns ``""``.
    """
    name = zip_name.replace("\\", "/").rsplit("/", 1)[-1]
    m = _EXPORT_RE.match(name)
    if m:
        return m.group("id")
    stem = name[:-4] if name.lower().endswith(".zip") else name
    return stem or "default"


_YEAR_FOLDER_RE = re.compile(r"^Photos from (\d{4})$")


def year_of_folder(folder: str) -> int | None:
    """``"Photos from 2019"`` -> ``2019``; any other folder (an album) -> ``None``."""
    m = _YEAR_FOLDER_RE.match(folder)
    return int(m.group(1)) if m else None


# Filenames that messaging apps give to images they save. Anchored at the start so that a
# camera photo which merely *contains* such text is not flagged.
_MESSAGING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("whatsapp", re.compile(r"^(?:IMG|VID)-\d{8}-WA\d{4}", re.IGNORECASE)),
    ("whatsapp", re.compile(r"^WhatsApp (?:Image|Video) \d{4}-\d{2}-\d{2}", re.IGNORECASE)),
    ("messenger", re.compile(r"^received_\d{6,}", re.IGNORECASE)),
    ("facebook", re.compile(r"^FB_IMG_\d{6,}", re.IGNORECASE)),
    ("signal", re.compile(r"^signal-\d{4}-\d{2}-\d{2}", re.IGNORECASE)),
    ("telegram", re.compile(r"^photo_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", re.IGNORECASE)),
    ("snapchat", re.compile(r"^Snapchat-\d{4,}", re.IGNORECASE)),
    ("reddit", re.compile(r"^RDT_\d{8}_", re.IGNORECASE)),
)


def messaging_app(filename: str) -> str | None:
    """Name of the messaging app whose save-file naming ``filename`` follows, else ``None``."""
    for app, rx in _MESSAGING_PATTERNS:
        if rx.match(filename):
            return app
    return None


_SCREENSHOT_RE = re.compile(
    r"^(?:"
    r"screenshot[_ (-]"  # Android "Screenshot_20200101-120000_App", Windows "Screenshot (3)"
    r"|screenshot \d{4}-\d{2}-\d{2} at "  # macOS "Screenshot 2020-01-01 at 10.00.00"
    r"|screen shot "  # older macOS "Screen Shot 2019-01-01 at ..."
    r"|screencapture"  # macOS/Chrome "screencapture-example-com-2020-01-01.png"
    r")",
    re.IGNORECASE,
)


def is_screenshot_name(filename: str) -> bool:
    """True when the filename follows a screenshot tool's naming scheme."""
    return bool(_SCREENSHOT_RE.match(filename))


# Old Google Camera / many Android OEMs: IMG_20200101_120000_BURST001_COVER.jpg, and the
# "00000IMG_00000_BURST20191104123456789_COVER.jpg" variant where the BURST number is the
# shared burst timestamp and the leading counter differs per frame.
_BURST_RE = re.compile(
    r"^(?P<prefix>.+?)_BURST(?P<num>\d{3,})(?P<cover>_COVER)?(?:\.[A-Za-z0-9]+)?$",
    re.IGNORECASE,
)
# Pixel: PXL_20230101_123456789.RAW-01.MP.COVER.jpg, PXL_20230101_123456789.RAW-02.ORIGINAL.dng
_PXL_RAW_RE = re.compile(
    r"^(?P<prefix>PXL_\d{8}_\d{6,9})\.RAW-\d{2}\.(?P<rest>.+)$",
    re.IGNORECASE,
)


def burst_key(filename: str) -> tuple[str, bool] | None:
    """Group key for camera burst frames: ``(shared prefix, is_cover)`` or ``None``.

    Frames of one burst return the same key, so the merge can gather them without looking at
    pixels. Keys are only meaningful within one folder of one export.
    """
    # Takeout's "(N)" de-duplication index is not part of the camera's name.
    name = parse_media_name(filename).plain
    m = _PXL_RAW_RE.match(name)
    if m:
        parts = m.group("rest").upper().split(".")
        return m.group("prefix"), "COVER" in parts
    m = _BURST_RE.match(name)
    if m:
        cover = m.group("cover") is not None
        num = m.group("num")
        if len(num) >= 14:
            # Timestamp-style burst id: the per-frame prefix differs, the id is what is shared.
            return "BURST" + num, cover
        return m.group("prefix"), cover
    return None
