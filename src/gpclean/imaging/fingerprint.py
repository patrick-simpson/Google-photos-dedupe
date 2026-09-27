"""Content fingerprints: sha256, a 64-bit perceptual hash, and the verification signature.

pHash finds candidate near-duplicates cheaply (Hamming distance on 64 bits). It is known to
collide on screenshots, documents and near-blank images, so every candidate pair must also
pass :func:`sig_verify`, which compares a small luma thumbnail block by block plus coarse
chroma (PLAN section 5, "Duplicate grouping").
"""

from __future__ import annotations

import hashlib

import numpy as np
from PIL import Image

from gpclean.config import MergeConfig
from gpclean.version import SIG_VERSION

_HASH_N = 32  # pHash input is a 32x32 luma image
_HASH_K = 8  # keep the 8x8 lowest-frequency DCT coefficients (including DC)

_SIG_LUMA = 32  # signature luma grid
_SIG_CHROMA = 8  # signature chroma grid
_SIG_BLOCK = 8  # block size (in signature pixels) for the local-difference check
SIG_BYTES = _SIG_LUMA * _SIG_LUMA + 2 * _SIG_CHROMA * _SIG_CHROMA  # 1152 for SIG_VERSION 1

if SIG_VERSION != 1:  # the byte layout below is SIG_VERSION 1; a bump needs new code
    raise ImportError("gpclean.imaging.fingerprint implements SIG_VERSION 1 only")

_U64 = 1 << 64
_MASK64 = _U64 - 1


def _dct_matrix(n: int) -> np.ndarray:
    """Orthonormal DCT-II matrix D, so that D @ x is the DCT of column vector x."""
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    d = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    d[0, :] = np.sqrt(1.0 / n)
    return d


# Precomputed once: the 2-D DCT of X is D @ X @ D.T (no scipy needed).
_DCT = _dct_matrix(_HASH_N)


def sha256(data: bytes) -> bytes:
    """Raw 32-byte SHA-256 of the file bytes (exact-duplicate key)."""
    return hashlib.sha256(data).digest()


def phash64(work: Image.Image) -> int:
    """64-bit DCT perceptual hash of the work image, as a signed int64 (SQLite INTEGER).

    Luma -> 32x32 (LANCZOS, aspect ignored) -> 2-D DCT-II -> top-left 8x8 including DC ->
    bit i is set when coefficient i (row-major, bit 0 = [0,0]) exceeds the median of the 64.
    """
    luma = work.convert("L").resize((_HASH_N, _HASH_N), Image.Resampling.LANCZOS)
    x = np.asarray(luma, dtype=np.float64)
    coeffs = (_DCT @ x @ _DCT.T)[:_HASH_K, :_HASH_K].ravel()
    bits = coeffs > np.median(coeffs)
    value = 0
    for i in np.flatnonzero(bits):
        value |= 1 << int(i)
    # Store as signed so it fits SQLite's 64-bit INTEGER.
    return value - _U64 if value >= (1 << 63) else value


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two pHashes (signed or unsigned representation)."""
    return ((a ^ b) & _MASK64).bit_count()


def signature(work: Image.Image) -> bytes:
    """SIG_VERSION 1 verification signature, 1152 bytes.

    1024 B: 32x32 BOX-averaged luma (uint8, row-major), then 64 B Cb and 64 B Cr on an 8x8
    BOX grid (JPEG-style YCbCr). BOX averaging makes it insensitive to resampling and
    compression noise while keeping real content differences.
    """
    rgb = work if work.mode == "RGB" else work.convert("RGB")
    luma = rgb.convert("L").resize((_SIG_LUMA, _SIG_LUMA), Image.Resampling.BOX)
    ycc = rgb.convert("YCbCr").resize((_SIG_CHROMA, _SIG_CHROMA), Image.Resampling.BOX)
    _, cb, cr = ycc.split()
    return luma.tobytes() + cb.tobytes() + cr.tobytes()


def _split_sig(sig: bytes) -> tuple[np.ndarray, np.ndarray]:
    if len(sig) != SIG_BYTES:
        raise ValueError(f"signature must be {SIG_BYTES} bytes, got {len(sig)}")
    arr = np.frombuffer(sig, dtype=np.uint8).astype(np.int16)
    n = _SIG_LUMA * _SIG_LUMA
    return arr[:n].reshape(_SIG_LUMA, _SIG_LUMA), arr[n:]


def sig_distance(a: bytes, b: bytes) -> tuple[float, float, float]:
    """(global luma MAD, max 8x8-block luma MAD, max abs Cb/Cr difference) between two sigs.

    The block term catches differences confined to one region (e.g. different text in a
    chat bubble) that the global mean would dilute.
    """
    la, ca = _split_sig(a)
    lb, cb = _split_sig(b)
    diff = np.abs(la - lb).astype(np.float64)
    mad = float(diff.mean())
    g = _SIG_LUMA // _SIG_BLOCK
    blocks = diff.reshape(g, _SIG_BLOCK, g, _SIG_BLOCK).mean(axis=(1, 3))
    block_max = float(blocks.max())
    chroma_max = float(np.abs(ca - cb).max())
    return mad, block_max, chroma_max


def sig_verify(a: bytes, b: bytes, cfg: MergeConfig) -> bool:
    """True when two signatures are close enough to be the same picture (all three limits)."""
    mad, block_max, chroma_max = sig_distance(a, b)
    return (
        mad <= cfg.sig_mad_max
        and block_max <= cfg.sig_block_max
        and chroma_max <= cfg.sig_chroma_max
    )


def sig_luma_std(sig: bytes) -> float:
    """Standard deviation of the signature's 32x32 luma; low values flag near-blank images."""
    luma, _ = _split_sig(sig)
    return float(luma.std())
