"""Cheap per-image quality features used by the junk scores and burst best-shot choice.

All features come from the work image at fixed working sizes, so values are comparable
across photos of any original resolution (PLAN section 5, "Junk scores").
"""

from __future__ import annotations

import numpy as np
from PIL import Image

_LAP_EDGE = 512  # sharpness is measured at this long edge
_STATS_EDGE = 128  # luma statistics and colourfulness at this long edge
_DARK = 20  # luma below this counts as dark
_BRIGHT = 245  # luma above this counts as blown out
_FLAT_BINS = 32


def _shrink(im: Image.Image, long_edge: int, resample: Image.Resampling) -> Image.Image:
    """Downscale to ``long_edge``; never upscale (upscaling would invent smoothness)."""
    w, h = im.size
    if max(w, h) <= long_edge:
        return im
    scale = long_edge / max(w, h)
    return im.resize((max(1, round(w * scale)), max(1, round(h * scale))), resample)


def laplacian_variance(luma: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian: low for blurry or featureless images."""
    if luma.shape[0] < 3 or luma.shape[1] < 3:
        return 0.0
    x = luma.astype(np.float32)
    lap = (
        x[:-2, 1:-1] + x[2:, 1:-1] + x[1:-1, :-2] + x[1:-1, 2:] - 4.0 * x[1:-1, 1:-1]
    )
    return float(lap.var(dtype=np.float64))


def colorfulness(rgb: np.ndarray) -> float:
    """Hasler & Suesstrunk (2003) colourfulness metric; ~0 for grayscale images."""
    r, g, b = (rgb[..., i].astype(np.float64) for i in range(3))
    rg = r - g
    yb = 0.5 * (r + g) - b
    std_root = np.hypot(rg.std(), yb.std())
    mean_root = np.hypot(rg.mean(), yb.mean())
    return float(std_root + 0.3 * mean_root)


def features(work: Image.Image) -> dict:
    """Return lap_var, luma_mean, luma_std, luma_p02, luma_p98, frac_dark, frac_bright,
    flat_frac and colorfulness (plain floats) for an RGB work image.

    ``flat_frac`` is the share of pixels in the fullest bin of a 32-bin luma histogram: high
    for pocket shots, blank pages and flat-coloured graphics.
    """
    rgb_img = work if work.mode == "RGB" else work.convert("RGB")
    luma_img = rgb_img.convert("L")

    lap = _shrink(luma_img, _LAP_EDGE, Image.Resampling.LANCZOS)
    lap_var = laplacian_variance(np.asarray(lap))

    # BOX (area average) keeps statistics honest; LANCZOS ringing would add fake extremes.
    small_rgb = _shrink(rgb_img, _STATS_EDGE, Image.Resampling.BOX)
    small = np.asarray(small_rgb.convert("L"), dtype=np.float64)
    hist, _ = np.histogram(small, bins=_FLAT_BINS, range=(0.0, 256.0))
    p02, p98 = np.percentile(small, [2.0, 98.0])
    return {
        "lap_var": lap_var,
        "luma_mean": float(small.mean()),
        "luma_std": float(small.std()),
        "luma_p02": float(p02),
        "luma_p98": float(p98),
        "frac_dark": float((small < _DARK).mean()),
        "frac_bright": float((small > _BRIGHT).mean()),
        "flat_frac": float(hist.max() / small.size),
        "colorfulness": colorfulness(np.asarray(small_rgb)),
    }
