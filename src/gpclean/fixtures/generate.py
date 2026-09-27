"""Fake Google Takeout export with planted cases, plus the ground truth (``expected.json``).

The end-to-end test runs the whole pipeline over these zips and compares its output with
``expected.json``: every planted duplicate must be found (0 false negatives) and nothing else
may be grouped (0 false positives). So the generator's first job is to be *right* about what
it planted; realism comes second, speed third.

What gets written (see docs/INTERFACES.md "Fixtures" and docs/PLAN.md section 11)::

    out/takeout-20260101T000000Z-001.zip   export A, part 1
    out/takeout-20260101T000000Z-002.zip   export A, part 2
    out/takeout-20260201T000000Z-001.zip   export B (a second, overlapping export)
    out/expected.json

Everything is synthetic: images are seeded value noise + random shapes + rendered text, url
ids are ``AF1QipFAKE...``, GPS points lie in the open Atlantic, people are made up.

How to read ``expected.json`` (conventions the INTERFACES.md schema leaves open):

- It describes a run with ``include_albums=true`` (recorded as ``"include_albums": true``).
  Every album photo is a copy of a year-folder photo with the same url, so ``n_items`` is the
  same either way; :func:`expected_view` derives the ``include_albums=false`` truth (album
  members leave ``item_keys`` / ``pairings`` / ``collapsed`` and become ``album_media`` skips).
- ``collapsed[*].copies`` lists *every* member of that item (primary included), sorted.
- ``pairings`` covers every image member (``null`` = it truly has no sidecar in its export).
- ``junk`` maps an item key to the categories it must be flagged in. It is exhaustive for the
  deterministic categories (``screenshot``, ``messaging``, ``tiny``, ``dup_extra``,
  ``burst_extra``); for the pixel-statistics ones (``blur``, ``dark``, ``overexposed``,
  ``pocket``) it lists only what was deliberately planted, and other items are unconstrained.
- ``dup_groups[*].keeper`` follows PLAN deviation 5 (resolution, camera EXIF, GPS,
  favorited / albums, larger file, earlier taken). Every group is decided by one level with no
  ties above it; GPS is present in EXIF and sidecar together or in neither, so it does not
  matter which one the merge looks at.
- ``bursts`` lists filename bursts and time bursts together; ``best`` is the sharpest frame.
  A burst has at least two frames: a lone name that ``names.burst_key`` recognises (the
  Pixel ``PXL_...RAW-01.MP.COVER.jpg``, whose ``.dng`` partner is skipped) is *not* a burst.
  Every frame pair of a time burst is within ``MergeConfig.burst_phash_max``, so the result
  does not depend on how the merge orders frames that share a capture second.
- Items without camera EXIF that are not screenshots use photo shapes (4:3, 3:4 or 3:2 give or
  take a pixel), several of them at classic 4:3 display sizes (1600x1200, 1280x960,
  1200x900). The screenshot score's screen-shape rule must not flag 4:3 or 3:2 shapes, or
  those items turn graphic, groups P2/P5/P6/P8/P9 fall apart and ``screenshot`` gains items.
  The only other no-EXIF sizes are the 200x150 ``tiny`` thumbnail and 2000x1333 (aspect
  1.5004, P9).
- One photo (C2) is ``archived`` in both exports; ``Trash/`` holds one skipped image. Partner
  sharing and ``trashed: true`` sidecars are not planted.
- ``videos.by_day`` uses the video sidecar's ``photoTakenTime`` in America/New_York.
- ``unindexed_by_day`` counts, per local day (same rule), every year-folder library item that
  gets no index row: the videos plus the skipped ``raw`` / ``other_image`` / ``too_large``
  media that have a sidecar (one per url). It is what the merge writes to ``videos_by_day``,
  which the review site checks before it offers "select the whole day". Album copies are not
  counted (they are copies of year-folder items), so it is the same with or without albums.

Where the PLAN section 11 cases live: P1-P12 in :func:`_plant_positives`, C1-C3 in
:func:`_plant_collapses`, N1-N6 in :func:`_plant_negatives`, N7 in :func:`_plant_fillers`,
junk and filename bursts in :func:`_plant_junk`, videos and skips in
:func:`_plant_videos_and_misc`. Sidecar cases S1-S11 mostly ride along on those items (each
is marked ``(+Sn)`` in a comment); the rest are in :func:`_plant_sidecar_cases`.
"""

from __future__ import annotations

import copy
import functools
import io
import json
import logging
import random
import re
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pillow_heif
from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

ZIP_A1 = "takeout-20260101T000000Z-001.zip"
ZIP_A2 = "takeout-20260101T000000Z-002.zip"
ZIP_B1 = "takeout-20260201T000000Z-001.zip"
ZIP_NAMES = (ZIP_A1, ZIP_A2, ZIP_B1)
# Fixed member timestamps so the zip bytes depend only on the seed.
_ZIP_TIME = {ZIP_A1: (2026, 1, 1, 0, 0, 0), ZIP_A2: (2026, 1, 1, 0, 0, 0),
             ZIP_B1: (2026, 2, 1, 0, 0, 0)}

ROOT = "Takeout/Google Photos"
ALBUM = "Summer Trip 2021"
TZ = ZoneInfo("America/New_York")  # the fixture's owner lives here (PLAN: tz fallback)

N_FILLERS_DEFAULT = 300

# Takeout caps a sidecar name at 46 characters before ".json" (51 in total); longer names are
# cut from the end, which is how ".supplemental-metada.json", ".suppl.json" and ".s.json" occur.
JSON_STEM_MAX = 46
SUPP = ".supplemental-metadata"

# Fictional phones. Make/Model must be present for "has camera EXIF"; bursts need one model.
CAM_MAIN = ("Fixturephone", "FP-7")
CAM_OLD = ("Fixturephone", "FP-5")
CAM_DSLR = ("Examplecam", "EX-100")

_WORDS = (
    "river maple harbor lantern meadow copper violet summit cedar orbit willow pebble "
    "canyon ember glacier juniper marble nectar opal prairie quartz saffron tundra velvet "
    "breeze cobalt dune fern garnet hazel indigo jasper kelp lagoon mesa north ocean pine"
).split()
_CHAT = (
    "Are we still on for tonight?", "Yes, 7pm at the usual place", "Running ten minutes late",
    "Can you grab milk on the way?", "Did you see the game last night?", "No, what happened?",
    "We won in overtime!", "Happy birthday!!", "Thanks so much", "Call me when you land",
    "Parking is on the left side", "Bring the blue folder please", "See you there",
    "The package arrived today", "Great news about the job", "Dinner is ready",
    "Which train are you on?", "Almost home", "Sounds good to me", "Let me check and get back",
)
_PEOPLE = ("Alex Example", "Sam Placeholder", "Jordan Sample", "Riley Testcase")


# ---------------------------------------------------------------------------------------------
# Image synthesis
# ---------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Pillow's bundled font at ``size`` px (no font files needed on any platform)."""
    return ImageFont.load_default(size=size)


def _noise(rng: np.random.Generator, w: int, h: int, octaves: int = 7,
           persistence: float = 0.55) -> np.ndarray:
    """Multi-octave value noise in [0, 1], shape (h, w): smooth, photo-like variation."""
    acc = np.zeros((h, w), np.float32)
    amp, total = 1.0, 0.0
    for octave in range(1, octaves + 1):
        gw = 2**octave + 1
        gh = max(2, round(2**octave * h / w) + 1)
        grid = rng.random((gh, gw), dtype=np.float32)
        up = Image.fromarray(grid, "F").resize((w, h), Image.Resampling.BICUBIC)
        acc += amp * np.asarray(up)
        total += amp
        amp *= persistence
    acc /= total
    lo, hi = float(acc.min()), float(acc.max())
    return (acc - lo) / max(hi - lo, 1e-6)


def _grain(im: Image.Image, rng: np.random.Generator, amp: float = 2.5) -> Image.Image:
    """Add sensor-like grain.

    A 256 px noise patch is tiled over the image and added in C (ImageChops), which keeps a
    4000 px photo cheap to make; the repeat is invisible to every fingerprint we compute.
    """
    tile = np.clip(rng.normal(128.0, amp, (256, 256, 3)), 0, 255).astype(np.uint8)
    patch = Image.fromarray(tile, "RGB")
    canvas = Image.new("RGB", im.size)
    for y in range(0, im.height, 256):
        for x in range(0, im.width, 256):
            canvas.paste(patch, (x, y))
    return ImageChops.add(im.convert("RGB"), canvas, 1.0, -128)


def _random_colour(rng: np.random.Generator) -> tuple[int, int, int]:
    return tuple(int(v) for v in rng.integers(0, 256, 3))


def _shapes(draw: ImageDraw.ImageDraw, rng: np.random.Generator, w: int, h: int,
            n: int) -> None:
    """Random opaque shapes: they give pHash and the signature real, local structure."""
    for _ in range(n):
        kind = int(rng.integers(0, 4))
        cx, cy = float(rng.uniform(0, w)), float(rng.uniform(0, h))
        r = float(rng.uniform(0.03, 0.16)) * min(w, h)
        colour = _random_colour(rng)
        if kind == 0:
            draw.ellipse([cx - r, cy - r * rng.uniform(0.5, 1.5), cx + r,
                          cy + r * rng.uniform(0.5, 1.5)], fill=colour)
        elif kind == 1:
            draw.rectangle([cx - r, cy - r * 0.7, cx + r, cy + r * 0.7], fill=colour)
        elif kind == 2:
            k = int(rng.integers(3, 7))
            angles = np.sort(rng.uniform(0, 2 * np.pi, k))
            pts = [(cx + r * np.cos(a), cy + r * np.sin(a)) for a in angles]
            draw.polygon(pts, fill=colour)
        else:
            x2, y2 = float(rng.uniform(0, w)), float(rng.uniform(0, h))
            draw.line([cx, cy, x2, y2], fill=colour, width=max(2, int(r / 6)))


def _words(rng: np.random.Generator, n: int) -> str:
    return " ".join(_WORDS[int(i)] for i in rng.integers(0, len(_WORDS), n))


def _is_textured(im: Image.Image) -> bool:
    """True when ``im`` is clearly a 'photo' for the pipeline, not a flat graphic.

    Mirrors (with margin) the merge's graphic test: luma histogram of a 128 px thumbnail
    must not pile up in one of 32 bins, and the 32x32 luma must vary.
    """
    thumb = im.convert("L")
    thumb.thumbnail((128, 128), Image.Resampling.BOX)
    luma = np.asarray(thumb, np.float32)
    hist, _ = np.histogram(luma, bins=32, range=(0.0, 256.0))
    box = np.asarray(im.convert("L").resize((32, 32), Image.Resampling.BOX), np.float32)
    return hist.max() / luma.size < 0.2 and float(box.std()) > 25.0


def _photo_base(rng: np.random.Generator, bw: int, bh: int, shapes: int | None,
                text: bool) -> Image.Image:
    # The texture is smooth, so it is made at <= 320 px and upscaled; shapes and text are
    # drawn afterwards at full base size to keep their edges crisp.
    nw = min(bw, 320)
    nh = max(2, round(nw * bh / bw))
    t = _noise(rng, nw, nh)[..., None]
    u = _noise(rng, nw, nh, octaves=5)[..., None]
    # A dark and a bright end colour guarantee real luma contrast across the texture.
    c0 = rng.integers(0, 100, 3).astype(np.float32)
    c1 = rng.integers(150, 256, 3).astype(np.float32)
    c2 = np.array(_random_colour(rng), np.float32)
    rgb = c0 * (1 - t) + c1 * t
    rgb = rgb * (1 - 0.5 * u) + c2 * (0.5 * u)
    im = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")
    if (nw, nh) != (bw, bh):
        im = im.resize((bw, bh), Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(im)
    _shapes(draw, rng, bw, bh, int(rng.integers(6, 14)) if shapes is None else shapes)
    if text:
        size = max(12, int(bw * rng.uniform(0.04, 0.07)))
        x, y = float(rng.uniform(0, bw * 0.4)), float(rng.uniform(0, bh * 0.8))
        label = _words(rng, 2).upper()
        tw = draw.textlength(label, font=_font(size))
        draw.rectangle([x - 6, y - 6, x + tw + 6, y + size * 1.3], fill=_random_colour(rng))
        draw.text((x, y), label, fill=_random_colour(rng), font=_font(size))
    return im


def photo(rng: np.random.Generator, w: int, h: int, *, shapes: int | None = None,
          text: bool = True, grain: float = 2.5) -> Image.Image:
    """A distinct 'photo': coloured fractal texture, random shapes and a rendered text sign.

    Content is built at <= 900 px wide and upscaled, like a soft camera image, so even a
    4000 px picture takes a fraction of a second to make. Draws that come out too flat to
    pass as a photo are redrawn (deterministically, from the same generator).
    """
    bw = min(w, 900)
    bh = max(2, round(bw * h / w))
    for _ in range(20):
        im = _photo_base(rng, bw, bh, shapes, text)
        if _is_textured(im):
            break
    else:  # pragma: no cover - 20 flat draws in a row does not happen in practice
        raise RuntimeError("could not draw a textured photo")
    if (w, h) != (bw, bh):
        # Bilinear is plenty for soft content and twice as fast as bicubic at 4000 px.
        im = im.resize((w, h), Image.Resampling.BILINEAR)
    return _grain(im, rng, grain) if grain else im


def chat_screenshot(rng: np.random.Generator, messages: list[str], *, clock: str,
                    contact: str, w: int = 1080, h: int = 2400) -> Image.Image:
    """One fixed chat-app template (status bar, app bar, bubbles, keyboard bar).

    Only the text changes between screenshots, which is exactly the pHash trap PLAN warns
    about: these must never group unless byte-identical.
    """
    im = Image.new("RGB", (w, h), (236, 229, 221))
    d = ImageDraw.Draw(im)
    small, body = _font(36), _font(44)
    d.rectangle([0, 0, w, 90], fill=(7, 94, 84))
    d.text((40, 24), clock, fill=(255, 255, 255), font=small)
    d.text((w - 190, 24), "5G  87%", fill=(255, 255, 255), font=small)
    d.rectangle([0, 90, w, 250], fill=(18, 140, 126))
    d.ellipse([30, 115, 140, 225], fill=(200, 200, 200))
    d.text((170, 140), contact, fill=(255, 255, 255), font=_font(52))
    d.rectangle([0, h - 150, w, h], fill=(255, 255, 255))
    d.rounded_rectangle([30, h - 125, w - 160, h - 25], radius=45, fill=(240, 240, 240))
    d.text((70, h - 100), "Message", fill=(150, 150, 150), font=body)
    y = 300
    for i, msg in enumerate(messages):
        tw = int(d.textlength(msg, font=body))
        right = i % 2 == 1
        x0 = w - 40 - tw - 60 if right else 40
        colour = (220, 248, 198) if right else (255, 255, 255)
        d.rounded_rectangle([x0, y, x0 + tw + 60, y + 110], radius=24, fill=colour)
        d.text((x0 + 30, y + 30), msg, fill=(20, 20, 20), font=body)
        d.text((x0 + tw - 60, y + 80), f"{10 + i}:0{i % 10}", fill=(120, 120, 120),
               font=_font(24))
        y += 150 + int(rng.integers(0, 40))
    return im


def document_photo(rng: np.random.Generator, lines: list[str], w: int = 1500,
                   h: int = 2000) -> Image.Image:
    """A phone photo of a printed page on a desk: mostly white paper with lines of text."""
    desk = _noise(rng, 300, round(300 * h / w), octaves=5)[..., None]
    rgb = np.array((92, 64, 40), np.float32) * (0.7 + 0.5 * desk)
    im = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").resize(
        (w, h), Image.Resampling.BICUBIC)
    d = ImageDraw.Draw(im)
    mx, my = int(w * 0.05), int(h * 0.04)
    skew = float(rng.uniform(-0.01, 0.01)) * w
    d.polygon([(mx + skew, my), (w - mx + skew, my + 6), (w - mx - skew, h - my),
               (mx - skew, h - my - 6)], fill=(243, 241, 236))
    font = _font(max(18, w // 42))
    y = my + h * 0.06
    step = (h - 2 * my - h * 0.1) / max(1, len(lines))
    for line in lines:
        d.text((mx + w * 0.07, y), line, fill=(35, 35, 40), font=font)
        y += step
    return _grain(im, rng, 1.5)


def flat_frame(rng: np.random.Generator, w: int, h: int, colour: tuple[int, int, int],
               *, noise: float, gradient: float = 0.0) -> Image.Image:
    """Near-uniform frame (lens cap, pocket shot, white wall): different noise, same look."""
    if gradient:
        # A gentle left-to-right light falloff, as on a lit wall.
        ramp = np.linspace(-gradient, gradient, w, dtype=np.float32)[None, :, None]
        row = np.clip(np.array(colour, np.float32)[None, None, :] + ramp, 0, 255)
        im = Image.fromarray(row.astype(np.uint8), "RGB").resize((w, h))
    else:
        im = Image.new("RGB", (w, h), colour)
    # Coarse (4 px) sensor noise, added in C to keep 3 MP frames cheap.
    small = rng.normal(128.0, noise, (h // 4 + 1, w // 4 + 1, 3))
    grain = Image.fromarray(np.clip(small, 0, 255).astype(np.uint8), "RGB").resize(
        (w, h), Image.Resampling.NEAREST)
    return ImageChops.add(im, grain, 1.0, -128)


def same_luma_hues(rng: np.random.Generator, w: int, h: int) -> tuple[Image.Image, Image.Image]:
    """Two images with the same luma channel and opposite chroma (warm vs cold).

    pHash and the luma part of the signature are identical, so only the chroma check can
    tell them apart (PLAN N3).
    """
    luma = np.asarray(photo(rng, w, h, grain=0).convert("L"), np.float32)
    # Squeeze luma a little so neither chroma pushes RGB into clipping (which would change
    # the luma of one image but not the other).
    y = 30 + luma * (195 / 255)
    out = []
    for cb, cr in ((108, 150), (150, 108)):
        ycc = np.stack([y, np.full_like(y, cb), np.full_like(y, cr)], axis=-1)
        im = Image.fromarray(ycc.astype(np.uint8), "YCbCr").convert("RGB")
        out.append(im)
    return out[0], out[1]


def with_subject(bg: Image.Image, x_frac: float, *, blur: float = 0.0,
                 scale: float = 1.0, ball: tuple[float, float] | None = None) -> Image.Image:
    """Draw a high-contrast 'subject' (a figure) at horizontal position ``x_frac``.

    Used for burst frames: the background stays put while the subject moves or blurs.
    ``ball`` adds a small magenta ball at that (x, y) fraction: it moves far between frames
    yet is too small to shift pHash much, which is how a burst of a thrown ball looks.
    """
    im = bg.copy()
    w, h = im.size
    d = ImageDraw.Draw(im)
    cx, cy, r = x_frac * w, 0.55 * h, 0.16 * h * scale
    d.ellipse([cx - r * 0.7, cy - r, cx + r * 0.7, cy + r], fill=(28, 28, 40))
    d.rectangle([cx - r * 0.7, cy - r * 0.15, cx + r * 0.7, cy + r * 0.15], fill=(245, 205, 40))
    d.ellipse([cx - r * 0.35, cy - r * 1.75, cx + r * 0.35, cy - r * 1.05], fill=(230, 180, 150))
    if ball is not None:
        bx, by, br = ball[0] * w, ball[1] * h, 0.05 * h
        d.ellipse([bx - br, by - br, bx + br, by + br], fill=(250, 40, 190))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    return im


def _p3_profile() -> bytes:
    """A real ICC v2 display profile with Display P3 primaries (D65 white, gamma 2.2).

    ImageCms can only *create* sRGB/LAB/XYZ profiles, so the file is assembled by hand:
    header + desc/wtpt/rXYZ/gXYZ/bXYZ/TRC/cprt tags, colorants Bradford-adapted to D50.
    """

    def xy_to_xyz(x: float, y: float) -> np.ndarray:
        return np.array([x / y, 1.0, (1 - x - y) / y])

    prim = np.stack([xy_to_xyz(0.680, 0.320), xy_to_xyz(0.265, 0.690),
                     xy_to_xyz(0.150, 0.060)], 1)
    white65 = xy_to_xyz(0.3127, 0.3290)
    m = prim * np.linalg.solve(prim, white65)  # RGB -> XYZ (D65)
    brad = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367],
                     [0.0389, -0.0685, 1.0296]])
    d50 = np.array([0.9642, 1.0, 0.8249])
    m50 = np.linalg.inv(brad) @ np.diag((brad @ d50) / (brad @ white65)) @ brad @ m

    def s15(v: float) -> bytes:
        return struct.pack(">i", round(v * 65536))

    def xyz_tag(v) -> bytes:
        return b"XYZ " + b"\0" * 4 + b"".join(s15(float(c)) for c in v)

    text = b"Display P3 (gpclean fixture)\0"
    desc = (b"desc" + b"\0" * 4 + struct.pack(">I", len(text)) + text
            + b"\0" * 8 + b"\0" * 3 + b"\0" * 67)
    curv = b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", round(2.2 * 256))
    tags = [(b"desc", desc), (b"wtpt", xyz_tag(d50)), (b"rXYZ", xyz_tag(m50[:, 0])),
            (b"gXYZ", xyz_tag(m50[:, 1])), (b"bXYZ", xyz_tag(m50[:, 2])),
            (b"rTRC", curv), (b"gTRC", curv), (b"bTRC", curv),
            (b"cprt", b"text" + b"\0" * 4 + b"public domain\0")]
    table = struct.pack(">I", len(tags))
    body = b""
    offset = 128 + 4 + 12 * len(tags)
    for sig, data in tags:
        while (offset + len(body)) % 4:
            body += b"\0"
        table += sig + struct.pack(">II", offset + len(body), len(data))
        body += data
    size = 128 + len(table) + len(body)
    header = (struct.pack(">I", size) + b"lcms" + struct.pack(">I", 0x02100000)
              + b"mntr" + b"RGB " + b"XYZ " + b"\0" * 12 + b"acsp" + b"\0" * 4
              + b"\0" * 4 + b"\0" * 8 + b"\0" * 8 + struct.pack(">I", 0)
              + b"".join(s15(float(c)) for c in d50) + b"\0" * 4 + b"\0" * 16 + b"\0" * 28)
    return header + table + body


def to_display_p3(im: Image.Image) -> tuple[Image.Image, bytes]:
    """Convert sRGB pixels to Display P3 pixel values; returns (image, ICC bytes to embed)."""
    icc = _p3_profile()
    dst = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    xform = ImageCms.buildTransform(ImageCms.createProfile("sRGB"), dst, "RGB", "RGB")
    return ImageCms.applyTransform(im, xform), icc


# ---------------------------------------------------------------------------------------------
# Encoders and metadata
# ---------------------------------------------------------------------------------------------


def encode(im: Image.Image, fmt: str = "JPEG", **params) -> bytes:
    """Encode ``im`` to bytes (``fmt`` is a PIL format name; HEIF goes through pillow-heif)."""
    if fmt == "HEIF":
        pillow_heif.register_heif_opener()
        # x265's default preset takes ~1 s per image; "ultrafast" is 8x quicker and still a
        # perfectly ordinary HEIC file. A libheif built with another HEVC encoder rejects the
        # parameter, so fall back to its defaults there.
        try:
            buf = io.BytesIO()
            im.save(buf, format=fmt, enc_params={"preset": "ultrafast"}, **params)
            return buf.getvalue()
        except ValueError:
            pass
    buf = io.BytesIO()
    im.save(buf, format=fmt, **params)
    return buf.getvalue()


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    deg = int(value)
    minutes = int((value - deg) * 60)
    sec = round((value - deg - minutes / 60) * 3600, 2)
    return float(deg), float(minutes), sec


def camera_exif(cam: tuple[str, str] | None, when: datetime | None, *,
                subsec: str | None = None, offset: bool = False, orientation: int | None = None,
                gps: tuple[float, float] | None = None, software: str | None = None) -> bytes:
    """EXIF as a phone writes it: Make/Model/ExposureTime + DateTimeOriginal (local time).

    ``offset=True`` adds OffsetTimeOriginal for the New York offset at ``when``.
    """
    ex = Image.Exif()
    if cam:
        ex[0x010F], ex[0x0110] = cam
    if orientation is not None:
        ex[0x0112] = orientation
    if software:
        ex[0x0131] = software
    sub = ex.get_ifd(0x8769)
    if when is not None:
        stamp = when.strftime("%Y:%m:%d %H:%M:%S")
        ex[0x0132] = stamp
        sub[0x9003] = stamp  # DateTimeOriginal
        sub[0x9004] = stamp  # DateTimeDigitized
        if offset:
            off = when.replace(tzinfo=TZ).utcoffset()
            minutes = int(off.total_seconds() // 60)
            sign = "-" if minutes < 0 else "+"
            sub[0x9011] = f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    if subsec is not None:
        sub[0x9291] = subsec  # SubSecTimeOriginal
    if cam:
        sub[0x829A] = 0.008  # ExposureTime
        sub[0x8827] = 100  # ISO
    if gps is not None:
        g = ex.get_ifd(0x8825)
        g[1], g[2] = ("N" if gps[0] >= 0 else "S"), _dms(gps[0])
        g[3], g[4] = ("E" if gps[1] >= 0 else "W"), _dms(gps[1])
    return ex.tobytes()


def utc_ts(local: datetime) -> int:
    """Epoch seconds of a naive New York wall-clock time."""
    return int(local.replace(tzinfo=TZ).timestamp())


def _formatted(ts: int) -> str:
    """Takeout's English (US) timestamp text, e.g. ``Mar 4, 2019, 3:05:00 PM UTC``."""
    d = datetime.fromtimestamp(ts, UTC)
    hour = d.hour % 12 or 12
    ampm = "AM" if d.hour < 12 else "PM"
    return f"{d:%b} {d.day}, {d.year}, {hour}:{d:%M:%S} {ampm} UTC"


def sidecar_name(media: str, *, style: str = "new", dup: int | None = None) -> str:
    """The sidecar name Takeout writes for ``media`` (the name *without* any "(n)").

    New style ``NAME.ext.supplemental-metadata.json``, old style ``NAME.ext.json``; the part
    before ".json" is cut to 46 characters, and a duplicate index lands after the cut.
    """
    stem = media + (SUPP if style == "new" else "")
    stem = stem[:JSON_STEM_MAX]
    return stem + (f"({dup})" if dup is not None else "") + ".json"


def sidecar_bytes(*, title: str, taken: int, created: int, url_id: str,
                  gps: tuple[float, float] | None = None, origin: str | None = "Camera",
                  favorited: bool = False, archived: bool = False, description: str = "",
                  people: tuple[str, ...] = (), views: int = 0,
                  shared_album: bool = False) -> bytes:
    """A Google Photos Takeout sidecar, laid out like the real thing (2-space JSON)."""
    lat, lon = gps if gps else (0.0, 0.0)
    geo = {"latitude": lat, "longitude": lon, "altitude": 12.5 if gps else 0.0,
           "latitudeSpan": 0.0, "longitudeSpan": 0.0}
    doc: dict = {
        "title": title,
        "description": description,
        "imageViews": str(views),
        "creationTime": {"timestamp": str(created), "formatted": _formatted(created)},
        "photoTakenTime": {"timestamp": str(taken), "formatted": _formatted(taken)},
        "geoData": geo,
        "geoDataExif": dict(geo),
    }
    if people:
        doc["people"] = [{"name": p} for p in people]
    doc["url"] = "https://photos.google.com/photo/" + url_id
    if shared_album:
        doc["googlePhotosOrigin"] = {"fromSharedAlbum": {}}
    elif origin:
        doc["googlePhotosOrigin"] = {"mobileUpload": {
            "deviceFolder": {"localFolderName": origin}, "deviceType": "ANDROID_PHONE"}}
    if favorited:
        doc["favorited"] = True
    if archived:
        doc["archived"] = True
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


def _fake_mp4(rng: np.random.Generator, size: int) -> bytes:
    """Bytes that look like an MP4 to anything sniffing the header; never decoded."""
    head = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    return head + rng.bytes(size - len(head))


# ---------------------------------------------------------------------------------------------
# The fixture model
# ---------------------------------------------------------------------------------------------


def ref(zip_name: str, member: str) -> str:
    """The ``<zip>::<member>`` reference used throughout ``expected.json``."""
    return f"{zip_name}::{member}"


@dataclass
class Item:
    """One library item (one Google Photos url, or one url-less photo)."""

    key: str
    case: str
    filename: str
    url_id: str | None
    members: list[str] = field(default_factory=list)  # image member refs
    junk: set[str] = field(default_factory=set)
    meta: dict = field(default_factory=dict)  # sidecar fields, reused for copies


@dataclass
class _Member:
    name: str
    data: bytes
    zip64: bool = False


class _Builder:
    """Collects members per zip plus the ground truth, then writes both out."""

    def __init__(self, seed: int):
        self.seed = seed
        self.rand = random.Random(seed)
        self._img_counter = 0
        self.zips: dict[str, list[_Member]] = {z: [] for z in ZIP_NAMES}
        self.items: dict[str, Item] = {}
        self.item_keys: dict[str, str] = {}
        self.pairings: dict[str, str | None] = {}
        self.dup_groups: list[dict] = []
        self.must_not: set[tuple[str, str]] = set()
        self.collapsed: list[dict] = []
        self.bursts: list[dict] = []
        self.videos: list[str] = []
        self.video_days: dict[str, int] = {}
        # Year-folder library items with no index row (videos + skipped media with a sidecar),
        # counted once per url on their local day.
        self.unindexed_days: dict[str, int] = {}
        self._unindexed_urls: set[str] = set()
        self.skipped: dict[str, str] = {}
        self._urls: set[str] = set()
        self._clock: dict[int, int] = {}
        self._data: dict[str, bytes] = {}

    # -- randomness ---------------------------------------------------------------------------

    def rng(self) -> np.random.Generator:
        """A fresh generator per image, so adding a case never changes the other images."""
        self._img_counter += 1
        return np.random.default_rng([self.seed, self._img_counter])

    def new_url_id(self) -> str:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        while True:
            uid = "AF1QipFAKE" + "".join(self.rand.choice(alphabet) for _ in range(28))
            if uid not in self._urls:
                self._urls.add(uid)
                return uid

    def moment(self, year: int) -> datetime:
        """Next capture time in ``year``: at least a day apart from every other allocated one,
        so unrelated photos never look like a burst or trip the 60 s capture-time rule."""
        n = self._clock.get(year, 0)
        self._clock[year] = n + 1
        return datetime(year, 1, 2, 9, 0) + timedelta(days=2 * n, hours=n % 11,
                                                      minutes=(7 * n) % 60, seconds=n % 50)

    # -- members --------------------------------------------------------------------------------

    def add_member(self, zip_name: str, name: str, data: bytes, *, zip64: bool = False) -> str:
        mref = ref(zip_name, name)
        assert mref not in self._data, f"duplicate member {mref}"
        self._data[mref] = data
        self.zips[zip_name].append(_Member(name, data, zip64))
        return mref

    def data_of(self, mref: str) -> bytes:
        return self._data[mref]

    def skip(self, zip_name: str, name: str, data: bytes, reason: str) -> None:
        self.skipped[self.add_member(zip_name, name, data)] = reason

    def count_unindexed(self, url_id: str, taken: int) -> None:
        """Count one library item without an index row on its local day (once per url)."""
        if url_id in self._unindexed_urls:
            return
        self._unindexed_urls.add(url_id)
        day = datetime.fromtimestamp(taken, TZ).date().isoformat()
        self.unindexed_days[day] = self.unindexed_days.get(day, 0) + 1

    def add_skipped_media(self, zip_name: str, folder_year: int, filename: str, data: bytes,
                          reason: str, *, taken: int, created: int, **sidecar) -> None:
        """A year-folder file the scanner skips (``raw`` / ``other_image`` / ``too_large``)
        with its sidecar: no index row, yet a library item on its day in Google Photos.

        The sidecar is an orphan for pairing purposes (no image claims it), but the merge
        still counts the item in ``videos_by_day``, and so does ``unindexed_by_day``.
        """
        folder = f"{ROOT}/Photos from {folder_year}"
        self.skip(zip_name, f"{folder}/{filename}", data, reason)
        url_id = self.new_url_id()
        self.add_member(zip_name, f"{folder}/{sidecar_name(filename)}", sidecar_bytes(
            title=filename, taken=taken, created=created, url_id=url_id, **sidecar))
        self.count_unindexed(url_id, taken)

    # -- items ------------------------------------------------------------------------------

    def add_photo(self, case: str, folder_year: int | str, filename: str, data: bytes, *,
                  taken: int, zip_name: str | None = None, json_zip: str | None = None,
                  sidecar: bool = True, json_name: str | None = None, style: str = "new",
                  dup: int | None = None, title: str | None = None,
                  gps: tuple[float, float] | None = None, origin: str | None = "Camera",
                  favorited: bool = False, junk: tuple[str, ...] = (), zip64: bool = False,
                  **meta) -> Item:
        """Add one library item's year-folder (or given-folder) media plus its sidecar.

        ``taken`` is the sidecar's photoTakenTime (UTC epoch). ``json_name`` overrides the
        derived sidecar name; ``dup`` is the "(n)" Takeout added to the media name.
        """
        folder = f"Photos from {folder_year}" if isinstance(folder_year, int) else folder_year
        if zip_name is None:
            year = folder_year if isinstance(folder_year, int) else 2021
            zip_name = ZIP_A1 if year <= 2021 else ZIP_A2
        member = f"{ROOT}/{folder}/{filename}"
        mref = self.add_member(zip_name, member, data, zip64=zip64)
        url_id = self.new_url_id() if sidecar else None
        key = url_id or f"nourl:{filename}"
        assert key not in self.items, key
        item = Item(key=key, case=case, filename=filename, url_id=url_id, junk=set(junk))
        item.members.append(mref)
        self.items[key] = item
        self.item_keys[mref] = key
        self.pairings[mref] = None
        if sidecar:
            plain = title or filename
            name = json_name or sidecar_name(plain, style=style, dup=dup)
            jref = self.add_member(
                json_zip or zip_name, f"{ROOT}/{folder}/{name}",
                sidecar_bytes(title=plain, taken=taken, created=taken + 86400 + 3 * 3600,
                              url_id=url_id, gps=gps, origin=origin, favorited=favorited,
                              views=self.rand.randrange(0, 40), **meta))
            self.pairings[mref] = jref
            item.meta = dict(title=plain, taken=taken, gps=gps, origin=origin,
                             favorited=favorited, **meta)
        return item

    def add_copy(self, item: Item, cid: str, zip_name: str, folder: str, data: bytes, *,
                 sidecar: bool = True) -> str:
        """Another record of the same library item (album copy or a second export)."""
        member = f"{ROOT}/{folder}/{item.filename}"
        mref = self.add_member(zip_name, member, data)
        self.item_keys[mref] = item.key
        self.pairings[mref] = None
        item.members.append(mref)
        if sidecar:
            meta = dict(item.meta)
            taken = meta.pop("taken")
            title = meta.pop("title")
            name = sidecar_name(title)
            jref = self.add_member(zip_name, f"{ROOT}/{folder}/{name}", sidecar_bytes(
                title=title, taken=taken, created=taken + 86400 + 3 * 3600,
                url_id=item.url_id, **meta))
            self.pairings[mref] = jref
        entry = next((c for c in self.collapsed if c["item_key"] == item.key), None)
        if entry is None:
            self.collapsed.append({"id": cid, "item_key": item.key, "copies": []})
        return mref

    def add_video(self, folder: str, filename: str, taken: int, *, zip_name: str = ZIP_A1,
                  album: bool = False) -> None:
        """A fake video (random bytes) with its sidecar; year-folder videos count per day."""
        member = f"{ROOT}/{folder}/{filename}"
        data = _fake_mp4(self.rng(), 300_000 + self.rand.randrange(0, 200_000))
        mref = self.add_member(zip_name, member, data)
        url_id = self.new_url_id()
        self.add_member(zip_name, f"{ROOT}/{folder}/{sidecar_name(filename)}", sidecar_bytes(
            title=filename, taken=taken, created=taken + 7200, url_id=url_id))
        if album:
            self.skipped[mref] = "album_video"
            return
        self.videos.append(mref)
        day = datetime.fromtimestamp(taken, TZ).date().isoformat()
        self.video_days[day] = self.video_days.get(day, 0) + 1
        self.count_unindexed(url_id, taken)

    # -- ground truth -------------------------------------------------------------------------

    def group(self, gid: str, members: list[Item], keeper: Item) -> None:
        """A planted duplicate group; every non-keeper is also a ``dup_extra``."""
        assert keeper in members
        self.dup_groups.append({"id": gid, "members": [m.key for m in members],
                                "keeper": keeper.key})
        for m in members:
            if m is not keeper:
                m.junk.add("dup_extra")

    def burst(self, bid: str, frames: list[Item], best: Item) -> None:
        """A planted burst; every frame but the best is a ``burst_extra``."""
        self.bursts.append({"id": bid, "members": [f.key for f in frames], "best": best.key})
        for f in frames:
            if f is not best:
                f.junk.add("burst_extra")
        self.never_group(frames)

    def never_group(self, items: list[Item]) -> None:
        """Every pair of ``items`` must stay out of any duplicate group."""
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                self.must_not.add(tuple(sorted((a.key, b.key))))

    def expected(self) -> dict:
        for c in self.collapsed:
            c["copies"] = sorted(self.items[c["item_key"]].members)
        return {
            "include_albums": True,
            "item_keys": dict(sorted(self.item_keys.items())),
            "dup_groups": self.dup_groups,
            "must_not_group": [list(p) for p in sorted(self.must_not)],
            "collapsed": self.collapsed,
            "pairings": dict(sorted(self.pairings.items())),
            "bursts": self.bursts,
            "junk": {k: sorted(it.junk) for k, it in sorted(self.items.items()) if it.junk},
            "videos": {"members": sorted(self.videos),
                       "by_day": dict(sorted(self.video_days.items()))},
            "unindexed_by_day": dict(sorted(self.unindexed_days.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "n_items": len(self.items),
        }

    # -- output -----------------------------------------------------------------------------

    def write_zips(self, out: Path) -> None:
        for zip_name, members in self.zips.items():
            with zipfile.ZipFile(out / zip_name, "w", allowZip64=True) as zf:
                for i, m in enumerate(sorted(members, key=lambda m: m.name)):
                    zi = zipfile.ZipInfo(m.name, date_time=_ZIP_TIME[zip_name])
                    zi.create_system = 3  # fixed, so Windows and Linux write the same bytes
                    zi.external_attr = 0o100644 << 16
                    zi.compress_type = _compression(m.name, i)
                    if m.zip64:
                        with zf.open(zi, "w", force_zip64=True) as fh:
                            fh.write(m.data)
                    else:
                        zf.writestr(zi, m.data)


def _compression(name: str, index: int) -> int:
    """Mix STORED and DEFLATED like real exports: text deflated, most media stored."""
    ext = name.rsplit(".", 1)[-1].lower()
    if ext in ("json", "html", "png", "tif"):
        return zipfile.ZIP_DEFLATED
    if ext in ("jpg", "jpeg") and index % 4 == 0:
        return zipfile.ZIP_DEFLATED
    return zipfile.ZIP_STORED


# ---------------------------------------------------------------------------------------------
# Planted cases
# ---------------------------------------------------------------------------------------------

# Points in the open Atlantic: realistic-looking coordinates that locate nobody.
_GPS = [(31.2437, -44.7812), (29.8761, -41.2266), (33.5104, -47.9031), (27.4198, -39.6655),
        (35.0712, -43.3348), (30.6655, -46.1207)]


def _jpeg(im: Image.Image, q: int = 90, exif: bytes = b"") -> bytes:
    return encode(im, "JPEG", quality=q, exif=exif)


def _plant_positives(b: _Builder) -> dict[str, Item]:
    """P1-P12: copies of one picture that must group, with the keeper PLAN would pick."""
    out: dict[str, Item] = {}

    # P1: q95 original vs a q60 copy (Windows-style "(2)" copy name). Same dims, same camera
    # EXIF and GPS -> keeper by larger file.
    when = b.moment(2019)
    img = photo(b.rng(), 2400, 1800)
    ex = camera_exif(CAM_OLD, when, gps=_GPS[0])
    p1a = b.add_photo("P1", 2019, "IMG_1500.jpg", _jpeg(img, 95, ex), taken=utc_ts(when),
                      gps=_GPS[0])
    p1b = b.add_photo("P1", 2019, "IMG_1500 (2).jpg", _jpeg(img, 60, ex),
                      taken=utc_ts(when), gps=_GPS[0])
    b.group("P1", [p1a, p1b], p1a)
    out["P1_copy"] = p1b

    # P2: 4000 px original vs 2048 px export vs a 1600 px WhatsApp copy without EXIF.
    # Keeper by resolution. The original is also in the album (C1b).
    when = b.moment(2021)
    img = photo(b.rng(), 4000, 3000)
    ex = camera_exif(CAM_MAIN, when, offset=True, gps=_GPS[1])
    p2a = b.add_photo("P2", 2021, f"IMG_{when:%Y%m%d_%H%M%S}.jpg", _jpeg(img, 90, ex),
                      taken=utc_ts(when), gps=_GPS[1])
    p2b = b.add_photo("P2", 2021, f"Export_{when:%Y%m%d_%H%M%S}.jpg",
                      _jpeg(img.resize((2048, 1536), Image.Resampling.LANCZOS), 90, ex),
                      taken=utc_ts(when), gps=_GPS[1], origin="Pictures")
    wa_when = b.moment(2021)
    p2c = b.add_photo("P2", 2021, f"IMG-{wa_when:%Y%m%d}-WA0001.jpg",
                      _jpeg(img.resize((1600, 1200), Image.Resampling.LANCZOS), 80),
                      taken=utc_ts(wa_when), origin="WhatsApp Images", junk=("messaging",))
    b.group("P2", [p2a, p2b, p2c], p2a)
    out["P2_keeper"] = p2a

    # P3 (+S2 old-style sidecar): metadata stripped. Keeper: the one with camera EXIF.
    when = b.moment(2019)
    img = photo(b.rng(), 2000, 1500)
    p3a = b.add_photo("P3", 2019, f"IMG_{when:%Y%m%d_%H%M%S}.jpg",
                      _jpeg(img, 90, camera_exif(CAM_OLD, when)), taken=utc_ts(when),
                      style="old")
    p3b = b.add_photo("P3", 2019, f"IMG_{when:%Y%m%d_%H%M%S}_nometa.jpg", _jpeg(img, 90),
                      taken=utc_ts(b.moment(2019)), origin="Pictures")
    b.group("P3", [p3a, p3b], p3a)

    # P4 (+S8): stored landscape with orientation 6 vs a physically rotated copy. Same
    # displayed size and camera EXIF; keeper by GPS (only the original has a location).
    when = b.moment(2020)
    disp = photo(b.rng(), 1500, 2000)
    stored = disp.rotate(90, expand=True)  # orientation 6 turns it back upright
    orig = _jpeg(stored, 90, camera_exif(CAM_MAIN, when, orientation=6, gps=_GPS[2]))
    base = f"IMG_{when:%Y%m%d_%H%M%S}"
    p4a = b.add_photo("P4", 2020, f"{base}.jpg", orig, taken=utc_ts(when), gps=_GPS[2])
    p4b = b.add_photo("P4", 2020, f"{base}_rotated.jpg",
                      _jpeg(disp, 90, camera_exif(CAM_MAIN, when, orientation=1)),
                      taken=utc_ts(when))
    b.group("P4", [p4a, p4b], p4a)
    # S8: the edited copy has no sidecar and is skipped; the original keeps its JSON.
    edited = _jpeg(disp.filter(ImageFilter.SMOOTH), 88)
    b.skip(ZIP_A1, f"{ROOT}/Photos from 2020/{base}-edited.jpg", edited, "edited")

    # P5 (+S9 case mismatch): PNG vs JPEG. Keeper: the JPEG, which has camera EXIF.
    when = b.moment(2020)
    img = photo(b.rng(), 1600, 1200)
    p5a = b.add_photo("P5", 2020, "IMG_4500.JPG", _jpeg(img, 92, camera_exif(CAM_OLD, when)),
                      taken=utc_ts(when), json_name=sidecar_name("IMG_4500.jpg"))
    p5b = b.add_photo("P5", 2020, "IMG_4500.png", encode(img, "PNG"),
                      taken=utc_ts(b.moment(2020)), origin="Pictures")
    b.group("P5", [p5a, p5b], p5a)

    # P6: HEIC in Display P3 (camera EXIF) vs its sRGB JPEG export vs a smaller sRGB HEIC.
    # Keeper: resolution ties the first two, camera EXIF picks the P3 HEIC.
    when = b.moment(2023)
    img = photo(b.rng(), 1600, 1200)
    p3img, icc = to_display_p3(img)
    heic_p3 = encode(p3img, "HEIF", quality=85, icc_profile=icc,
                     exif=camera_exif(CAM_MAIN, when, offset=True))
    p6a = b.add_photo("P6", 2023, "IMG_3100.HEIC", heic_p3, taken=utc_ts(when))
    p6b = b.add_photo("P6", 2023, "IMG_3100.jpg", _jpeg(img, 90),
                      taken=utc_ts(b.moment(2023)), origin="Pictures")
    small = img.resize((1200, 900), Image.Resampling.LANCZOS)
    p6c = b.add_photo("P6", 2023, "IMG_3100_small.HEIC", encode(small, "HEIF", quality=80),
                      taken=utc_ts(b.moment(2023)), origin="Pictures")
    b.group("P6", [p6a, p6b, p6c], p6a)

    # P7: byte-identical photo under two urls; everything ties except favorited.
    when = b.moment(2020)
    data = _jpeg(photo(b.rng(), 2000, 1500), 90, camera_exif(CAM_MAIN, when, gps=_GPS[3]))
    base = f"IMG_{when:%Y%m%d_%H%M%S}"
    p7a = b.add_photo("P7", 2020, f"{base}.jpg", data, taken=utc_ts(when), gps=_GPS[3])
    p7b = b.add_photo("P7", 2020, f"{base}~2.jpg", data, taken=utc_ts(when),
                      gps=_GPS[3], favorited=True)
    b.group("P7", [p7a, p7b], p7b)

    # P7b: byte-identical screenshot (graphic: groups by exact SHA only). The copy got "(1)"
    # and a truncated "(1)" sidecar; keeper by earlier photoTakenTime.
    when = b.moment(2020)
    shot = encode(chat_screenshot(b.rng(), list(_CHAT[:5]), clock="9:15", contact="Alex"),
                  "PNG")
    name = f"Screenshot_{when:%Y%m%d-%H%M%S}.png"
    s7a = b.add_photo("P7b", 2020, name, shot, taken=utc_ts(when), origin="Screenshots",
                      junk=("screenshot",))
    s7b = b.add_photo("P7b", 2020, name.replace(".png", "(1).png"), shot,
                      taken=utc_ts(when) + 2 * 86400, origin="Screenshots", title=name, dup=1,
                      junk=("screenshot",))
    b.group("P7b", [s7a, s7b], s7a)
    out["P7b_shot"] = s7a

    # P8: a triple. Original (written as the forced-ZIP64 member), a q70 copy without EXIF,
    # a half-size copy with EXIF. Keeper: resolution, then camera EXIF.
    when = b.moment(2020)
    img = photo(b.rng(), 2400, 1800)
    ex = camera_exif(CAM_MAIN, when)
    base = f"IMG_{when:%Y%m%d_%H%M%S}"
    p8a = b.add_photo("P8", 2020, f"{base}.jpg", _jpeg(img, 92, ex), taken=utc_ts(when),
                      zip64=True)
    p8b = b.add_photo("P8", 2020, f"{base}-1.jpg", _jpeg(img, 70),
                      taken=utc_ts(b.moment(2020)), origin="Pictures")
    p8c = b.add_photo("P8", 2020, f"{base}_small.jpg",
                      _jpeg(img.resize((1200, 900), Image.Resampling.LANCZOS), 85, ex),
                      taken=utc_ts(when))
    b.group("P8", [p8a, p8b, p8c], p8a)

    # P9: one photo in all three zips (both parts of export A and export B), different urls.
    when = b.moment(2020)
    img = photo(b.rng(), 3000, 2000)
    ex = camera_exif(CAM_DSLR, when)
    p9a = b.add_photo("P9", 2020, "DSC_0042.JPG", _jpeg(img, 92, ex), taken=utc_ts(when))
    p9b = b.add_photo("P9", 2020, "DSC_0042_web.jpg",
                      _jpeg(img.resize((1500, 1000), Image.Resampling.LANCZOS), 85, ex),
                      taken=utc_ts(when), zip_name=ZIP_A2)
    p9c = b.add_photo("P9", 2020, "DSC_0042_print.jpg",
                      _jpeg(img.resize((2000, 1333), Image.Resampling.LANCZOS), 60),
                      taken=utc_ts(b.moment(2020)), zip_name=ZIP_B1, origin="Pictures")
    b.group("P9", [p9a, p9b, p9c], p9a)

    # P10 (+S4 ".supplemental-metada"): a motion photo (JPEG + trailing MP4) vs its still.
    when = b.moment(2023)
    img = photo(b.rng(), 2000, 1500)
    ex = camera_exif(CAM_MAIN, when, subsec="123", offset=True)
    motion = _jpeg(img, 90, ex) + _fake_mp4(b.rng(), 150_000)
    stamp = f"PXL_{when:%Y%m%d_%H%M%S}123"
    p10a = b.add_photo("P10", 2023, f"{stamp}.MP.jpg", motion, taken=utc_ts(when))
    p10b = b.add_photo("P10", 2023, f"{stamp}.jpg",
                       _jpeg(img.resize((1600, 1200), Image.Resampling.LANCZOS), 88, ex),
                       taken=utc_ts(when))
    b.group("P10", [p10a, p10b], p10a)

    # P11: a copy whose DateTimeOriginal was shifted by exactly one hour (time-zone fix).
    # Same JPEG stream and same-length EXIF, so the file sizes tie; keeper = earlier taken.
    when = b.moment(2021)
    img = photo(b.rng(), 1800, 1350)
    later = when + timedelta(hours=1)
    p11a = b.add_photo("P11", 2021, f"IMG_{when:%Y%m%d_%H%M%S}.jpg",
                       _jpeg(img, 90, camera_exif(CAM_MAIN, when, gps=_GPS[4])),
                       taken=utc_ts(when), gps=_GPS[4])
    p11b = b.add_photo("P11", 2021, f"IMG_{later:%Y%m%d_%H%M%S}.jpg",
                       _jpeg(img, 90, camera_exif(CAM_MAIN, later, gps=_GPS[4])),
                       taken=utc_ts(later), gps=_GPS[4])
    b.group("P11", [p11a, p11b], p11a)

    # P12: re-save keeping identical EXIF including SubSec (capture-time delta is exactly 0).
    when = b.moment(2022)
    img = photo(b.rng(), 2000, 1500)
    ex = camera_exif(CAM_MAIN, when, subsec="347", offset=True)
    p12a = b.add_photo("P12", 2022, f"IMG_{when:%Y%m%d_%H%M%S}.jpg", _jpeg(img, 92, ex),
                       taken=utc_ts(when))
    p12b = b.add_photo("P12", 2022, f"IMG_{when:%Y%m%d_%H%M%S}_resaved.jpg",
                       _jpeg(img, 80, ex), taken=utc_ts(when))
    b.group("P12", [p12a, p12b], p12a)
    return out


def _plant_collapses(b: _Builder, pos: dict[str, Item]) -> None:
    """C1-C3: several records of one library item; they must collapse, never group."""
    # C1: year-folder photo also in an album, same url, own JSON in the album folder.
    when = b.moment(2021)
    data = _jpeg(photo(b.rng(), 2000, 1500), 90, camera_exif(CAM_MAIN, when))
    c1 = b.add_photo("C1", 2021, f"IMG_{when:%Y%m%d_%H%M%S}.jpg", data, taken=utc_ts(when))
    b.add_copy(c1, "C1", ZIP_A1, ALBUM, data)
    # C1b: the P2 keeper is in the album too (its album copy must not join the P2 group).
    p2 = pos["P2_keeper"]
    b.add_copy(p2, "C1b", ZIP_A1, ALBUM, b.data_of(p2.members[0]))
    b.skip(ZIP_A1, f"{ROOT}/{ALBUM}/metadata.json", json.dumps(
        {"title": ALBUM, "description": "", "access": "protected",
         "date": {"timestamp": "1626300000", "formatted": _formatted(1626300000)}},
        indent=2).encode(), "non_sidecar_json")

    # C2: double export. A plain photo, and the P1 copy (a dup-group member), re-exported in B.
    when = b.moment(2022)
    data = _jpeg(photo(b.rng(), 2000, 1500), 90, camera_exif(CAM_MAIN, when))
    # Archived, so --small has an archived item; add_copy reuses the metadata, so both
    # exports agree on it.
    c2 = b.add_photo("C2", 2022, f"IMG_{when:%Y%m%d_%H%M%S}.jpg", data, taken=utc_ts(when),
                     zip_name=ZIP_A1, archived=True)
    b.add_copy(c2, "C2", ZIP_B1, "Photos from 2022", data)
    p1c = pos["P1_copy"]
    b.add_copy(p1c, "C2b", ZIP_B1, "Photos from 2019", b.data_of(p1c.members[0]))

    # C3: re-exported in B, but B has no sidecar for it: collapses by (sha, name, date).
    when = b.moment(2023)
    data = _jpeg(photo(b.rng(), 2000, 1500), 90, camera_exif(CAM_MAIN, when, offset=True))
    c3 = b.add_photo("C3", 2023, f"IMG_{when:%Y%m%d_%H%M%S}.jpg", data, taken=utc_ts(when))
    b.add_copy(c3, "C3", ZIP_B1, "Photos from 2023", data, sidecar=False)


def _plant_sidecar_cases(b: _Builder) -> None:
    """S3, S4 (46-char old style), S5, S7, S11 on distinct photos (other S cases ride along
    on the P/N/junk items: S2 P3, S4 P10/junk, S6 N5, S8 P4, S9 P5, S10 N3)."""
    # S3 (+S2): IMG_1234.jpg and IMG_1234(1).jpg are *different* photos (old-style names).
    items = []
    for dup in (None, 1):
        when = b.moment(2018)
        fname = "IMG_1234.jpg" if dup is None else "IMG_1234(1).jpg"
        items.append(b.add_photo(
            "S3", 2018, fname, _jpeg(photo(b.rng(), 1600, 1200), 90,
                                     camera_exif(CAM_OLD, when)),
            taken=utc_ts(when), style="old", dup=dup, title="IMG_1234.jpg"))
    b.never_group(items)

    # S4: a name over 46 characters with an old-style sidecar cut to 46 characters.
    when = b.moment(2019)
    b.add_photo("S4", 2019, "Family reunion at the lake house summer evening.jpg",
                _jpeg(photo(b.rng(), 1800, 1200), 90, camera_exif(CAM_OLD, when)),
                taken=utc_ts(when), style="old")

    # S5: two long names with the same 46-character prefix -> "X.json" and "X(1).json";
    # only title and time tell them apart.
    pair = []
    for suffix, dup in (("A", None), ("B", 1)):
        when = b.moment(2018)
        fname = f"Holiday dinner with the whole family at grandmas {suffix}.jpg"
        pair.append(b.add_photo(
            "S5", 2018, fname, _jpeg(photo(b.rng(), 1600, 1200), 90,
                                     camera_exif(CAM_OLD, when)),
            taken=utc_ts(when), json_name=sidecar_name(fname, dup=dup)))
    b.never_group(pair)

    # S7: a camera photo with no sidecar at all (EXIF date, no url).
    when = b.moment(2018)
    b.add_photo("S7", 2018, f"IMG_{when:%Y%m%d_%H%M%S}.jpg",
                _jpeg(photo(b.rng(), 1600, 1200), 90, camera_exif(CAM_OLD, when)),
                taken=utc_ts(when), sidecar=False)

    # S11: a sidecar name no naming rule derives; only its title identifies the photo.
    when = b.moment(2020)
    fname = f"IMG_{when:%Y%m%d_%H%M%S}.jpg"
    b.add_photo("S11", 2020, fname,
                _jpeg(photo(b.rng(), 1600, 1200), 90, camera_exif(CAM_MAIN, when)),
                taken=utc_ts(when), json_name=fname + ".metadata.json")


def _plant_negatives(b: _Builder, pos: dict[str, Item]) -> None:
    """N1-N6: look-alikes that must never group."""
    # N1: six screenshots of the same chat template, different text (graphic -> SHA only).
    shots = []
    for i in range(6):
        when = b.moment(2021)
        msgs = [_CHAT[(i * 3 + k) % len(_CHAT)] for k in range(4 + i % 3)]
        im = chat_screenshot(b.rng(), msgs, clock=f"{when:%H:%M}", contact="Alex")
        suffix = "_Messages" if i % 2 else ""
        shots.append(b.add_photo("N1", 2021, f"Screenshot_{when:%Y%m%d-%H%M%S}{suffix}.png",
                                 encode(im, "PNG"), taken=utc_ts(when), origin="Screenshots",
                                 junk=("screenshot",)))
    b.never_group(shots + [pos["P7b_shot"]])

    # N2: black pocket shots, a solid grey frame and a blank wall, all with camera EXIF.
    frames = []
    specs = [((7, 7, 9), 3.0, 0.0, ("dark", "pocket")), ((6, 6, 7), 3.0, 0.0, ("dark", "pocket")),
             ((122, 124, 121), 1.5, 0.0, ("pocket",)), ((236, 232, 225), 1.5, 5.0, ())]
    for colour, noise, grad, junk in specs:
        when = b.moment(2022)
        im = flat_frame(b.rng(), 2000, 1500, colour, noise=noise, gradient=grad)
        frames.append(b.add_photo("N2", 2022, f"IMG_{when:%Y%m%d_%H%M%S}.jpg",
                                  _jpeg(im, 90, camera_exif(CAM_MAIN, when)),
                                  taken=utc_ts(when), junk=junk))
    b.never_group(frames)

    # N3 (+S10 extension-less sidecar): same luma, opposite chroma.
    warm, cold = same_luma_hues(b.rng(), 1600, 1200)
    hues = []
    for fname, im in (("IMG_5100.jpg", warm), ("IMG_5101.jpg", cold)):
        when = b.moment(2023)
        hues.append(b.add_photo("N3", 2023, fname, _jpeg(im, 92, camera_exif(CAM_DSLR, when)),
                                taken=utc_ts(when),
                                json_name="IMG_5100.json" if fname == "IMG_5100.jpg" else None))
    b.never_group(hues)

    # N4: burst frames 1 s apart with SubSec; a small subject moves ~3%, the last frame is
    # blurred. The pixels are near-identical (they pass pHash and the signature), so only
    # the capture-time rule keeps them apart.
    when = b.moment(2022)
    bg = photo(b.rng(), 2000, 1500)
    frames = []
    for k, (x, blur) in enumerate(((0.40, 0.0), (0.43, 2.5), (0.43, 7.0))):
        t = when + timedelta(seconds=k)
        im = with_subject(bg, x, blur=blur, scale=0.25)
        frames.append(b.add_photo(
            "N4", 2022, f"IMG_{t:%Y%m%d_%H%M%S}.jpg",
            _jpeg(im, 90, camera_exif(CAM_MAIN, t, subsec=f"{120 + 7 * k}", offset=True)),
            taken=utc_ts(t)))
    b.burst("N4", frames, frames[0])

    # N4b: same second, no SubSec (capture-time rule cannot help): frames differ visibly.
    # With equal capture times the merge may order the frames any way it likes, so *every*
    # pair (not just neighbours) must stay within the burst pHash limit: the figure moves
    # only 1% per frame. A thrown ball crossing the frame keeps the signature check from
    # matching any pair (measured over 16 seeds: pHash <= 6, signature misses by > 2x).
    when = b.moment(2023)
    bg = photo(b.rng(), 2000, 1500)
    frames = []
    for k, (x, blur, ball) in enumerate(((0.40, 0.0, (0.66, 0.30)), (0.41, 3.0, (0.74, 0.22)),
                                         (0.42, 6.0, (0.82, 0.30)))):
        im = with_subject(bg, x, blur=blur, ball=ball)
        suffix = "" if k == 0 else f"_{k}"
        frames.append(b.add_photo(
            "N4b", 2023, f"IMG_{when:%Y%m%d_%H%M%S}{suffix}.jpg",
            _jpeg(im, 90, camera_exif(CAM_MAIN, when)), taken=utc_ts(when)))
    b.burst("N4b", frames, frames[0])

    # N5 (+S6 cross-zip sidecar): a 10% crop (aspect changes) and a same-aspect 90% zoom.
    # The edited copies carry no EXIF, so pixels alone must keep them apart.
    when = b.moment(2024)
    img = photo(b.rng(), 2400, 1600, shapes=18)
    w, h = img.size
    crop = img.crop((0, 0, int(w * 0.9), h))
    zoom = img.crop((int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.95))).resize(
        (w, h), Image.Resampling.LANCZOS)
    base = f"IMG_{when:%Y%m%d_%H%M%S}"
    n5 = [b.add_photo("N5", 2024, f"{base}.jpg", _jpeg(img, 90, camera_exif(CAM_MAIN, when)),
                      taken=utc_ts(when), zip_name=ZIP_A1),
          b.add_photo("N5", 2024, f"{base}_crop.jpg", _jpeg(crop, 90),
                      taken=utc_ts(b.moment(2024)), zip_name=ZIP_A1, json_zip=ZIP_A2,
                      origin="Pictures"),
          b.add_photo("N5", 2024, f"{base}_zoom.jpg", _jpeg(zoom, 90),
                      taken=utc_ts(b.moment(2024)), origin="Pictures")]
    b.never_group(n5)

    # N6: photographed document pages, one to two minutes apart, different text.
    when = b.moment(2024)
    pages = []
    for k in range(3):
        rng = b.rng()
        lines = [_words(rng, int(rng.integers(4, 9))) for _ in range(22)]
        t = when + timedelta(seconds=95 * k)
        pages.append(b.add_photo(
            "N6", 2024, f"IMG_{t:%Y%m%d_%H%M%S}.jpg",
            _jpeg(document_photo(rng, lines), 90, camera_exif(CAM_MAIN, t)),
            taken=utc_ts(t)))
    b.never_group(pages)


def _plant_junk(b: _Builder) -> None:
    """Blur, dark, overexposed, tiny, messaging names, filename bursts, a Pixel RAW pair."""
    # Blur (+S4 ".suppl": a 40-character name).
    when = b.moment(2022)
    im = photo(b.rng(), 2000, 1500).filter(ImageFilter.GaussianBlur(14))
    b.add_photo("junk", 2022, f"PXL_{when:%Y%m%d_%H%M%S}123.LONG_EXPOSURE.jpg",
                _jpeg(im, 90, camera_exif(CAM_MAIN, when)), taken=utc_ts(when),
                junk=("blur",))

    # Dark (+S4 ".supplemental-metada": a 26-character PXL name).
    when = b.moment(2021)
    im = photo(b.rng(), 2000, 1500)
    im = Image.fromarray((np.asarray(im, np.float32) * 0.07 + 2).astype(np.uint8), "RGB")
    b.add_photo("junk", 2021, f"PXL_{when:%Y%m%d_%H%M%S}456.jpg",
                _jpeg(im, 90, camera_exif(CAM_MAIN, when)), taken=utc_ts(when),
                junk=("dark",))

    # Overexposed (+S4 ".s": a 44-character name).
    when = b.moment(2022)
    im = photo(b.rng(), 2000, 1500)
    im = Image.fromarray((255 - (255 - np.asarray(im, np.float32)) * 0.1).astype(np.uint8),
                         "RGB")
    b.add_photo("junk", 2022, f"PXL_{when:%Y%m%d_%H%M%S}789.PORTRAIT.ORIGINAL.jpg",
                _jpeg(im, 90, camera_exif(CAM_MAIN, when)), taken=utc_ts(when),
                junk=("overexposed",))

    # Tiny: a 200x150 web thumbnail, no EXIF.
    b.add_photo("junk", 2019, "thumbnail_0042.jpg", _jpeg(photo(b.rng(), 200, 150), 85),
                taken=utc_ts(b.moment(2019)), origin="Pictures", junk=("tiny",))

    # Messaging apps: names and origin folders, no camera EXIF.
    for year, fname, origin, (w, h) in (
            (2020, "received_1234567890123456.jpeg", "Messenger", (1280, 960)),
            (2021, "FB_IMG_1612345678901.jpg", "Facebook", (1200, 1600)),
            (2022, "signal-2022-05-05-101010.jpg", "Signal", (1600, 1200))):
        b.add_photo("junk", year, fname, _jpeg(photo(b.rng(), w, h), 80),
                    taken=utc_ts(b.moment(year)), origin=origin, junk=("messaging",))

    # Filename burst: _BURST000_COVER is the sharp one; SubSec differs, subject moves.
    when = b.moment(2022)
    bg = photo(b.rng(), 1800, 1350)
    frames = []
    for k, (x, blur) in enumerate(((0.35, 0.0), (0.38, 4.0), (0.41, 6.0))):
        cover = "_COVER" if k == 0 else ""
        frames.append(b.add_photo(
            "burst", 2022, f"IMG_{when:%Y%m%d_%H%M%S}_BURST{k:03d}{cover}.jpg",
            _jpeg(with_subject(bg, x, blur=blur), 90,
                  camera_exif(CAM_MAIN, when, subsec=f"{100 + 300 * k}")),
            taken=utc_ts(when)))
    b.burst("BURST", frames, frames[0])

    # Pixel RAW pair: the COVER jpg is a normal item; the .dng is skipped (its sidecar stays
    # an orphan and must not be claimed by the cover).
    when = b.moment(2023)
    stamp = f"PXL_{when:%Y%m%d_%H%M%S}321"
    b.add_photo("raw", 2023, f"{stamp}.RAW-01.MP.COVER.jpg",
                _jpeg(photo(b.rng(), 2000, 1500), 90, camera_exif(CAM_MAIN, when)),
                taken=utc_ts(when))
    b.add_skipped_media(ZIP_A2, 2023, f"{stamp}.RAW-02.ORIGINAL.dng",
                        b"II*\x00" + b.rng().bytes(40_000), "raw", taken=utc_ts(when),
                        created=utc_ts(when) + 3600)


def _plant_fillers(b: _Builder, n: int) -> None:
    """N7: distinct photos in assorted formats and sizes (plus a few fixed special ones)."""
    # Fixed ones, present in --small too: WebP, animated GIF, a non-ASCII name, a
    # shared-album item, and one that only exists in export B.
    when = b.moment(2019)
    b.add_photo("N7", 2019, "Überraschung im Café.jpg",
                _jpeg(photo(b.rng(), 1600, 1200), 90, camera_exif(CAM_OLD, when)),
                taken=utc_ts(when), description="Kaffee und Kuchen", people=(_PEOPLE[0],))
    b.add_photo("N7", 2021, "sunset_share.webp",
                encode(photo(b.rng(), 1600, 1200), "WEBP", quality=80),
                taken=utc_ts(b.moment(2021)), shared_album=True)
    rng = b.rng()
    bg = photo(rng, 1200, 900, grain=0)
    gif_frames = [with_subject(bg, 0.3 + 0.2 * k).quantize(128) for k in range(3)]
    buf = io.BytesIO()
    gif_frames[0].save(buf, "GIF", save_all=True, append_images=gif_frames[1:], duration=200,
                       loop=0)
    b.add_photo("N7", 2022, "animation_0001.gif", buf.getvalue(),
                taken=utc_ts(b.moment(2022)), origin="Pictures")
    when = b.moment(2025)
    b.add_photo("N7", 2025, f"IMG_{when:%Y%m%d_%H%M%S}.jpg",
                _jpeg(photo(b.rng(), 1600, 1200), 90, camera_exif(CAM_MAIN, when)),
                taken=utc_ts(when), zip_name=ZIP_B1)

    fillers = []
    # Photo shapes only (4:3, 3:4, 3:2, 2:3; long edge >= 1280): nothing here may look like a
    # screen capture or a thumbnail, or the junk expectations would stop being exhaustive.
    # 1600x1200 and 1280x960 are also classic display sizes, which the screenshot score has to
    # tolerate anyway (see the module docstring).
    sizes = [(1600, 1200), (1200, 1600), (2000, 1500), (1800, 1200), (1200, 1800),
             (1280, 960), (2400, 1800), (960, 1280), (1500, 1000)]
    for i in range(n):
        year = 2018 + i % 8
        when = b.moment(year)
        w, h = sizes[b.rand.randrange(len(sizes))] if i % 25 else (4000, 3000)
        img = photo(b.rng(), w, h)
        gps = None
        if i % 10 == 7:  # a few without camera EXIF (4:3 or 3:2, never screen-shaped)
            data, name, origin = _jpeg(img, 85), f"image_{i:04d}.jpg", "Pictures"
        else:
            cam = (CAM_MAIN, CAM_OLD, CAM_DSLR)[i % 3]
            gps = _GPS[i % len(_GPS)] if i % 3 == 0 else None
            data = _jpeg(img, 88, camera_exif(cam, when, gps=gps))
            name, origin = f"IMG_{when:%Y%m%d_%H%M%S}.jpg", "Camera"
        extra: dict = {}
        if i % 17 == 3:
            extra["people"] = (_PEOPLE[i % len(_PEOPLE)],)
        if i % 29 == 5:
            extra["archived"] = True
        fillers.append(b.add_photo("N7", year, name, data, taken=utc_ts(when), origin=origin,
                                   gps=gps, favorited=(i % 23 == 11), **extra))
    # Adjacent pairs are enough to name N7 explicitly; exact group equality covers the rest.
    for a, c in zip(fillers, fillers[1:]):
        b.must_not.add(tuple(sorted((a.key, c.key))))


def _plant_videos_and_misc(b: _Builder) -> None:
    """Fake videos (counted per local day, never read) and members that must be skipped."""
    b.add_video("Photos from 2021", "VID_20210716_110000.mp4",
                utc_ts(datetime(2021, 7, 16, 11, 0, 0)))
    b.add_video(ALBUM, "VID_20210716_110000.mp4", utc_ts(datetime(2021, 7, 16, 11, 0, 0)),
                album=True)
    b.add_video("Photos from 2022", "VID_20220601_101010.mp4",
                utc_ts(datetime(2022, 6, 1, 10, 10, 10)))
    b.add_video("Photos from 2022", "VID_20220601_183000.mp4",
                utc_ts(datetime(2022, 6, 1, 18, 30, 0)))
    # Pixel names are UTC: 02:30 UTC on Jul 4 is the evening of Jul 3 in New York.
    b.add_video("Photos from 2022", "PXL_20220704_023000123.mp4",
                int(datetime(2022, 7, 4, 2, 30, tzinfo=UTC).timestamp()))
    # 04:30 UTC on the DST-change day is still 23:30 EST on Mar 11 (a naive -4 h says Mar 12).
    b.add_video("Photos from 2023", "VID_20230311_233000.mov",
                int(datetime(2023, 3, 12, 4, 30, tzinfo=UTC).timestamp()), zip_name=ZIP_A2)

    # Google Photos' own trash folder: skipped whatever it holds.
    b.skip(ZIP_A1, f"{ROOT}/Trash/IMG_0001.jpg",
           _jpeg(photo(b.rng(), 640, 480), 85, camera_exif(CAM_MAIN, datetime(2021, 5, 5, 10))),
           "trash")
    b.skip(ZIP_A1, "Takeout/archive_browser.html",
           b"<!doctype html><title>Takeout</title><p>Your data export</p>", "outside_photos")
    b.skip(ZIP_B1, "Takeout/archive_browser.html",
           b"<!doctype html><title>Takeout</title><p>Your data export</p>", "outside_photos")
    b.skip(ZIP_A2, f"{ROOT}/user-generated-memory-titles.json", b'{"titles": []}',
           "non_sidecar_json")
    # A scanner TIFF: skipped (no index row) but counted on its day; its sidecar is an orphan.
    b.add_skipped_media(ZIP_A1, 2018, "scan_0001.tif",
                        encode(photo(b.rng(), 400, 300, grain=0), "TIFF"), "other_image",
                        taken=utc_ts(datetime(2018, 3, 3, 12)),
                        created=utc_ts(datetime(2018, 3, 4, 12)), origin=None)


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------


def generate(out: Path, *, seed: int = 0, small: bool = False) -> dict:
    """Write the three fixture zips and ``expected.json`` into ``out``; return the expected dict.

    ``small`` skips the ~300 N7 filler photos (fast tests). The planted cases are identical in
    both modes, and output bytes depend only on ``seed`` (and the library versions).
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    pillow_heif.register_heif_opener()
    b = _Builder(seed)
    pos = _plant_positives(b)
    _plant_collapses(b, pos)
    _plant_sidecar_cases(b)
    _plant_negatives(b, pos)
    _plant_junk(b)
    _plant_videos_and_misc(b)
    _plant_fillers(b, 0 if small else N_FILLERS_DEFAULT)
    b.write_zips(out)
    expected = b.expected()
    (out / "expected.json").write_text(
        json.dumps(expected, indent=1, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8")
    log.info("fixtures: items=%d members=%d groups=%d", expected["n_items"],
             len(expected["item_keys"]), len(expected["dup_groups"]))
    return expected


def _in_album(ref_: str) -> bool:
    """True for a member that sits in an album folder rather than a "Photos from YYYY" one."""
    folder = ref_.split("::", 1)[1].split("/")[2]
    return not re.fullmatch(r"Photos from \d{4}", folder)


def expected_view(expected: dict, *, include_albums: bool) -> dict:
    """``expected`` as a run with ``include_albums`` should see it.

    ``expected.json`` describes ``include_albums=true``. Without albums, album photos are
    skipped (``album_media``) instead of being copies; since every one of them is a copy of a
    year-folder photo, the library items, groups, bursts and junk stay the same. So do
    ``videos`` and ``unindexed_by_day``: album videos are ``album_video`` skips either way, and
    only year-folder media is counted per day.
    """
    out = copy.deepcopy(expected)
    if include_albums:
        return out
    album = [r for r in out["item_keys"] if _in_album(r)]
    for r in album:
        del out["item_keys"][r]
        del out["pairings"][r]
        out["skipped"][r] = "album_media"
    out["skipped"] = dict(sorted(out["skipped"].items()))
    for c in out["collapsed"]:
        c["copies"] = [r for r in c["copies"] if not _in_album(r)]
    out["collapsed"] = [c for c in out["collapsed"] if len(c["copies"]) > 1]
    out["include_albums"] = False
    return out


def cli_generate(out: Path, seed: int = 0, small: bool = False) -> int:
    """``gpclean fixtures --out DIR [--seed N] [--small]``."""
    expected = generate(Path(out), seed=seed, small=small)
    print(f"wrote {len(ZIP_NAMES)} zips + expected.json to {out}: "
          f"{expected['n_items']} items, {len(expected['item_keys'])} image members, "
          f"{len(expected['dup_groups'])} duplicate groups, {len(expected['bursts'])} bursts")
    return 0
