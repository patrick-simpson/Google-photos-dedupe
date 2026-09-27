"""Tests for gpclean.imaging (decode, fingerprint, features, thumbs, process_image).

Every image here is synthetic: seeded fractal-like value noise (so pHash has real texture to
work with, unlike flat colours), rendered text, or tiny generated frames.
"""

from __future__ import annotations

import io
import sqlite3
import struct
import zlib

import numpy as np
import pytest
from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageFont, UnidentifiedImageError

from gpclean.config import MergeConfig, ScanConfig
from gpclean.imaging import (
    configure_pillow,
    decode,
    features,
    hamming,
    phash64,
    process_image,
    sha256,
    sig_distance,
    sig_luma_std,
    sig_verify,
    signature,
    thumbs,
)
from gpclean.imaging.thumbs import webp_metadata_chunks
from gpclean.schema import SHARD_DDL

MCFG = MergeConfig()
SCFG = ScanConfig()

configure_pillow()


# ---------------------------------------------------------------------------------------------
# Synthetic image helpers
# ---------------------------------------------------------------------------------------------


def fractal(seed: int, w: int, h: int, *, base: int = 1000, grain: float = 4.0) -> Image.Image:
    """Seeded multi-octave value noise in RGB (1/f-like), plus a little per-pixel grain.

    Built at ``base`` px wide and upscaled, so a 4000x3000 image stays cheap to make.
    """
    rng = np.random.default_rng(seed)
    bw = min(base, w)
    bh = max(2, round(bw * h / w))
    acc = np.zeros((bh, bw, 3))
    amp, total = 1.0, 0.0
    for octave in range(1, 9):
        gw = 2**octave + 1
        gh = max(2, round((2**octave) * h / w) + 1)
        grid = rng.random((gh, gw, 3)).astype(np.float32)
        up = np.stack(
            [
                np.asarray(Image.fromarray(grid[..., c], "F").resize((bw, bh), Image.BICUBIC))
                for c in range(3)
            ],
            axis=-1,
        )
        acc += amp * up
        total += amp
        amp *= 0.6
    acc /= total
    acc = (acc - acc.min()) / (acc.max() - acc.min()) * 255.0
    im = Image.fromarray(acc.astype(np.uint8), "RGB")
    if (w, h) != (bw, bh):
        im = im.resize((w, h), Image.BICUBIC)
    if grain:
        noisy = np.asarray(im).astype(np.int16) + rng.normal(0, grain, (h, w, 3)).astype(np.int16)
        im = Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8), "RGB")
    return im


def encode(im: Image.Image, fmt: str = "JPEG", **params) -> bytes:
    """Encode ``im`` to bytes in ``fmt``."""
    buf = io.BytesIO()
    im.save(buf, format=fmt, **params)
    return buf.getvalue()


def camera_exif(orientation: int | None = None, *, gps: tuple | None = None) -> bytes:
    """EXIF block as a phone camera writes it (tags in their proper IFDs)."""
    ex = Image.Exif()
    ex[271] = "FakeCam"
    ex[272] = "Model X"
    ex[306] = "2021:06:01 12:00:05"
    if orientation is not None:
        ex[0x0112] = orientation
    sub = ex.get_ifd(0x8769)
    sub[36867] = "2021:06:01 12:00:00"
    sub[36881] = "-04:00"
    sub[37521] = "123"
    sub[33434] = 0.008
    if gps is not None:
        g = ex.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = gps
    return ex.tobytes()


def full_decode_phash(data: bytes) -> int:
    """pHash via a full-resolution decode (no draft), used to measure draft drift."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        scale = 640 / max(im.size)
        work = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    return phash64(work)


def p3_profile_bytes() -> bytes:
    """A minimal ICC v2 display profile with Display P3 primaries and gamma 2.2 curves.

    ImageCms can only create sRGB/LAB/XYZ profiles, so this builds the file by hand:
    header + desc/wtpt/rXYZ/gXYZ/bXYZ/r/g/bTRC/cprt tags, colorants Bradford-adapted to D50.
    """

    def xy_to_xyz(x: float, y: float) -> np.ndarray:
        return np.array([x / y, 1.0, (1 - x - y) / y])

    prim = np.stack([xy_to_xyz(0.680, 0.320), xy_to_xyz(0.265, 0.690), xy_to_xyz(0.150, 0.060)], 1)
    white65 = xy_to_xyz(0.3127, 0.3290)
    m = prim * np.linalg.solve(prim, white65)  # RGB -> XYZ (D65)
    brad = np.array(
        [[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]]
    )
    d50 = np.array([0.9642, 1.0, 0.8249])
    adapt = np.linalg.inv(brad) @ np.diag((brad @ d50) / (brad @ white65)) @ brad
    m50 = adapt @ m

    def s15(v: float) -> bytes:
        return struct.pack(">i", round(v * 65536))

    def xyz_tag(v) -> bytes:
        return b"XYZ " + b"\0" * 4 + b"".join(s15(float(c)) for c in v)

    def text_desc(s: str) -> bytes:
        a = s.encode("ascii") + b"\0"
        return (
            b"desc" + b"\0" * 4 + struct.pack(">I", len(a)) + a
            + b"\0" * 8  # unicode language code + count
            + b"\0" * 3 + b"\0" * 67  # scriptcode code, count, 67-byte string
        )

    curv = b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", round(2.2 * 256))
    tags = [
        (b"desc", text_desc("Synthetic Display P3")),
        (b"wtpt", xyz_tag(d50)),
        (b"rXYZ", xyz_tag(m50[:, 0])),
        (b"gXYZ", xyz_tag(m50[:, 1])),
        (b"bXYZ", xyz_tag(m50[:, 2])),
        (b"rTRC", curv),
        (b"gTRC", curv),
        (b"bTRC", curv),
        (b"cprt", b"text" + b"\0" * 4 + b"public domain test\0"),
    ]
    table = struct.pack(">I", len(tags))
    body = b""
    offset = 128 + 4 + 12 * len(tags)
    for sig, data in tags:
        while (offset + len(body)) % 4:
            body += b"\0"
        table += sig + struct.pack(">II", offset + len(body), len(data))
        body += data
    size = 128 + len(table) + len(body)
    header = (
        struct.pack(">I", size) + b"lcms" + struct.pack(">I", 0x02100000)
        + b"mntr" + b"RGB " + b"XYZ " + b"\0" * 12 + b"acsp" + b"\0" * 4
        + b"\0" * 4 + b"\0" * 8 + b"\0" * 8 + struct.pack(">I", 0)
        + b"".join(s15(float(c)) for c in d50) + b"\0" * 4 + b"\0" * 16 + b"\0" * 28
    )
    assert len(header) == 128
    return header + table + body


def screenshot(messages: list[str]) -> Image.Image:
    """A phone chat screenshot: status bar, coloured app bar, bubbles sized by their text."""
    im = Image.new("RGB", (1080, 2340), (236, 229, 221))
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=44)
    d.rectangle([0, 0, 1080, 80], fill=(7, 94, 84))
    d.rectangle([0, 80, 1080, 240], fill=(18, 140, 126))
    d.text((140, 130), "Chat", fill=(255, 255, 255), font=font)
    d.rectangle([0, 2200, 1080, 2340], fill=(255, 255, 255))
    y = 300
    for i, msg in enumerate(messages):
        tw = int(d.textlength(msg, font=font))
        right = i % 2 == 1
        x0 = 1040 - tw - 60 if right else 40
        colour = (220, 248, 198) if right else (255, 255, 255)
        d.rounded_rectangle([x0, y, x0 + tw + 60, y + 110], radius=24, fill=colour)
        d.text((x0 + 30, y + 30), msg, fill=(20, 20, 20), font=font)
        y += 150
    return im


# ---------------------------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def big_photo() -> Image.Image:
    """A 4000x3000 textured 'photo'."""
    return fractal(1, 4000, 3000)


@pytest.fixture(scope="module")
def photo() -> Image.Image:
    """A smaller textured photo for tests that do not need 12 MP."""
    return fractal(7, 1200, 900)


@pytest.fixture
def restore_limits():
    """Put the default pixel limit back after a test lowers it."""
    yield
    configure_pillow()


def all_pairs_close(datas: list[bytes], max_hamming: int = 2) -> None:
    decs = [decode(d) for d in datas]
    hashes = [phash64(x.work) for x in decs]
    sigs = [signature(x.work) for x in decs]
    for i in range(len(datas)):
        for j in range(i + 1, len(datas)):
            assert hamming(hashes[i], hashes[j]) <= max_hamming, (i, j)
            assert sig_verify(sigs[i], sigs[j], MCFG), (i, j, sig_distance(sigs[i], sigs[j]))


# ---------------------------------------------------------------------------------------------
# Near-duplicates must match
# ---------------------------------------------------------------------------------------------


def test_resized_recompressed_and_stripped_copies_match(big_photo):
    original = encode(big_photo, quality=92, exif=camera_exif())
    resized = encode(big_photo.resize((2048, 1536), Image.LANCZOS), quality=90, exif=camera_exif())
    with Image.open(io.BytesIO(original)) as im:
        recompressed = encode(im.convert("RGB"), quality=60)
    whatsapp = encode(big_photo.resize((1600, 1200), Image.LANCZOS), quality=80)  # no EXIF
    all_pairs_close([original, resized, recompressed, whatsapp])

    dec = decode(original)
    assert (dec.width, dec.height) == (4000, 3000)
    assert (dec.stored_w, dec.stored_h) == (4000, 3000)
    assert dec.work.size == (640, 480)
    assert decode(whatsapp).exif["has_camera_exif"] is False


def test_draft_vs_full_decode_drift(big_photo):
    for q in (92, 70):
        data = encode(big_photo, quality=q)
        assert hamming(phash64(decode(data).work), full_decode_phash(data)) <= 2


def test_draft_keeps_long_edge_at_work_edge():
    # 5000x1000 panorama: draft may only shrink while both dims stay >= requested.
    data = encode(fractal(3, 5000, 1000), quality=85)
    dec = decode(data)
    assert dec.work.size == (640, 128)
    assert (dec.width, dec.height) == (5000, 1000)


def test_png_vs_jpeg(photo):
    all_pairs_close([encode(photo, "PNG"), encode(photo, quality=85)])
    assert decode(encode(photo, "PNG")).format == "PNG"


def test_webp_vs_jpeg(photo):
    all_pairs_close([encode(photo, "WEBP", quality=80), encode(photo, quality=85)])


def test_heic_vs_jpeg(photo):
    heic = encode(photo, "HEIF", quality=85)
    dec = decode(heic)
    assert dec.format == "HEIF"
    assert dec.orientation == 1
    all_pairs_close([heic, encode(photo, quality=90)])


def test_heic_orientation_is_not_applied_twice(photo):
    # pillow-heif applies the rotation itself; decode must report orientation 1 and the
    # already-rotated dimensions, and the pixels must match a physically rotated JPEG.
    heic = encode(photo, "HEIF", quality=90, exif=camera_exif(orientation=6))
    dec = decode(heic)
    assert dec.orientation == 1
    assert (dec.width, dec.height) == (900, 1200)
    rotated = encode(photo.transpose(Image.Transpose.ROTATE_270), quality=90)
    all_pairs_close([heic, rotated])


def test_mpo_decodes_primary_frame(photo):
    second = fractal(99, 1200, 900)
    data = encode(photo, "MPO", save_all=True, append_images=[second], quality=90)
    dec = decode(data)
    assert dec.format == "MPO"
    assert dec.animated is False
    all_pairs_close([data, encode(photo, quality=90)])


# ---------------------------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------------------------


def test_orientation_tag_vs_physical_rotation(big_photo):
    sensor = big_photo.resize((3000, 2000), Image.LANCZOS)
    tagged = encode(sensor, quality=90, exif=camera_exif(orientation=6))
    physical = encode(sensor.transpose(Image.Transpose.ROTATE_270), quality=90, exif=camera_exif())
    a, b = decode(tagged), decode(physical)
    assert a.orientation == 6 and b.orientation == 1
    assert (a.width, a.height) == (b.width, b.height) == (2000, 3000)
    assert (a.stored_w, a.stored_h) == (3000, 2000)
    assert a.work.size == b.work.size == (427, 640)
    # Tag 6 and physically rotated pixels must agree exactly: any slack here would hide an
    # off-by-one in the transpose table or resize rounding that differs between the paths.
    assert phash64(a.work) == phash64(b.work)
    assert sig_distance(signature(a.work), signature(b.work))[0] < 0.5
    assert sig_verify(signature(a.work), signature(b.work), MCFG)


# The transform that turns the *displayed* image into what a camera stores for each tag
# (the inverse of what decode applies).
_STORE_FOR_TAG = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}


@pytest.mark.parametrize("tag", range(1, 9))
def test_all_eight_orientations(photo, tag):
    displayed = photo.resize((800, 600), Image.LANCZOS)
    op = _STORE_FOR_TAG[tag]
    stored = displayed if op is None else displayed.transpose(op)
    dec = decode(encode(stored, "PNG", exif=camera_exif(orientation=tag)))
    assert dec.orientation == tag
    assert (dec.width, dec.height) == (800, 600)
    expected = displayed.resize((640, 480), Image.LANCZOS)
    assert hamming(phash64(dec.work), phash64(expected)) <= 1
    assert sig_verify(signature(dec.work), signature(expected), MCFG)


def test_bogus_orientation_value_is_ignored(photo):
    dec = decode(encode(photo, quality=90, exif=camera_exif(orientation=42)))
    assert dec.orientation == 1
    assert (dec.width, dec.height) == photo.size


# ---------------------------------------------------------------------------------------------
# Colour management
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def p3_case():
    """(sRGB image, same colours re-encoded as Display P3 pixel values, P3 profile bytes)."""
    srgb = fractal(11, 1200, 900, grain=2.0)
    # Boost saturation so the P3 vs sRGB difference is large enough to matter.
    hsv = np.asarray(srgb.convert("HSV")).copy()
    hsv[..., 1] = np.clip(hsv[..., 1].astype(np.int16) * 3 + 60, 0, 255).astype(np.uint8)
    srgb = Image.fromarray(hsv, "HSV").convert("RGB")
    p3 = p3_profile_bytes()
    to_p3 = ImageCms.buildTransform(
        ImageCms.createProfile("sRGB"), ImageCms.ImageCmsProfile(io.BytesIO(p3)), "RGB", "RGB"
    )
    return srgb, ImageCms.applyTransform(srgb, to_p3), p3


def test_display_p3_jpeg_is_converted_to_srgb(p3_case):
    srgb, p3_pixels, p3 = p3_case
    reference = signature(decode(encode(srgb, quality=92)).work)
    converted = decode(encode(p3_pixels, quality=92, icc_profile=p3))
    assert sig_verify(signature(converted.work), reference, MCFG)
    assert converted.work.info == {}
    # Without the profile the same pixels are visibly desaturated: the conversion matters.
    unconverted = signature(decode(encode(p3_pixels, quality=92)).work)
    assert sig_distance(unconverted, reference)[2] > MCFG.sig_chroma_max


def test_display_p3_heic_vs_jpeg(p3_case):
    srgb, p3_pixels, p3 = p3_case
    heic = encode(p3_pixels, "HEIF", quality=90, icc_profile=p3)
    with Image.open(io.BytesIO(heic)) as im:
        assert im.info.get("icc_profile"), "pillow-heif did not keep the ICC profile"
    all_pairs_close([heic, encode(srgb, quality=92)])


def test_embedded_srgb_profile_is_harmless(photo):
    srgb_icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    all_pairs_close([encode(photo, quality=90, icc_profile=srgb_icc), encode(photo, quality=90)])


def test_broken_icc_profile_is_ignored(photo):
    data = encode(photo, quality=90, icc_profile=b"definitely not an ICC profile" * 4)
    dec = decode(data)
    assert dec.work.mode == "RGB"
    all_pairs_close([data, encode(photo, quality=90)])


# ---------------------------------------------------------------------------------------------
# Modes and formats
# ---------------------------------------------------------------------------------------------


def test_16bit_grayscale_png():
    ramp = (np.arange(300 * 200, dtype=np.uint32).reshape(200, 300) * 65535 // (300 * 200 - 1))
    im16 = Image.fromarray(ramp.astype(np.uint16))
    data = encode(im16, "PNG")
    with Image.open(io.BytesIO(data)) as check:
        assert check.mode.startswith("I")
    dec = decode(data)
    assert dec.work.mode == "RGB" and dec.work.size == (300, 200)
    got = np.asarray(dec.work.convert("L")).astype(int)
    want = np.rint(ramp / 257.0).astype(int)
    assert np.abs(got - want).max() <= 1  # scaled, not clipped at 255


def test_palette_png_with_transparency_flattens_to_white():
    im = Image.new("P", (100, 80), 0)
    im.putpalette([0, 0, 0, 200, 30, 30] + [0] * 762)
    ImageDraw.Draw(im).rectangle([50, 0, 99, 79], fill=1)
    data = encode(im, "PNG", transparency=0)
    dec = decode(data)
    assert dec.work.mode == "RGB"
    assert dec.work.getpixel((10, 40)) == (255, 255, 255)  # transparent -> white
    r, g, b = dec.work.getpixel((80, 40))
    assert r > 150 and g < 80 and b < 80


def test_rgba_png_flattens_alpha_on_white(photo):
    rgba = photo.resize((400, 300)).convert("RGBA")
    alpha = Image.new("L", rgba.size, 255)
    ImageDraw.Draw(alpha).rectangle([0, 0, 199, 299], fill=0)
    rgba.putalpha(alpha)
    dec = decode(encode(rgba, "PNG"))
    px = np.asarray(dec.work)
    assert (px[:, :190] == 255).all()
    assert px[:, 210:].std() > 10


def test_la_and_1bit_png():
    la = Image.new("LA", (64, 64), (0, 255))
    ImageDraw.Draw(la).rectangle([0, 0, 31, 63], fill=(0, 0))
    dec = decode(encode(la, "PNG"))
    assert dec.work.getpixel((5, 5)) == (255, 255, 255)
    assert dec.work.getpixel((50, 5)) == (0, 0, 0)
    bw = Image.new("1", (64, 64), 1)
    assert decode(encode(bw, "PNG")).work.getpixel((3, 3)) == (255, 255, 255)


def test_cmyk_and_grayscale_jpeg(photo):
    cmyk = decode(encode(photo.convert("CMYK"), quality=92))
    assert cmyk.work.mode == "RGB" and cmyk.format == "JPEG"
    all_pairs_close([encode(photo.convert("CMYK"), quality=92), encode(photo, quality=92)])
    gray = decode(encode(photo.convert("L"), quality=90))
    assert gray.work.mode == "RGB" and gray.work.size == (640, 480)


def test_animated_gif_uses_frame_zero():
    frames = [Image.new("RGB", (120, 90), c) for c in ((250, 10, 10), (10, 250, 10), (10, 10, 250))]
    data = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    dec = decode(data)
    assert dec.animated is True and dec.format == "GIF"
    r, g, b = dec.work.getpixel((60, 45))
    assert r > 200 and g < 50 and b < 50
    still = decode(encode(frames[1], "GIF"))
    assert still.animated is False


def test_animated_webp_uses_frame_zero():
    frames = [Image.new("RGB", (120, 90), c) for c in ((10, 250, 10), (250, 10, 10))]
    data = encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], lossless=True)
    dec = decode(data)
    assert dec.animated is True
    r, g, b = dec.work.getpixel((60, 45))
    assert g > 200 and r < 50


@pytest.mark.parametrize(
    "data",
    [
        b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n",
        encode(Image.new("RGB", (32, 32), (1, 2, 3)), "BMP"),
        encode(Image.new("RGB", (32, 32), (1, 2, 3)), "TIFF"),
        encode(Image.new("RGB", (32, 32), (1, 2, 3)), "PPM"),
        b"",
        b"\x00" * 1000,
        b"not an image at all",
    ],
    ids=["eps", "bmp", "tiff", "ppm", "empty", "zeros", "text"],
)
def test_disallowed_or_unknown_formats_are_rejected(data):
    with pytest.raises(UnidentifiedImageError):
        decode(data)


def _corrupt_idat(png: bytes, *, fix_crc: bool) -> bytes:
    """Flip 20 bytes in the middle of the first IDAT chunk's compressed body.

    Pillow does not verify IDAT CRCs, so both variants reach the zlib/filter decoder; with
    ``fix_crc`` the file is also valid at the chunk level, exactly as a bit-rotted file
    re-saved by a careless tool would be.
    """
    out = bytearray(png)
    pos = 8  # skip the PNG signature
    while True:
        (length,) = struct.unpack(">I", out[pos : pos + 4])
        if out[pos + 4 : pos + 8] == b"IDAT":
            break
        pos += 12 + length
    body = pos + 8
    start = body + length // 2
    for i in range(start, start + 20):
        out[i] ^= 0x5A
    if fix_crc:
        crc = zlib.crc32(b"IDAT" + bytes(out[body : body + length]))
        out[body + length : body + length + 4] = struct.pack(">I", crc)
    return bytes(out)


def _broken_inputs(photo: Image.Image) -> dict[str, bytes]:
    jpeg = encode(photo, quality=90)
    png = encode(photo, "PNG")
    # A PNG without EXIF, like every screenshot: PngImageFile.getexif() then loads the
    # pixels itself, which is the path that used to swallow IDAT corruption.
    plain_png = encode(photo.resize((800, 600)), "PNG")
    heic = encode(photo, "HEIF", quality=80)
    broken_ihdr = bytearray(png)
    broken_ihdr[18] ^= 0xFF  # width byte inside IHDR: chunk CRC no longer matches
    return {
        "jpeg-half": jpeg[: len(jpeg) // 2],
        "jpeg-header-only": jpeg[:600],
        "png-half": png[: len(png) // 2],
        "heic-half": heic[: len(heic) // 2],
        "png-bad-ihdr": bytes(broken_ihdr),
        "jpeg-bad-markers": jpeg[:2] + b"\x13" * 64 + jpeg[66:],
        "png-bad-idat": _corrupt_idat(plain_png, fix_crc=False),
        "png-bad-idat-crc-fixed": _corrupt_idat(plain_png, fix_crc=True),
    }


@pytest.mark.parametrize(
    "case", ["jpeg-half", "jpeg-header-only", "png-half", "heic-half", "png-bad-ihdr",
             "jpeg-bad-markers", "png-bad-idat", "png-bad-idat-crc-fixed"]
)
def test_truncated_and_corrupt_data_raise(photo, case):
    # Every failure surfaces as OSError, so the scan's per-item handler sees one type.
    with pytest.raises(OSError):
        decode(_broken_inputs(photo)[case])


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_decompression_bomb_is_rejected(restore_limits):
    small = encode(Image.new("RGB", (1000, 1000), (9, 9, 9)), "PNG")
    big = encode(Image.new("RGB", (1001, 1000), (9, 9, 9)), "PNG")
    configure_pillow(max_pixels=1_000_000)
    assert decode(small).width == 1000  # exactly at the limit is fine
    with pytest.raises(Image.DecompressionBombError):
        decode(big)
    # The check uses the stored size, so JPEG draft cannot sneak a bomb past it.
    with pytest.raises(Image.DecompressionBombError):
        decode(encode(Image.new("RGB", (2000, 1000), (9, 9, 9)), quality=50))


# ---------------------------------------------------------------------------------------------
# EXIF
# ---------------------------------------------------------------------------------------------


def test_exif_fields_extracted(photo):
    gps = ("S", (33.0, 51.0, 36.0), "E", (151.0, 12.0, 0.0))
    dec = decode(encode(photo, quality=90, exif=camera_exif(gps=gps)))
    ex = dec.exif
    assert ex["dt"] == "2021:06:01 12:00:00"
    assert ex["offset"] == "-04:00"
    assert ex["subsec"] == "123"
    assert ex["dt_any"] == "2021:06:01 12:00:05"
    assert ex["make"] == "FakeCam" and ex["model"] == "Model X"
    assert ex["has_camera_exif"] is True
    assert ex["lat"] == pytest.approx(-33.86)
    assert ex["lon"] == pytest.approx(151.2)


def test_exif_gps_zero_zero_is_none_and_no_exif_is_empty(photo):
    gps = ("N", (0.0, 0.0, 0.0), "E", (0.0, 0.0, 0.0))
    ex = decode(encode(photo, quality=90, exif=camera_exif(gps=gps))).exif
    assert ex["lat"] is None and ex["lon"] is None
    bare = decode(encode(photo, quality=90)).exif
    assert set(bare) == {"dt", "offset", "subsec", "dt_any", "make", "model",
                         "has_camera_exif", "lat", "lon"}
    assert bare["has_camera_exif"] is False
    assert all(bare[k] is None for k in bare if k != "has_camera_exif")


def test_exposure_time_alone_counts_as_camera_exif(photo):
    ex = Image.Exif()
    ex.get_ifd(0x8769)[33434] = 0.01
    assert decode(encode(photo, quality=90, exif=ex.tobytes())).exif["has_camera_exif"] is True


def test_garbage_exif_does_not_break_decoding(photo):
    data = encode(photo, quality=90, exif=b"Exif\x00\x00" + b"\xde\xad\xbe\xef" * 20)
    dec = decode(data)
    assert dec.orientation == 1 and dec.work.size == (640, 480)


# ---------------------------------------------------------------------------------------------
# Different images must not match
# ---------------------------------------------------------------------------------------------


def test_different_textures_do_not_match():
    decs = [decode(encode(fractal(seed, 1600, 1200), quality=85)) for seed in (21, 22, 23)]
    for i in range(3):
        for j in range(i + 1, 3):
            assert hamming(phash64(decs[i].work), phash64(decs[j].work)) > 10
            assert not sig_verify(signature(decs[i].work), signature(decs[j].work), MCFG)


def text_screen(lines: list[str]) -> Image.Image:
    """A notes/document app screenshot: dark app bar, then lines of large black text."""
    im = Image.new("RGB", (1080, 2340), (255, 255, 255))
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=60)
    d.rectangle([0, 0, 1080, 200], fill=(30, 30, 30))
    y = 260
    for line in lines:
        d.text((40, y), line, fill=(0, 0, 0), font=font)
        y += 90
    return im


def _words(seed: int, n: int) -> list[str]:
    vocab = ("the quick brown fox jumps over a lazy dog meeting notes budget plan review "
             "ticket email call later").split()
    rng = np.random.default_rng(seed)
    return [" ".join(rng.choice(vocab, rng.integers(2, 6))) for _ in range(n)]


def test_same_template_screenshots_with_different_text_fail_verify():
    a, b = text_screen(_words(1, 25)), text_screen(_words(2, 25))
    wa, wb = decode(encode(a, "PNG")).work, decode(encode(b, "PNG")).work
    sa, sb = signature(wa), signature(wb)
    assert not sig_verify(sa, sb, MCFG), sig_distance(sa, sb)
    # ...while a lossy re-encode of the same screenshot still verifies.
    assert sig_verify(sa, signature(decode(encode(a, quality=80)).work), MCFG)


def test_chat_screenshots_rely_on_the_graphic_guard():
    """Known limit: chat bubbles are low-contrast, so a one-word change is invisible to both
    pHash and sig at 32x32. PLAN's is_graphic rule (flat_frac >= 0.35, or screenshot score,
    or sig luma std < 8) keeps such images out of near-duplicate grouping; this checks the
    feature it depends on is reliably high for screenshots."""
    msgs = ["Hey, are we still on for tonight?", "Yes! 7pm at the usual place",
            "Great, see you there", "Bring the tickets please"]
    other = list(msgs)
    other[2] = "Great, see you soon"
    for im in (screenshot(msgs), screenshot(other)):
        assert features(decode(encode(im, "PNG")).work)["flat_frac"] >= 0.35


def test_same_luma_different_hue_fails_verify(photo):
    # Swap red and blue: luma changes little, chroma a lot.
    r, g, b = photo.split()
    swapped = Image.merge("RGB", (b, g, r))
    sa = signature(decode(encode(photo, quality=90)).work)
    sb = signature(decode(encode(swapped, quality=90)).work)
    assert not sig_verify(sa, sb, MCFG)


# ---------------------------------------------------------------------------------------------
# Fingerprint primitives
# ---------------------------------------------------------------------------------------------


def test_phash_is_signed_int64_with_dc_bit(photo):
    h = phash64(photo)
    assert -(2**63) <= h < 2**63
    assert h & 1  # DC coefficient is always above the median for non-negative luma
    assert hamming(h, h) == 0
    assert hamming(0, -1) == 64
    assert hamming(1, 3) == 1


def test_sha256_is_raw_digest():
    assert sha256(b"abc").hex() == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_signature_layout_and_helpers(photo):
    sig = signature(photo)
    assert len(sig) == 1152
    flat = signature(Image.new("RGB", (100, 100), (128, 128, 128)))
    assert sig_luma_std(flat) == 0.0
    assert sig_luma_std(sig) > 8
    assert sig_distance(sig, sig) == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        sig_distance(sig, sig[:-1])


def test_block_mad_catches_local_change(photo):
    base = photo.resize((640, 480))
    edited = base.copy()
    # Blank out a quarter of one 8x8 signature block (80x60 px of the 640x480 image).
    ImageDraw.Draw(edited).rectangle([0, 0, 79, 59], fill=(0, 0, 0))
    mad, block, _ = sig_distance(signature(base), signature(edited))
    assert mad <= MCFG.sig_mad_max < block


# ---------------------------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------------------------


def test_features_values(photo):
    work = decode(encode(photo, quality=90)).work
    f = features(work)
    assert set(f) == {"lap_var", "luma_mean", "luma_std", "luma_p02", "luma_p98",
                      "frac_dark", "frac_bright", "flat_frac", "colorfulness"}
    assert all(isinstance(v, float) for v in f.values())
    assert 0 <= f["luma_p02"] <= f["luma_mean"] <= f["luma_p98"] <= 255

    blurred = features(work.filter(ImageFilter.GaussianBlur(4)))
    assert blurred["lap_var"] < f["lap_var"] / 4

    black = features(Image.new("RGB", (640, 480), (5, 5, 5)))
    assert black["frac_dark"] == 1.0 and black["flat_frac"] == 1.0 and black["lap_var"] == 0.0
    white = features(Image.new("RGB", (640, 480), (252, 252, 252)))
    assert white["frac_bright"] == 1.0
    gray = features(work.convert("L").convert("RGB"))
    assert gray["colorfulness"] == pytest.approx(0.0, abs=1e-9)
    assert f["colorfulness"] > 10


# ---------------------------------------------------------------------------------------------
# Thumbnails and process_image
# ---------------------------------------------------------------------------------------------


def _assert_clean_webp(data: bytes, long_edge: int) -> None:
    assert not webp_metadata_chunks(data)
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "WEBP"
        assert max(im.size) == long_edge
        assert not im.info.get("exif") and not im.info.get("icc_profile")
        assert not im.info.get("xmp")
        assert not im.getexif()


def test_thumbs_sizes_and_no_metadata(photo):
    gps = ("N", (40.0, 26.0, 46.0), "W", (79.0, 58.0, 56.0))
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'
    data = encode(photo, quality=90, exif=camera_exif(gps=gps),
                  icc_profile=p3_profile_bytes(), xmp=xmp)
    dec = decode(data)
    dec.work.info.update({"exif": camera_exif(gps=gps), "icc_profile": b"x", "xmp": xmp})
    grid, preview = thumbs(dec.work, SCFG)
    _assert_clean_webp(grid, SCFG.thumb_grid_px)
    _assert_clean_webp(preview, SCFG.thumb_preview_px)
    with Image.open(io.BytesIO(grid)) as im:
        assert im.size == (160, 120)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_passthrough_work_image_is_metadata_free(fmt):
    # Small RGB with no ICC/alpha/rotation needs no conversion; decode must still hand back
    # a plain Image, not the opened file object whose getexif() would expose GPS.
    gps = ("N", (40.0, 26.0, 46.0), "W", (79.0, 58.0, 56.0))
    small = fractal(3, 120, 100, base=120)
    dec = decode(encode(small, fmt, exif=camera_exif(gps=gps)))
    assert dec.exif["lat"] is not None  # the source really carried GPS
    assert type(dec.work) is Image.Image
    assert not dict(dec.work.getexif())
    assert dec.work.info == {}


def test_thumbs_never_upscale():
    tiny = Image.new("RGB", (100, 50), (40, 90, 200))
    grid, preview = thumbs(tiny, SCFG)
    for data in (grid, preview):
        with Image.open(io.BytesIO(data)) as im:
            assert im.size == (100, 50)


def test_webp_metadata_detector():
    ex = camera_exif()
    tagged = encode(Image.new("RGB", (20, 20)), "WEBP", exif=ex)
    assert b"EXIF" in webp_metadata_chunks(tagged)
    assert webp_metadata_chunks(b"garbage") == {b"????"}


_CALLER_COLUMNS = {"member_idx", "member", "folder", "folder_kind", "filename", "ext",
                   "file_size", "crc32", "emb", "err"}


def test_process_image_row_matches_items_raw(photo):
    data = encode(photo, quality=90, exif=camera_exif(orientation=6))
    row, grid, preview, work = process_image(data, SCFG)

    conn = sqlite3.connect(":memory:")
    conn.executescript(SHARD_DDL)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(items_raw)")}
    assert set(row) == columns - _CALLER_COLUMNS

    # The row binds straight into the table once the caller adds its fields.
    full = dict(row, member_idx=0, member="Takeout/Google Photos/Photos from 2021/a.jpg",
                folder="Photos from 2021", folder_kind="year", filename="a.jpg", ext="jpg",
                file_size=len(data), crc32=0)
    names = ",".join(full)
    conn.execute(f"INSERT INTO items_raw ({names}) VALUES ({','.join('?' * len(full))})",
                 list(full.values()))
    stored = conn.execute("SELECT phash64, sig, sha256, width, height FROM items_raw").fetchone()
    assert stored == (row["phash64"], row["sig"], sha256(data), 900, 1200)

    assert row["format"] == "JPEG" and row["orientation"] == 6
    assert (row["stored_w"], row["stored_h"]) == (1200, 900)
    assert row["has_camera_exif"] == 1 and row["animated"] == 0
    assert row["exif_dt"] == "2021:06:01 12:00:00"
    assert work.mode == "RGB" and work.size == (480, 640) and work.info == {}
    assert row["phash64"] == phash64(work) and row["sig"] == signature(work)
    _assert_clean_webp(grid, 160)
    _assert_clean_webp(preview, 640)
