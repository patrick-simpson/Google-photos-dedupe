"""Tests for gpclean.merge.scores: screenshot / messaging / tiny / pocket rules and the
ECDF-with-gates scores for blur, dark and overexposed."""

from __future__ import annotations

import pytest

from gpclean.merge.scores import (
    MIN_STORED,
    junk_scores,
    messaging_score,
    pocket_score,
    screen_shaped,
    screenshot_score,
    tiny_score,
)


def photo(**kw) -> dict:
    base = {"filename": "IMG_0001.jpg", "origin_folder": "Camera", "width": 4000,
            "height": 3000, "has_camera_exif": 1, "format": "JPEG", "flat_frac": 0.1,
            "lap_var": 300.0, "luma_mean": 120.0, "luma_std": 50.0, "frac_dark": 0.02,
            "frac_bright": 0.02}
    return {**base, **kw}


@pytest.mark.parametrize("w, h, want", [
    (1080, 2400, True), (2400, 1080, True), (1170, 2532, True), (1440, 3200, True),
    (2048, 1536, True), (1536, 2048, True), (1668, 2388, True),
    # Photo shapes and classic 4:3 display sizes are not screens.
    (1600, 1200, False), (1280, 960, False), (1200, 900, False), (2000, 1333, False),
    (4000, 3000, False), (1080, 1080, False), (1080, 1350, False), (200, 150, False),
    (0, 0, False),
])
def test_screen_shaped(w, h, want):
    assert screen_shaped(w, h) is want


def test_screenshot_score():
    assert screenshot_score(photo(filename="Screenshot_20200101-101010.png"))[0] == 0.95
    assert screenshot_score(photo(origin_folder="Screenshots"))[0] == 0.95
    no_exif = dict(has_camera_exif=0, width=1080, height=2400)
    assert screenshot_score(photo(format="PNG", **no_exif))[0] == 0.8
    assert screenshot_score(photo(flat_frac=0.5, **no_exif))[0] == 0.8
    score, reason = screenshot_score(photo(**no_exif))
    assert score == 0.65 and "1080x2400" in reason
    # Screen-shaped *with* camera EXIF is a photo; PNG photo shapes are not screenshots.
    assert screenshot_score(photo(width=1080, height=2400))[0] == 0.0
    assert screenshot_score(photo(format="PNG", has_camera_exif=0, width=1600,
                                  height=1200))[0] == 0.0


@pytest.mark.parametrize("filename, origin, app", [
    ("IMG-20210104-WA0001.jpg", "Camera", "WhatsApp"),
    ("received_1234567890123456.jpeg", None, "Messenger"),
    ("FB_IMG_1612345678901.jpg", None, "Facebook"),
    ("signal-2022-05-05-101010.jpg", None, "Signal"),
    ("IMG_0001.jpg", "WhatsApp Images", "WhatsApp"),
    ("IMG_0001.jpg", "Telegram Images", "Telegram"),
    ("IMG_0001.jpg", "Download", "Downloads"),
    ("IMG_0001.jpg", "Discord", "Discord"),
])
def test_messaging_score(filename, origin, app):
    score, reason = messaging_score(photo(filename=filename, origin_folder=origin))
    assert score == 0.9 and app in reason


def test_messaging_needs_a_sign():
    assert messaging_score(photo(origin_folder="Pictures"))[0] == 0.0
    assert messaging_score(photo(origin_folder=None))[0] == 0.0


def test_tiny_score():
    assert tiny_score(photo(width=200, height=150)) == (1.0, "tiny: 200x150")
    assert tiny_score(photo(width=700, height=500))[0] == 0.6
    assert tiny_score(photo(width=800, height=600))[0] == 0.0
    assert tiny_score(photo(width=None, height=None))[0] == 0.0


def test_pocket_score():
    assert pocket_score(photo(luma_mean=7, luma_std=3))[0] == 0.8
    assert pocket_score(photo(flat_frac=0.7))[0] == 0.7
    assert pocket_score(photo(lap_var=10))[0] == 0.6
    assert pocket_score(photo())[0] == 0.0
    assert pocket_score(photo(has_camera_exif=0, luma_mean=7, luma_std=3))[0] == 0.0


def _scores(rows, graphic=None, shot=None, burst=None, dup=None):
    graphic = graphic or [False] * len(rows)
    shot = shot or [screenshot_score(r) for r in rows]
    out = junk_scores(rows, graphic=graphic, shot=shot, burst_extra=burst or {},
                      dup_extra=dup or {})
    return {(k, cat): (score, reason) for k, cat, score, reason in out}


def _library(n=60):
    # A spread of ordinary camera photos: sharpness 100..700, brightness 80..160.
    return [photo(lap_var=100.0 + 10 * k, luma_mean=80.0 + (k % 9) * 10) for k in range(n)]


def test_gated_scores_reach_the_default_threshold():
    rows = _library() + [photo(lap_var=5.0), photo(luma_mean=12.0, luma_std=30.0),
                         photo(frac_bright=0.6, luma_mean=200.0)]
    s = _scores(rows)
    n = len(rows)
    assert s[(n - 3, "blur")][0] >= 0.9
    assert s[(n - 2, "dark")][0] >= 0.9
    assert s[(n - 1, "overexposed")][0] >= 0.9
    assert "blurrier than" in s[(n - 3, "blur")][1]


def test_ungated_scores_are_damped():
    rows = _library()
    s = _scores(rows)
    for (k, cat), (score, _reason) in s.items():
        if cat in ("blur", "dark", "overexposed"):
            assert score <= 0.3 + 1e-9, (k, cat)
    # The blurriest ordinary photo is still ranked (just damped), the sharpest is not stored.
    assert s[(0, "blur")][0] == pytest.approx(0.3 * 59 / 60, abs=1e-3)
    assert (59, "blur") not in s


def test_gate_floor_when_the_whole_library_is_dark():
    rows = [photo(luma_mean=10.0 + k * 0.1) for k in range(30)]
    s = _scores(rows)
    assert all(s[(k, "dark")][0] >= 0.5 for k in range(29))


def test_screenshots_get_no_photo_quality_scores_and_small_scores_are_dropped():
    shot = photo(filename="Screenshot_1.png", format="PNG", has_camera_exif=0, lap_var=1.0,
                 luma_mean=10.0, width=1080, height=2400)
    rows = _library() + [shot]
    k = len(rows) - 1
    s = _scores(rows, graphic=[False] * k + [True])
    assert s[(k, "screenshot")][0] == 0.95
    assert not {(k, c) for c in ("blur", "dark", "overexposed")} & set(s)
    assert all(score >= MIN_STORED for score, _r in s.values())


def test_burst_and_dup_extras_pass_through():
    rows = _library(3)
    s = _scores(rows, burst={1: (0.8, "burst of 3 in 1.0 s; #1 is 2.0x sharper")},
                dup={2: (1.0, "duplicate of #1")})
    assert s[(1, "burst_extra")] == (0.8, "burst of 3 in 1.0 s; #1 is 2.0x sharper")
    assert s[(2, "dup_extra")] == (1.0, "duplicate of #1")


def test_missing_features_do_not_crash():
    rows = [photo(lap_var=None, luma_mean=None, frac_bright=None, luma_std=None,
                  flat_frac=None, width=None, height=None, filename=None, origin_folder=None)]
    assert _scores(rows) == {}
