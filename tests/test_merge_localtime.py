"""Tests for gpclean.merge.localtime: EXIF parsing, local day rules, DST, videos."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gpclean.merge.localtime import (
    exif_epoch,
    local_fields,
    parse_exif_dt,
    parse_offset,
    subsec_fraction,
    video_day,
    wall_seconds,
)

NY = "America/New_York"


def _utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def _fields(**kw):
    base = {"exif_dt": None, "exif_offset": None, "exif_dt_any": None, "sidecar_ts": None,
            "folder_year": None, "tz_name": NY}
    return local_fields(**{**base, **kw})


@pytest.mark.parametrize("text, want", [
    ("2021:07:16 11:00:00", datetime(2021, 7, 16, 11, 0, 0)),
    ("2021-07-16 11:00:00", datetime(2021, 7, 16, 11, 0, 0)),
    ("2021:07:16T11:00:00+02:00", datetime(2021, 7, 16, 11, 0, 0)),
    ("0000:00:00 00:00:00", None),
    ("1985:01:01 00:00:00", None),  # before 1990: a reset clock
    ("2021:13:01 00:00:00", None),
    ("2021:02:30 00:00:00", None),
    ("    ", None),
    (None, None),
    ("garbage", None),
])
def test_parse_exif_dt(text, want):
    assert parse_exif_dt(text) == want


def test_parse_exif_dt_rejects_far_future_but_accepts_next_year():
    next_year = datetime.now(UTC).year + 1
    assert parse_exif_dt(f"{next_year}:01:01 00:00:00") is not None
    assert parse_exif_dt(f"{next_year + 1}:01:01 00:00:00") is None


@pytest.mark.parametrize("text, minutes", [
    ("-04:00", -240), ("+05:30", 330), ("+0100", 60), ("+00:00", 0),
    ("+15:00", None), ("-04:60", None), ("x", None), (None, None), ("", None),
])
def test_parse_offset(text, minutes):
    got = parse_offset(text)
    assert (got is None) if minutes is None else got == timedelta(minutes=minutes)


def test_subsec_fraction():
    assert subsec_fraction("123") == pytest.approx(0.123)
    assert subsec_fraction("5") == pytest.approx(0.5)
    assert subsec_fraction("050") == pytest.approx(0.05)
    assert subsec_fraction(None) is None
    assert subsec_fraction("abc") is None


def test_wall_seconds_ignores_zones():
    a = parse_exif_dt("2022:01:01 10:00:00")
    b = parse_exif_dt("2022:01:01 10:00:07")
    assert wall_seconds(b) - wall_seconds(a) == 7


def test_exif_epoch_with_offset_and_in_fallback_zone():
    # With an explicit offset.
    assert exif_epoch("2022:07:01 12:00:00", "-04:00", NY) == _utc(2022, 7, 1, 16)
    # Without: New York wall clock, summer (-4) and winter (-5).
    assert exif_epoch("2022:07:01 12:00:00", None, NY) == _utc(2022, 7, 1, 16)
    assert exif_epoch("2022:01:01 12:00:00", None, NY) == _utc(2022, 1, 1, 17)
    assert exif_epoch(None, None, NY) is None


def test_exif_date_wins_and_is_certain():
    f = _fields(exif_dt="2022:07:03 23:30:00", sidecar_ts=_utc(2022, 7, 4, 3, 30))
    assert f["local_date"] == "2022-07-03" and f["local_time"] == "23:30:00"
    assert f["day_uncertain"] == 0 and f["year"] == 2022
    # taken_ts prefers the sidecar's real instant.
    assert f["taken_src"] == "sidecar" and f["taken_ts"] == _utc(2022, 7, 4, 3, 30)


def test_taken_ts_from_exif_with_offset_then_local():
    f = _fields(exif_dt="2022:07:03 23:30:00", exif_offset="+02:00")
    assert f["taken_src"] == "exif_offset" and f["taken_ts"] == _utc(2022, 7, 3, 21, 30)
    f = _fields(exif_dt="2022:07:03 23:30:00")
    assert f["taken_src"] == "exif_local" and f["taken_ts"] == _utc(2022, 7, 4, 3, 30)


def test_sidecar_only_converts_in_fallback_zone_and_is_uncertain():
    # 02:30 UTC on Jul 4 is the evening of Jul 3 in New York.
    f = _fields(sidecar_ts=_utc(2022, 7, 4, 2, 30), folder_year=2022)
    assert f["local_date"] == "2022-07-03" and f["local_time"] == "22:30:00"
    assert f["day_uncertain"] == 1 and f["taken_src"] == "sidecar"


def test_dst_change_night():
    # 04:30 UTC on 2023-03-12 is still 23:30 EST on Mar 11 (a naive -4 h would say Mar 12).
    f = _fields(sidecar_ts=_utc(2023, 3, 12, 4, 30))
    assert f["local_date"] == "2023-03-11" and f["local_time"] == "23:30:00"
    # And after the change, 12:00 UTC is 08:00 EDT.
    f = _fields(sidecar_ts=_utc(2023, 3, 12, 12, 0))
    assert f["local_time"] == "08:00:00"


def test_offset_without_datetime_original_pins_the_day():
    f = _fields(exif_offset="+09:00", sidecar_ts=_utc(2022, 7, 3, 20, 0))
    assert f["local_date"] == "2022-07-04" and f["day_uncertain"] == 0


def test_exif_datetime_fallback_and_unknown():
    f = _fields(exif_dt="0000:00:00 00:00:00", exif_dt_any="2019:05:05 10:00:00")
    assert f["local_date"] == "2019-05-05" and f["day_uncertain"] == 1
    assert f["taken_src"] == "exif_any"
    f = _fields(folder_year=2018)
    assert f["local_date"] is None and f["local_time"] is None
    assert f["year"] == 2018 and f["taken_src"] == "none" and f["taken_ts"] is None


def test_video_day():
    assert video_day(_utc(2023, 3, 12, 4, 30), NY) == "2023-03-11"
    assert video_day(None, NY) is None
    assert video_day(_utc(2023, 3, 12, 4, 30), "UTC") == "2023-03-12"


def test_pre_1970_sidecar_time_is_dated_on_every_platform():
    """datetime.fromtimestamp rejects negative epochs on Windows; old scans must still get
    their day (1960-01-01 00:00 UTC is the evening of Dec 31 in New York)."""
    ts = -315619200
    assert ts == _utc(1960, 1, 1)
    f = _fields(sidecar_ts=ts, folder_year=1960)
    assert f["local_date"] == "1959-12-31" and f["local_time"] == "19:00:00"
    assert f["year"] == 1959 and f["day_uncertain"] == 1 and f["taken_ts"] == ts
    assert video_day(ts, NY) == "1959-12-31"
    pinned = _fields(sidecar_ts=ts, exif_offset="+01:00")
    assert pinned["local_date"] == "1960-01-01" and pinned["day_uncertain"] == 0
    assert _fields(sidecar_ts=10**18)["local_date"] is None  # out of range: undated
