"""Tests for gpclean.merge.bursts: filename bursts, time bursts, best shot, burst_extra."""

from __future__ import annotations

from gpclean.config import MergeConfig
from gpclean.merge.bursts import burst_extras, find_bursts

CFG = MergeConfig()
EA = "takeout-20260101T000000Z"


def row(k: int, filename: str, *, ts: int | None = 1_600_000_000, subsec: str | None = None,
        phash: int = 0, model: str | None = "FP-7", lap: float = 100.0, dark: float = 0.0,
        bright: float = 0.0, w: int = 4000, h: int = 3000, folder: str = "Photos from 2022",
        export: str = EA) -> dict:
    return {"item_id": k + 1, "item_uid": f"u{k:03d}", "filename": filename, "folder": folder,
            "export_id": export, "taken_ts": ts, "exif_subsec": subsec, "phash64": phash,
            "model": model, "lap_var": lap, "frac_dark": dark, "frac_bright": bright,
            "width": w, "height": h}


def _members(bursts):
    return [(b.source, b.members) for b in bursts]


def test_filename_burst_best_is_sharpest_well_exposed_frame():
    rows = [
        row(0, "IMG_20220101_101010_BURST000_COVER.jpg", lap=80),
        row(1, "IMG_20220101_101010_BURST001.jpg", lap=300, dark=0.7),  # sharp but black
        row(2, "IMG_20220101_101010_BURST002.jpg", lap=120),
        row(3, "IMG_20220101_101010.jpg", ts=1_700_000_000),
    ]
    bursts = find_bursts(rows, [False] * 4, CFG)
    assert _members(bursts) == [("filename", [2, 1, 0])]  # best first, then by sharpness
    assert bursts[0].best == 2


def test_best_falls_back_to_sharpest_when_every_frame_is_badly_exposed():
    rows = [row(0, "A_BURST001.jpg", lap=50, bright=0.5), row(1, "A_BURST002.jpg", lap=90,
                                                               bright=0.3)]
    assert find_bursts(rows, [False, False], CFG)[0].best == 1


def test_ties_break_on_resolution():
    rows = [row(0, "A_BURST001.jpg", lap=90, w=2000, h=1500),
            row(1, "A_BURST002.jpg", lap=90)]
    assert find_bursts(rows, [False, False], CFG)[0].best == 1


def test_filename_bursts_are_per_folder():
    rows = [row(0, "A_BURST001.jpg"), row(1, "A_BURST002.jpg", folder="Photos from 2023",
                                          ts=1_700_000_000)]
    assert find_bursts(rows, [False] * 2, CFG) == []


def test_filename_burst_spans_exports_after_collapse():
    """A newer export holding only some frames makes their primaries come from two exports;
    it is still one burst with one best frame."""
    rows = [row(0, "A_BURST001.jpg", lap=50), row(1, "A_BURST002.jpg", lap=90),
            row(2, "A_BURST003.jpg", export="takeout-20260201T000000Z", lap=70)]
    assert _members(find_bursts(rows, [False] * 3, CFG)) == [("filename", [1, 2, 0])]


def test_time_burst_chains_consecutive_frames():
    t = 1_650_000_000
    rows = [
        row(0, "IMG_1.jpg", ts=t, subsec="120", phash=0, lap=200),
        row(1, "IMG_2.jpg", ts=t + 1, subsec="127", phash=0b111, lap=50),
        row(2, "IMG_3.jpg", ts=t + 2, subsec="134", phash=0b111111, lap=10),
        row(3, "IMG_4.jpg", ts=t + 10, phash=0b111111),  # too late
        row(4, "IMG_5.jpg", ts=t + 11, phash=-1),  # far pHash from IMG_4
    ]
    bursts = find_bursts(rows, [False] * 5, CFG)
    assert _members(bursts) == [("time", [0, 1, 2])]
    assert abs(bursts[0].duration_s - 2.014) < 1e-6
    assert bursts[0].start_ts == t and bursts[0].end_ts == t + 2


def test_time_burst_needs_same_camera_model_when_known():
    t = 1_650_000_000
    rows = [row(0, "a.jpg", ts=t, model="FP-7"), row(1, "b.jpg", ts=t + 1, model="EX-100")]
    assert find_bursts(rows, [False, False], CFG) == []
    rows[1]["model"] = None
    assert len(find_bursts(rows, [False, False], CFG)) == 1


def test_time_bursts_skip_graphic_items_dup_copies_and_filename_bursts():
    t = 1_650_000_000
    rows = [
        row(0, "a.jpg", ts=t),
        row(1, "b.jpg", ts=t + 1),  # graphic
        row(2, "c_BURST001.jpg", ts=t + 1),  # already a filename burst
        row(3, "c_BURST002.jpg", ts=t + 2),
        row(4, "d.jpg", ts=t + 30),
        row(5, "d_copy.jpg", ts=t + 30),  # non-keeper of d's duplicate group
    ]
    graphic = [False, True, False, False, False, False]
    bursts = find_bursts(rows, graphic, CFG, dup_group_of={4: 0, 5: 0}, dup_keepers={4})
    assert _members(bursts) == [("filename", [2, 3])]


def test_same_dup_group_keepers_do_not_link():
    t = 1_650_000_000
    rows = [row(0, "a.jpg", ts=t), row(1, "b.jpg", ts=t + 1)]
    # Both keepers of the *same* group cannot happen, but the explicit check guards it.
    assert find_bursts(rows, [False, False], CFG, dup_group_of={0: 0, 1: 0},
                       dup_keepers={0, 1}) == []


def test_undated_items_are_not_time_bursts():
    rows = [row(0, "a.jpg", ts=None), row(1, "b.jpg", ts=None)]
    assert find_bursts(rows, [False, False], CFG) == []


def test_burst_extra_reasons():
    t = 1_650_000_000
    rows = [row(0, "IMG_1.jpg", ts=t, lap=230), row(1, "IMG_2.jpg", ts=t + 2, lap=100),
            row(2, "IMG_3.jpg", ts=t + 3, lap=0)]
    bursts = find_bursts(rows, [False] * 3, CFG)
    extras = burst_extras(bursts, rows)
    assert set(extras) == {1, 2}
    score, reason = extras[1]
    assert score == 0.8
    assert reason == "burst of 3 in 3.0 s; #1 is 2.3x sharper"
    assert extras[2][1].endswith("#1 is sharper")
