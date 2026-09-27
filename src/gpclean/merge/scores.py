"""Junk scores (PLAN section 5, "Junk scores"): 0..1 hints with a short human reason.

Scores are never verdicts; the user (or Claude, proposing) decides. Deterministic categories
(screenshot, messaging, tiny, burst_extra, dup_extra) come from names, dimensions and the
grouping. Pixel-statistics categories (blur, dark, overexposed) are ECDF percentiles over the
library's own camera photos, so "blurry" means "blurrier than most of *your* photos", and are
damped (x0.3) unless a fixed starting gate says the problem is real. The gates are starting
values to be tuned on the test Takeout (milestones M7/M12).

Every function takes item rows: dicts with the ``items`` columns of the bundle index.
"""

from __future__ import annotations

import numpy as np

from gpclean.takeout.names import is_screenshot_name, messaging_app

MIN_STORED = 0.05  # scores below this are not worth a row

# Short sides of common phone screens (px), for aspect ratios 1.6..2.4 (tall phones), and of
# tablets, for 1.3..1.45. Camera photos are 4:3 / 3:2 / 16:9 at other sizes, and a
# screen-shaped image *with* camera EXIF is a photo anyway.
PHONE_SHORT_SIDES = frozenset({720, 750, 828, 1080, 1125, 1170, 1179, 1242, 1284, 1290, 1320,
                               1440})
PHONE_RATIO = (1.6, 2.4)
TABLET_SHORT_SIDES = frozenset({1536, 1620, 1640, 1668, 2048})
TABLET_RATIO = (1.3, 1.45)

# Origin folder (localFolderName) substrings -> app name shown in the reason.
_MESSAGING_FOLDERS = (
    ("whatsapp", "WhatsApp"), ("telegram", "Telegram"), ("messenger", "Messenger"),
    ("signal", "Signal"), ("instagram", "Instagram"), ("snapchat", "Snapchat"),
    ("facebook", "Facebook"), ("reddit", "Reddit"), ("discord", "Discord"),
    ("download", "Downloads"),
)
_APP_NAMES = {"whatsapp": "WhatsApp", "messenger": "Messenger", "facebook": "Facebook",
              "signal": "Signal", "telegram": "Telegram", "snapchat": "Snapchat",
              "reddit": "Reddit"}

# Starting gates (PLAN table).
BLUR_GATE = 50.0  # lap_var at 512 px
DARK_GATE = 25.0  # mean luma
OVER_BRIGHT_GATE = 0.5  # frac_bright
OVER_MEAN_GATE = 215.0  # mean luma
DAMP = 0.3  # factor for scores whose gate did not pass
GATE_FLOOR = 0.5  # a passed gate always reaches the default "show" threshold
MIN_REFERENCE = 20  # fewer camera photos than this -> use all non-graphic photos


def _dims(row: dict) -> tuple[int, int]:
    return int(row.get("width") or 0), int(row.get("height") or 0)


def screen_shaped(width: int, height: int) -> bool:
    """True for the pixel sizes phones and tablets take screenshots at."""
    short, long_ = sorted((width, height))
    if short <= 0:
        return False
    ratio = long_ / short
    if short in PHONE_SHORT_SIDES and PHONE_RATIO[0] <= ratio <= PHONE_RATIO[1]:
        return True
    return short in TABLET_SHORT_SIDES and TABLET_RATIO[0] <= ratio <= TABLET_RATIO[1]


def screenshot_score(row: dict) -> tuple[float, str | None]:
    """(score, reason) for "this is a screenshot". Computed before grouping (it feeds is_graphic)."""
    if is_screenshot_name(row.get("filename") or ""):
        return 0.95, "screenshot file name"
    origin = (row.get("origin_folder") or "").casefold()
    if "screenshot" in origin or "screen shot" in origin:
        return 0.95, "from a Screenshots folder"
    w, h = _dims(row)
    if not row.get("has_camera_exif") and screen_shaped(w, h):
        if (row.get("format") or "").upper() == "PNG" or (row.get("flat_frac") or 0) >= 0.35:
            return 0.8, f"screen-sized {w}x{h} graphic, no camera data"
        return 0.65, f"screen-sized {w}x{h}, no camera data"
    return 0.0, None


def messaging_score(row: dict) -> tuple[float, str | None]:
    """(score, reason) for images saved from a messaging / social app."""
    app = messaging_app(row.get("filename") or "")
    if app:
        return 0.9, f"saved from {_APP_NAMES.get(app, app)} (file name)"
    origin = (row.get("origin_folder") or "").casefold()
    for needle, name in _MESSAGING_FOLDERS:
        if needle in origin:
            return 0.9, f"from a {name} folder"
    return 0.0, None


def tiny_score(row: dict) -> tuple[float, str | None]:
    """1.0 below 480 px on the long edge, 0.6 below 800 px."""
    w, h = _dims(row)
    long_ = max(w, h)
    if 0 < long_ < 480:
        return 1.0, f"tiny: {w}x{h}"
    if 0 < long_ < 800:
        return 0.6, f"small: {w}x{h}"
    return 0.0, None


def pocket_score(row: dict) -> tuple[float, str | None]:
    """Accidental shots: camera EXIF and a black, flat or featureless frame."""
    if not row.get("has_camera_exif"):
        return 0.0, None
    mean, std = row.get("luma_mean"), row.get("luma_std")
    flat, lap = row.get("flat_frac"), row.get("lap_var")
    if mean is not None and std is not None and mean < 25 and std < 12:
        return 0.8, "black frame (pocket or lens cap?)"
    if flat is not None and flat > 0.6:
        return 0.7, f"{flat:.0%} of the frame is one flat tone"
    if lap is not None and lap < 20:
        return 0.6, "almost no detail"
    return 0.0, None


class _Ecdf:
    """Fraction of a reference population that is *less bad* than a value."""

    def __init__(self, values: list[float]):
        self.sorted = np.sort(np.asarray(values, dtype=np.float64))

    def below(self, x: float) -> float:
        """Share of reference values strictly below ``x``."""
        n = self.sorted.size
        return float(np.searchsorted(self.sorted, x, side="left")) / n if n else 0.0

    def above(self, x: float) -> float:
        """Share of reference values strictly above ``x``."""
        n = self.sorted.size
        return float(n - np.searchsorted(self.sorted, x, side="right")) / n if n else 0.0


def _gated(pct: float, gate: bool) -> float:
    return max(pct, GATE_FLOOR) if gate else pct * DAMP


def _over_value(row: dict) -> float:
    # One "how blown out" number for the percentile: whichever of the two signals is worse.
    return max(float(row["frac_bright"]), float(row["luma_mean"]) / 255.0)


def _reference(rows: list[dict], graphic: list[bool]) -> list[int]:
    ref = [k for k, r in enumerate(rows) if r.get("has_camera_exif") and not graphic[k]]
    if len(ref) < MIN_REFERENCE:
        ref = [k for k in range(len(rows)) if not graphic[k]]
    return ref or list(range(len(rows)))


def junk_scores(
    rows: list[dict],
    *,
    graphic: list[bool],
    shot: list[tuple[float, str | None]],
    burst_extra: dict[int, tuple[float, str]],
    dup_extra: dict[int, tuple[float, str]],
) -> list[tuple[int, str, float, str]]:
    """All scores >= MIN_STORED as (row index, category, score, reason), sorted.

    ``shot`` is :func:`screenshot_score` per row (already computed for is_graphic),
    ``burst_extra`` / ``dup_extra`` come from the burst and duplicate stages.
    """
    ref = _reference(rows, graphic)

    def values(col: str) -> list[float]:
        return [float(rows[k][col]) for k in ref if rows[k].get(col) is not None]

    lap = _Ecdf(values("lap_var"))
    mean = _Ecdf(values("luma_mean"))
    over = _Ecdf([_over_value(rows[k]) for k in ref
                  if rows[k].get("frac_bright") is not None
                  and rows[k].get("luma_mean") is not None])

    out: list[tuple[int, str, float, str]] = []

    def add(k: int, cat: str, score: float, reason: str | None) -> None:
        if score >= MIN_STORED and reason:
            out.append((k, cat, round(min(1.0, float(score)), 4), reason))

    for k, r in enumerate(rows):
        add(k, "screenshot", *shot[k])
        add(k, "messaging", *messaging_score(r))
        add(k, "tiny", *tiny_score(r))
        add(k, "pocket", *pocket_score(r))
        if k in burst_extra:
            add(k, "burst_extra", *burst_extra[k])
        if k in dup_extra:
            add(k, "dup_extra", *dup_extra[k])
        if shot[k][0] >= 0.8:
            continue  # "blurry" or "dark" says nothing useful about a screenshot
        if r.get("lap_var") is not None:
            lv = float(r["lap_var"])
            pct = lap.above(lv)
            add(k, "blur", _gated(pct, lv < BLUR_GATE),
                f"sharpness {lv:.0f}: blurrier than {pct:.0%} of camera photos")
        if r.get("luma_mean") is not None:
            lm = float(r["luma_mean"])
            pct = mean.above(lm)
            add(k, "dark", _gated(pct, lm < DARK_GATE),
                f"brightness {lm:.0f}/255: darker than {pct:.0%} of camera photos")
        if r.get("frac_bright") is not None and r.get("luma_mean") is not None:
            fb, lm = float(r["frac_bright"]), float(r["luma_mean"])
            pct = over.below(_over_value(r))
            add(k, "overexposed", _gated(pct, fb > OVER_BRIGHT_GATE or lm > OVER_MEAN_GATE),
                f"{fb:.0%} blown-out pixels, brightness {lm:.0f}/255")
    out.sort(key=lambda t: (t[0], t[1]))
    return out
