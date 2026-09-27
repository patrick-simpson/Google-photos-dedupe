"""Capture time, local calendar day and time-of-day for every item (PLAN section 5, "Local date").

Two different clocks are involved and they must not be mixed up:

- EXIF ``DateTimeOriginal`` is the camera's *wall clock* (local time, no zone unless
  ``OffsetTimeOriginal`` is present). It is what Google Photos shows as the photo's date, so it
  is the first choice for ``local_date`` / ``local_time``.
- The sidecar's ``photoTakenTime`` is a real UTC instant. Turning it into a calendar day needs a
  time zone; we only know the owner's fallback zone (America/New_York by default), so a day
  derived that way is marked ``day_uncertain``.

``taken_ts`` (a UTC epoch used for sorting, bursts and the keeper's "earlier taken" rule)
prefers the sidecar instant, then EXIF with its offset, then EXIF read in the fallback zone.
"""

from __future__ import annotations

import functools
import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# EXIF writes "YYYY:MM:DD HH:MM:SS"; some tools use "-" or "/" in the date or a "T"
# separator, and a few append junk (fractions, zones). Only the leading part is trusted.
_EXIF_DT_RE = re.compile(
    r"^\s*(\d{4})[:\-/](\d{2})[:\-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)
_OFFSET_RE = re.compile(r"^\s*([+-])(\d{2}):?(\d{2})\s*$")
_SUBSEC_RE = re.compile(r"^\s*(\d{1,9})")

MIN_YEAR = 1990  # older EXIF dates are almost always a reset camera clock


@functools.lru_cache(maxsize=8)
def zone(name: str) -> ZoneInfo:
    """Cached ``ZoneInfo`` (tzdata ships the database on Windows)."""
    return ZoneInfo(name)


def _max_year() -> int:
    return datetime.now(UTC).year + 1


def parse_exif_dt(text: str | None) -> datetime | None:
    """Naive wall-clock datetime from an EXIF date string, or None when absent or implausible.

    Plausible means a real calendar date with a year in 1990..(this year + 1): camera clocks
    that were never set report 1970 or 2000-01-01-ish values far less often than they report
    "0000:00:00 00:00:00", and both must not become a local date.
    """
    if not text:
        return None
    m = _EXIF_DT_RE.match(text)
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()))
    except ValueError:  # month 13, "0000:00:00", Feb 30 ...
        return None
    if not (MIN_YEAR <= dt.year <= _max_year()):
        return None
    return dt


def parse_offset(text: str | None) -> timedelta | None:
    """``"-04:00"`` / ``"+0530"`` -> timedelta; None when absent or out of the +-14 h range."""
    if not text:
        return None
    m = _OFFSET_RE.match(text)
    if not m:
        return None
    sign, hh, mm = m.groups()
    minutes = int(hh) * 60 + int(mm)
    if int(mm) >= 60 or minutes > 14 * 60:
        return None
    return timedelta(minutes=-minutes if sign == "-" else minutes)


def subsec_fraction(text: str | None) -> float | None:
    """EXIF SubSecTime digits -> fraction of a second (``"123"`` -> 0.123); None if absent."""
    if not text:
        return None
    m = _SUBSEC_RE.match(text)
    if not m:
        return None
    digits = m.group(1)
    return int(digits) / (10 ** len(digits))


def wall_seconds(dt: datetime) -> int:
    """Seconds since 1970-01-01 of a naive wall-clock datetime, *as if* it were UTC.

    Only used to compare two EXIF clocks with each other (capture-time rule), where the zone
    cancels out.
    """
    return int((dt - datetime(1970, 1, 1)).total_seconds())


def exif_epoch(exif_dt: str | None, exif_offset: str | None, tz_name: str) -> int | None:
    """UTC epoch of EXIF DateTimeOriginal: with its offset when known, else read in ``tz_name``."""
    dt = parse_exif_dt(exif_dt)
    if dt is None:
        return None
    off = parse_offset(exif_offset)
    if off is not None:
        return int(dt.replace(tzinfo=timezone(off)).timestamp())
    return int(dt.replace(tzinfo=zone(tz_name)).timestamp())


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _from_epoch(ts: int, tz) -> datetime | None:
    """``ts`` as an aware datetime in ``tz``, computed by date arithmetic.

    ``datetime.fromtimestamp`` rejects negative timestamps on Windows (OSError), which would
    leave pre-1970 scans undated there but dated on Linux; local and CI merges must agree.
    """
    try:
        return (_EPOCH + timedelta(seconds=int(ts))).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return None


def utc_to_local(ts: int, tz_name: str) -> datetime | None:
    """A UTC epoch as a wall-clock datetime in ``tz_name`` (None if out of range)."""
    return _from_epoch(ts, zone(tz_name))


def _utc_plus(ts: int, off: timedelta) -> datetime | None:
    return _from_epoch(ts, timezone(off))


def local_fields(
    *,
    exif_dt: str | None,
    exif_offset: str | None,
    exif_dt_any: str | None,
    sidecar_ts: int | None,
    folder_year: int | None,
    tz_name: str,
) -> dict:
    """Compute ``taken_ts, taken_src, local_date, local_time, day_uncertain, year``.

    Local date/time, in order: EXIF DateTimeOriginal (the wall clock Google Photos shows;
    certain), then ``photoTakenTime`` converted with the EXIF offset when one is known
    (certain) or to ``tz_name`` (uncertain: the real zone is unknown), then EXIF DateTime (a modification time on some devices; uncertain), else
    unknown. ``year`` falls back to the "Photos from YYYY" folder when there is no date.
    """
    dto = parse_exif_dt(exif_dt)
    off = parse_offset(exif_offset)
    any_dt = parse_exif_dt(exif_dt_any)

    # taken_ts: the best UTC instant we have.
    if sidecar_ts is not None:
        taken_ts, taken_src = int(sidecar_ts), "sidecar"
    elif dto is not None and off is not None:
        taken_ts, taken_src = int(dto.replace(tzinfo=timezone(off)).timestamp()), "exif_offset"
    elif dto is not None:
        taken_ts, taken_src = int(dto.replace(tzinfo=zone(tz_name)).timestamp()), "exif_local"
    elif any_dt is not None:
        taken_ts, taken_src = int(any_dt.replace(tzinfo=zone(tz_name)).timestamp()), "exif_any"
    else:
        taken_ts, taken_src = None, "none"

    local: datetime | None
    if dto is not None:
        local, uncertain = dto, 0
    elif sidecar_ts is not None and off is not None:
        # An offset without a usable DateTimeOriginal is rare, but it pins the zone exactly.
        local, uncertain = _utc_plus(int(sidecar_ts), off), 0
    elif sidecar_ts is not None:
        local, uncertain = utc_to_local(int(sidecar_ts), tz_name), 1
    elif any_dt is not None:
        local, uncertain = any_dt, 1
    else:
        local, uncertain = None, 1

    if local is None:
        return {"taken_ts": taken_ts, "taken_src": taken_src, "local_date": None,
                "local_time": None, "day_uncertain": 1, "year": folder_year}
    return {
        "taken_ts": taken_ts,
        "taken_src": taken_src,
        "local_date": local.strftime("%Y-%m-%d"),
        "local_time": local.strftime("%H:%M:%S"),
        "day_uncertain": uncertain,
        "year": local.year,
    }


def video_day(taken_ts: int | None, tz_name: str) -> str | None:
    """Local day of a video from its sidecar ``photoTakenTime`` (videos carry no EXIF here)."""
    if taken_ts is None:
        return None
    local = utc_to_local(int(taken_ts), tz_name)
    return local.strftime("%Y-%m-%d") if local is not None else None
