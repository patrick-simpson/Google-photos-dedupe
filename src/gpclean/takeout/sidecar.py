"""Takeout JSON sidecars: parsing, url validation, and pairing sidecars with media files.

Why pairing is careful: the sidecar carries the item's ``url``, and the review site sends the
user to that url to delete the photo. Pairing a photo with *another* photo's sidecar would send
the user to delete the wrong item. So every rule below only assigns a pair when it is unique on
both sides and the sidecar's ``title`` and time agree with the media file; anything doubtful is
left unpaired (the site then falls back to a search link).

Takeout naming facts this encodes (verified Sept 2026, see docs/PLAN.md):
- ``NAME.ext.supplemental-metadata.json`` (since ~Oct 2024) or the older ``NAME.ext.json``.
- The whole JSON name is capped at roughly 46-51 characters. Longer names are cut from the end
  of ``.supplemental-metadata`` (``.supplemental-metada.json``, ``.suppl.json``, ``.s.json``),
  then into the media name itself (old exports: ``<first 46 chars>.json``).
- A duplicate index sits before the extension on media (``NAME(1).jpg``) but after the whole
  name on its sidecar (``NAME.jpg(1).json``, ``NAME.jpg.supplemental-metadata(1).json``).
- Some sidecars drop the media extension (``IMG_3000.json``); case can differ (``IMG.JPG`` vs
  a title of ``img.jpg``); ``-edited`` copies have no sidecar; some media have none at all.
- Media and JSON can land in different zip parts, so pairing is per (export_id, folder).
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from gpclean.takeout.names import NON_SIDECAR_JSON, is_edited, parse_media_name

log = logging.getLogger(__name__)

RAW_MAX_BYTES = 64 * 1024
# Free text from a sidecar is shown on the review site and can be written by other people
# (shared albums), so each field is capped; ``raw`` keeps more of the original for debugging.
DESCRIPTION_MAX_BYTES = 4096
SHORT_TEXT_MAX_BYTES = 1024  # title, origin folder, device type
PEOPLE_MAX = 200
PERSON_NAME_MAX_BYTES = 200
# Epoch seconds outside +-10**12 (~31,700 years) are nonsense, and larger ints would not even
# fit SQLite's 64-bit INTEGER column.
_TS_LIMIT = 10**12

# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------

_PHOTOS_URL_RE = re.compile(
    r"https://photos\.google\.com/photo/(?P<id>[A-Za-z0-9_-]{10,200})/?(?:[?#][!-~]*)?"
)


def validate_photos_url(url: str | None) -> str | None:
    """Return the canonical ``https://photos.google.com/photo/<id>`` for a sidecar url, or None.

    The url ends up as a clickable link on the review site, and sidecar text can be written by
    other people (shared albums), so we accept exactly one shape and *rebuild* the url from the
    id instead of passing the original string through. A trailing slash, query or fragment is
    dropped; whitespace, other hosts, ports, userinfo or http:// are rejected.
    """
    if not isinstance(url, str) or len(url) > 2048:
        return None
    m = _PHOTOS_URL_RE.fullmatch(url)
    if not m:
        return None
    return "https://photos.google.com/photo/" + m.group("id")


def _get(obj, *keys):
    """Walk nested dicts; ``None`` as soon as a level is missing or is not a dict."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _text(value) -> str | None:
    """Non-empty string or None (Takeout writes "" for absent descriptions)."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _ts(value) -> int | None:
    """Takeout timestamps are decimal strings of epoch seconds. ``0`` means "unknown"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        ts = int(value)
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,12}", value.strip()):
        ts = int(value.strip())
    else:
        return None
    if not -_TS_LIMIT < ts < _TS_LIMIT:
        return None
    return ts or None


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _geo(block) -> tuple[float | None, float | None, float | None]:
    """(lat, lon, alt) from a geoData block. Takeout writes 0.0/0.0 for "no location"."""
    lat, lon, alt = _num(_get(block, "latitude")), _num(_get(block, "longitude")), _num(
        _get(block, "altitude")
    )
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return None, None, None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None, None, None
    return lat, lon, alt


def _capped(value, limit: int) -> str | None:
    """``_text`` then cut to ``limit`` UTF-8 bytes."""
    text = _text(value)
    return _truncate_utf8(text, limit) if text is not None else None


def _flag(value) -> int:
    return 1 if value is True else 0


def _truncate_utf8(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    # errors="ignore" drops a multi-byte character cut in half at the limit.
    return data[:limit].decode("utf-8", errors="ignore")


_SIDECAR_FIELDS = (
    "title", "taken_ts", "creation_ts", "lat", "lon", "alt", "lat_exif", "lon_exif", "url",
    "description", "people", "origin_folder", "device_type", "from_shared_album", "from_partner",
    "favorited", "archived", "trashed", "raw", "err",
)


def parse_sidecar(data: bytes) -> dict:
    """Parse one sidecar JSON into the ``sidecars_raw`` columns (minus the member fields).

    Never raises: undecodable or non-object JSON gives ``{"err": "<ExceptionClass>"}`` with
    every other column None. Unknown or oddly typed fields are ignored rather than trusted.
    """
    try:
        text = data.decode("utf-8-sig")  # tolerate a BOM
        doc = json.loads(text)
        if not isinstance(doc, dict):
            raise TypeError("sidecar JSON is not an object")
        return _fields(doc, text)
    except Exception as exc:  # noqa: BLE001 - by contract: never raise on bad input
        out = dict.fromkeys(_SIDECAR_FIELDS)
        out["err"] = type(exc).__name__
        return out


def _fields(doc: dict, text: str) -> dict:
    lat, lon, alt = _geo(doc.get("geoData"))
    lat_exif, lon_exif, _alt_exif = _geo(doc.get("geoDataExif"))
    people = doc.get("people")
    names = []
    if isinstance(people, list):
        names = [_truncate_utf8(p["name"], PERSON_NAME_MAX_BYTES)
                 for p in people if isinstance(p, dict) and _text(p.get("name"))][:PEOPLE_MAX]
    origin = doc.get("googlePhotosOrigin")
    origin = origin if isinstance(origin, dict) else {}
    title = _text(doc.get("title"))
    return {
        "title": _truncate_utf8(title.strip(), SHORT_TEXT_MAX_BYTES) if title else None,
        "taken_ts": _ts(_get(doc, "photoTakenTime", "timestamp")),
        "creation_ts": _ts(_get(doc, "creationTime", "timestamp")),
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "lat_exif": lat_exif,
        "lon_exif": lon_exif,
        "url": validate_photos_url(doc.get("url")),
        "description": _capped(doc.get("description"), DESCRIPTION_MAX_BYTES),
        "people": json.dumps(names, ensure_ascii=False) if names else None,
        "origin_folder": _capped(_get(origin, "mobileUpload", "deviceFolder", "localFolderName"),
                                 SHORT_TEXT_MAX_BYTES),
        "device_type": _capped(_get(origin, "mobileUpload", "deviceType"), SHORT_TEXT_MAX_BYTES),
        # Presence of these keys (their value is usually {}) marks items other people shared;
        # deleting those affects them too, so the site badges them.
        "from_shared_album": 1 if "fromSharedAlbum" in origin else 0,
        "from_partner": 1 if "fromPartnerSharing" in origin else 0,
        "favorited": _flag(doc.get("favorited")),
        "archived": _flag(doc.get("archived")),
        "trashed": _flag(doc.get("trashed")),
        "raw": _truncate_utf8(text, RAW_MAX_BYTES),
        "err": None,
    }


# ---------------------------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaRef:
    """A media member to pair; ``key`` is whatever the caller uses to identify it."""

    key: object  # opaque caller key
    export_id: str
    folder: str
    filename: str
    exif_ts: int | None = None  # UTC-ish epoch from EXIF DateTimeOriginal (for confirmation)


@dataclass(frozen=True)
class SidecarRef:
    """A parsed sidecar to pair."""

    key: object
    export_id: str
    folder: str
    json_name: str
    title: str | None
    taken_ts: int | None


@dataclass(frozen=True)
class Pairing:
    """Which sidecar (if any) belongs to a media item, by which rule, and how sure we are."""

    sidecar_key: object | None
    rule: str  # "R1".."R6" or "none"
    conf: str  # "high" | "low" | "none"


NO_PAIRING = Pairing(None, "none", "none")

SUPP = ".supplemental-metadata"
# Names at least this long (UTF-8 bytes) may have been cut by Takeout's length limit; shorter
# ones never are, so prefix matching is only tried on long names.
TRUNC_MIN_BYTES = 40
# A truncated prefix must still be this long to be worth matching on.
PREFIX_MIN = 20
# EXIF DateTimeOriginal is local wall-clock time read as UTC, photoTakenTime is real UTC, so a
# correct pair differs by the photo's UTC offset (at most 14 h). The accepted window adds a day
# of slack for cameras with a wrong clock or time zone.
TZ_SLACK_S = 14 * 3600
TIME_WINDOW_S = 86400 + TZ_SLACK_S

# Extensions that are the same format under two spellings (title "a.jpeg", file "a.jpg").
_EXT_FAMILY = {"jpeg": "jpg", "heif": "heic", "tiff": "tif"}

# Same shape as names._DUP_RE: Takeout's "(n)" never follows a space.
_JSON_DUP_RE = re.compile(r"^(?P<stem>.*\S)\((?P<n>\d{1,6})\)$")


def _nfc(text: str) -> str:
    """Unicode NFC form. Names from iOS/macOS often arrive decomposed (NFD: "e" + combining
    accent) while the zip member name is composed, so both sides are normalised before any
    comparison."""
    return text if text.isascii() else unicodedata.normalize("NFC", text)


def _name_key(name: str) -> tuple[str, str]:
    """(stem, ext) casefolded with same-format extensions unified, for title comparisons."""
    mn = parse_media_name(_nfc(name.strip()))
    stem = (mn.base + (f"({mn.dup})" if mn.dup is not None else "")).casefold()
    ext = mn.ext[1:].casefold()
    return stem, _EXT_FAMILY.get(ext, ext)


def _title_cf(title: str | None) -> str:
    return _nfc(title.strip()).casefold() if title else ""


def _long(name: str) -> bool:
    return len(name.encode("utf-8")) >= TRUNC_MIN_BYTES


@dataclass
class _Media:
    """Precomputed name forms of one media file (L = literal name, F = name without "(n)")."""

    ref: MediaRef
    dup: int | None
    lit: str  # L
    plain: str  # F
    base: str  # F without extension
    lit_key: tuple[str, str]
    plain_key: tuple[str, str]
    truncated: bool  # long enough that Takeout may have cut the media name itself

    @classmethod
    def of(cls, ref: MediaRef) -> "_Media":
        filename = _nfc(ref.filename)
        mn = parse_media_name(filename)
        return cls(
            ref=ref,
            dup=mn.dup,
            lit=filename,
            plain=mn.plain,
            base=mn.base,
            lit_key=_name_key(filename),
            plain_key=_name_key(mn.plain),
            truncated=_long(filename) and len(mn.base) >= PREFIX_MIN,
        )


@dataclass
class _Sidecar:
    """Precomputed forms of one sidecar name: ``<stem>[(dup)].json``."""

    ref: SidecarRef
    name: str  # json name in NFC
    title: str | None  # stripped NFC title, None when absent or blank
    name_cf: str
    stem_cf: str  # name without ".json" and without the trailing "(n)"
    dup: int | None
    long: bool
    # True when this "(n)" sidecar looks like a collision rename: a sibling with the same stem
    # and no "(n)" exists and carries a *different* title (set by _Group).
    collision: bool = False

    @classmethod
    def of(cls, ref: SidecarRef) -> "_Sidecar | None":
        name = _nfc(ref.json_name)
        if not name.casefold().endswith(".json") or name.casefold() in NON_SIDECAR_JSON:
            return None
        core = name[: -len(".json")]
        m = _JSON_DUP_RE.match(core)
        stem, dup = (m.group("stem"), int(m.group("n"))) if m else (core, None)
        title = _nfc(ref.title.strip()) if ref.title and ref.title.strip() else None
        return cls(ref=ref, name=name, title=title, name_cf=name.casefold(),
                   stem_cf=stem.casefold(), dup=dup, long=_long(name))


def _dup_str(dup: int | None) -> str:
    return f"({dup})" if dup is not None else ""


def _title_rank(title: str, m: _Media) -> int | None:
    """0 = title names this file, 1 = this (truncated) file name is a prefix of the title,
    None = the title belongs to something else."""
    stem, ext = _name_key(title)
    if (stem, ext) in (m.lit_key, m.plain_key):
        return 0
    # Takeout may have cut a long media name; the title keeps the full original name. The
    # extension must still agree, or IMG_1.HEIC could take the sidecar of its live-photo IMG_1.MOV.
    if m.truncated and ext == m.plain_key[1] and stem.startswith(m.base.casefold()):
        return 1
    return None


def _confirm(
    rule: str, m: _Media, s: _Sidecar, media_keys: set[tuple[str, str]]
) -> tuple[tuple[int, int, int], str] | None:
    """Check a candidate pair. Returns (quality, conf) with lower quality = better, or None.

    ``media_keys`` holds the name keys of every media file in the group. Quality is
    (title, dup, time) rank; it only breaks ties between candidates of one rule.
    """
    title = s.title
    if title:
        title_rank = _title_rank(title, m)
        if title_rank is None:
            return None
        if title_rank == 1 and _name_key(title) in media_keys:
            # The title names a file that is in this folder, so the sidecar is that file's,
            # not this shorter-named one's.
            return None
    elif rule in ("R3", "R4", "R6"):
        # Prefix and title rules have nothing else to lean on. R4 matches the name without its
        # extension, which a live photo's HEIC and MOV share, so only the title can tell.
        return None
    else:
        title_rank = 2  # whole-name match, but no title to double-check it

    time_rank = 2  # unknown time: allowed, but weakest
    if s.ref.taken_ts is not None and m.ref.exif_ts is not None:
        dt = abs(s.ref.taken_ts - m.ref.exif_ts)
        if dt > TIME_WINDOW_S:
            return None
        if dt <= TZ_SLACK_S:
            # A whole-quarter-hour difference is exactly a time-zone offset: the strongest sign
            # that both clocks describe the same shot.
            off = dt % 900
            time_rank = 0 if off <= 1 or off >= 899 else 1
    if title_rank == 1 and time_rank != 0:
        # A title prefix alone also fits a sibling with a longer name (NAME.jpg vs NAME_2.jpg)
        # whose media is missing here. The url is the item's identity, so a prefix match is only
        # believed when both clocks are known and differ by an exact time-zone offset.
        return None

    # Same "(n)" on both sides is the normal case. Only R3 generates mismatches on purpose
    # (collision-renamed sidecars), so this rank mostly matters there.
    dup_rank = 0 if s.dup == m.dup else 1

    if title_rank == 0 or (title_rank == 1 and rule not in ("R3", "R6")):
        conf = "high"
    else:
        conf = "low"
    return (title_rank, dup_rank, time_rank), conf


class _Group:
    """Pairing state for one (export_id, folder)."""

    def __init__(self, media: list[_Media], sidecars: list[_Sidecar]):
        self.media = media
        self.sidecars = sidecars
        self.claimed_m: dict[int, tuple[int, str, str]] = {}  # media idx -> (sidecar idx, rule, conf)
        self.claimed_s: set[int] = set()

        self.by_name: dict[str, list[int]] = defaultdict(list)  # exact json name
        self.by_name_cf: dict[str, list[int]] = defaultdict(list)
        plain_titles: dict[str, set[str]] = defaultdict(set)  # stem -> titles of "(n)"-less
        for i, s in enumerate(sidecars):
            self.by_name[s.name].append(i)
            self.by_name_cf[s.name_cf].append(i)
            if s.dup is None:
                plain_titles[s.stem_cf].add(_title_cf(s.title))
        for s in sidecars:
            # Two long names cut to the same prefix make Takeout add "(1)" to the second
            # sidecar although its media has no "(1)". That is only believable when the
            # "(n)"-less sibling exists and describes a different file; otherwise "(1)" means
            # a genuine duplicate name and belongs to a NAME(1).ext media file.
            if s.dup is not None and s.long:
                own = _title_cf(s.title)
                others = plain_titles.get(s.stem_cf, set())
                s.collision = bool(own) and any(t and t != own for t in others)

        # Media by title key (for title-first candidate lookups), the bases of media names that
        # may themselves have been cut (for "title / stem extends base"), and every name key in
        # the folder (a prefix-only title match must not name a file that is present).
        self.by_cut_base: dict[str, list[int]] = defaultdict(list)
        self.by_title_key: dict[tuple[str, str], list[int]] = defaultdict(list)
        self.media_keys: set[tuple[str, str]] = set()
        for i, m in enumerate(media):
            self.media_keys.update((m.lit_key, m.plain_key))
            self.by_title_key[m.lit_key].append(i)
            if m.plain_key != m.lit_key:
                self.by_title_key[m.plain_key].append(i)
            if m.truncated:
                self.by_cut_base[m.base.casefold()].append(i)

    def _cut_bases_of(self, text: str) -> list[int]:
        """Media whose (possibly truncated) base is a prefix of ``text``, at least PREFIX_MIN long."""
        out: list[int] = []
        for n in range(PREFIX_MIN, len(text) + 1):
            out.extend(self.by_cut_base.get(text[:n], ()))
        return out

    # -- candidate generators: yield (media idx, sidecar idx) among unclaimed items ----------

    def _free_media(self):
        return (i for i in range(len(self.media)) if i not in self.claimed_m)

    def _lookup(self, index: dict[str, list[int]], names) -> list[int]:
        out: list[int] = []
        for n in names:
            out.extend(j for j in index.get(n, ()) if j not in self.claimed_s)
        return out

    def _r1_names(self, m: _Media) -> list[str]:
        return [m.lit + SUPP + ".json", m.lit + ".json"]

    def _r2_names(self, m: _Media) -> list[str]:
        if m.dup is None:
            return []
        d = _dup_str(m.dup)
        return [m.plain + SUPP + d + ".json", m.plain + d + ".json"]

    def cand_r1(self):
        for i in self._free_media():
            for j in self._lookup(self.by_name, self._r1_names(self.media[i])):
                yield i, j

    def cand_r2(self):
        for i in self._free_media():
            for j in self._lookup(self.by_name, self._r2_names(self.media[i])):
                yield i, j

    def _titled_as(self, s: _Sidecar) -> set[int]:
        """Media the sidecar's title can confirm: named by it, or a cut base that prefixes it.

        Looking candidates up by title first keeps the rules linear: a folder of thousands of
        files sharing one long prefix would otherwise give every truncated sidecar every file.
        """
        if not s.title:
            return set()
        key = _name_key(s.title)
        cands = set(self.by_title_key.get(key, ()))
        cands.update(self._cut_bases_of(key[0]))
        return cands

    def cand_r3(self):
        for j, s in enumerate(self.sidecars):
            if j in self.claimed_s or not s.long or len(s.stem_cf) < PREFIX_MIN:
                continue
            for i in sorted(self._titled_as(s)):
                if i in self.claimed_m:
                    continue
                m = self.media[i]
                # Either the sidecar name was cut (its stem is a prefix of
                # "F.supplemental-metadata") or the media name was cut too (the stem extends the
                # media's truncated base).
                cut_sidecar = (m.plain.casefold() + SUPP).startswith(s.stem_cf)
                cut_media = m.truncated and s.stem_cf.startswith(m.base.casefold())
                if not (cut_sidecar or cut_media):
                    continue
                # Same "(n)", or a sidecar that got an "(n)" only because two long names were
                # cut to the same prefix (the media itself has none).
                if s.dup == m.dup or (m.dup is None and s.collision):
                    yield i, j

    def cand_r4(self):
        for i in self._free_media():
            m = self.media[i]
            if m.plain == m.base:
                continue  # no extension to drop; R1 already covered this name
            d = _dup_str(m.dup)
            names = [(m.base + d + ".json").casefold(), (m.base + SUPP + d + ".json").casefold()]
            for j in self._lookup(self.by_name_cf, names):
                yield i, j

    def cand_r5(self):
        for i in self._free_media():
            m = self.media[i]
            names = [n.casefold() for n in self._r1_names(m) + self._r2_names(m)]
            for j in self._lookup(self.by_name_cf, names):
                yield i, j

    def cand_r6(self):
        for j, s in enumerate(self.sidecars):
            if j in self.claimed_s:
                continue
            for i in sorted(self._titled_as(s)):
                # Strict "(n)" equality: "IMG.jpg(1).json" titled "IMG.jpg" belongs to IMG(1).jpg
                # even when that file is missing, never to IMG.jpg.
                if i not in self.claimed_m and self.media[i].dup == s.dup:
                    yield i, j

    # -- assignment ----------------------------------------------------------------------------

    def assign(self, rule: str, candidates) -> int:
        """Confirm candidates, then keep pairs that are the unique best on both sides.

        Repeats until nothing changes: once a pair is claimed its items drop out, which can make
        a remaining candidate unique "among still unclaimed items".
        """
        edges: dict[tuple[int, int], tuple[tuple[int, int, int], str]] = {}
        for i, j in candidates:
            if (i, j) not in edges:
                checked = _confirm(rule, self.media[i], self.sidecars[j], self.media_keys)
                if checked is not None:
                    edges[(i, j)] = checked

        total = 0
        while edges:
            by_m: dict[int, list[tuple[tuple[int, int, int], int]]] = defaultdict(list)
            by_s: dict[int, list[tuple[tuple[int, int, int], int]]] = defaultdict(list)
            for (i, j), (q, _conf) in edges.items():
                by_m[i].append((q, j))
                by_s[j].append((q, i))
            accepted = [
                (i, j, conf)
                for (i, j), (_q, conf) in edges.items()
                if _unique_best(by_m[i], j) and _unique_best(by_s[j], i)
            ]
            if not accepted:
                break
            for i, j, conf in accepted:
                self.claimed_m[i] = (j, rule, conf)
                self.claimed_s.add(j)
            total += len(accepted)
            edges = {
                (i, j): v for (i, j), v in edges.items()
                if i not in self.claimed_m and j not in self.claimed_s
            }
        return total


def _unique_best(options: list[tuple[tuple[int, int, int], int]], want: int) -> bool:
    """True when ``want`` is the only option with the best (lowest) quality."""
    best = min(q for q, _ in options)
    return [x for q, x in options if q == best] == [want]


_RULES = (
    ("R1", _Group.cand_r1),
    ("R2", _Group.cand_r2),
    ("R3", _Group.cand_r3),
    ("R4", _Group.cand_r4),
    ("R5", _Group.cand_r5),
    ("R6", _Group.cand_r6),
)


def pair(media: list[MediaRef], sidecars: list[SidecarRef]) -> dict[object, Pairing]:
    """Pair media files with their sidecars. Every media key appears in the result.

    Pairing is done per (export_id, folder) across all zip parts. Rules R1..R6 run in order;
    each assigns only pairs that are unique on both sides among still-unclaimed items after
    confirmation (title agrees, and photoTakenTime within a day + 14 h of EXIF when both are
    known). When one rule offers several confirmed candidates, a candidate wins only if it is
    strictly better on title, then "(n)", then time agreement, from *both* sides' point of
    view; otherwise the items stay for later rules or end up unpaired.

    Callers must pass *all* media of a folder, videos included: a live photo's MOV and HEIC
    share a name stem, and a MOV left out would leave its sidecar for the photo to compete for.
    Names and titles are compared in Unicode NFC.
    """
    result: dict[object, Pairing] = {}
    groups_m: dict[tuple[str, str], list[_Media]] = defaultdict(list)
    groups_s: dict[tuple[str, str], list[_Sidecar]] = defaultdict(list)
    for ref in media:
        result[ref.key] = NO_PAIRING
        # Edited copies never own a sidecar; letting them compete could steal the original's.
        if not is_edited(ref.filename):
            groups_m[(ref.export_id, ref.folder)].append(_Media.of(ref))
    for sref in sidecars:
        s = _Sidecar.of(sref)
        if s is not None:
            groups_s[(sref.export_id, sref.folder)].append(s)

    counts: dict[str, int] = defaultdict(int)
    for gkey, gmedia in groups_m.items():
        gsidecars = groups_s.get(gkey)
        if not gsidecars:
            continue
        group = _Group(gmedia, gsidecars)
        for rule, gen in _RULES:
            counts[rule] += group.assign(rule, gen(group))
        for i, (j, rule, conf) in group.claimed_m.items():
            result[gmedia[i].ref.key] = Pairing(gsidecars[j].ref.key, rule, conf)

    # Counts only: names are personal data and must not reach the logs.
    log.debug(
        "sidecar pairing: media=%d sidecars=%d %s unpaired=%d",
        len(media), len(sidecars),
        " ".join(f"{r}={counts[r]}" for r, _ in _RULES),
        sum(1 for p in result.values() if p.sidecar_key is None),
    )
    return result
