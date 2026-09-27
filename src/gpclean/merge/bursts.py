"""Bursts: several near-identical shots taken within a moment; keep the best, flag the rest.

- **Filename bursts:** frames the camera named as one burst (``*_BURSTnnn[_COVER]``,
  ``PXL_*.RAW-nn.*``), gathered by :func:`gpclean.takeout.names.burst_key` within one
  folder.
- **Time bursts:** non-graphic items sorted by capture time; consecutive items within
  ``burst_window_s`` seconds, pHash within ``burst_phash_max`` and the same camera model (when
  both are known) are chained. A duplicate group counts once, through its keeper, and frames
  already in a filename burst are not reported again.

The best shot is the sharpest (highest ``lap_var``) among frames that are neither mostly black
nor blown out, then the higher resolution. Every other frame gets ``burst_extra`` 0.8.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from gpclean.config import MergeConfig
from gpclean.merge.group import hamming64
from gpclean.merge.localtime import subsec_fraction
from gpclean.takeout.names import burst_key

BURST_EXTRA = 0.8
_DARK_MAX = 0.5  # frac_dark at or above this: not eligible as best shot
_BRIGHT_MAX = 0.2  # frac_bright at or above this: not eligible as best shot


@dataclass
class Burst:
    """One burst. ``members`` are row indices in rank order (best first)."""

    source: str  # filename | time
    members: list[int]
    start_ts: int | None
    end_ts: int | None
    duration_s: float
    quality: dict[int, float] = field(default_factory=dict)

    @property
    def best(self) -> int:
        return self.members[0]


def _time(row: dict) -> float | None:
    ts = row.get("taken_ts")
    if ts is None:
        return None
    return float(ts) + (subsec_fraction(row.get("exif_subsec")) or 0.0)


def _lap(row: dict) -> float:
    return float(row.get("lap_var") or 0.0)


def _rank(members: list[int], rows: list[dict]) -> list[int]:
    """Best frame first, then the others by sharpness."""
    def exposed_ok(k: int) -> bool:
        r = rows[k]
        return (r.get("frac_dark") or 0.0) < _DARK_MAX and (r.get("frac_bright") or 0.0) < _BRIGHT_MAX

    def key(k: int) -> tuple:
        r = rows[k]
        pixels = int(r.get("width") or 0) * int(r.get("height") or 0)
        return (-_lap(r), -pixels, r.get("item_uid") or "")

    pool = [k for k in members if exposed_ok(k)] or list(members)
    best = min(pool, key=key)
    return [best] + sorted((k for k in members if k != best), key=key)


def _make(source: str, members: list[int], rows: list[dict]) -> Burst:
    ranked = _rank(members, rows)
    ts = [rows[k]["taken_ts"] for k in members if rows[k].get("taken_ts") is not None]
    times = [t for t in (_time(rows[k]) for k in members) if t is not None]
    return Burst(
        source=source,
        members=ranked,
        start_ts=min(ts) if ts else None,
        end_ts=max(ts) if ts else None,
        duration_s=(max(times) - min(times)) if times else 0.0,
        quality={k: _lap(rows[k]) for k in ranked},
    )


def find_bursts(rows: list[dict], graphic: list[bool], cfg: MergeConfig,
                dup_group_of: dict[int, int] | None = None,
                dup_keepers: set[int] | None = None) -> list[Burst]:
    """Filename and time bursts over item rows. ``dup_group_of`` maps row index -> dup
    group number, ``dup_keepers`` holds the rows that are the keeper of their group."""
    dup_group_of = dup_group_of or {}
    dup_keepers = dup_keepers or set()
    bursts: list[Burst] = []

    by_key: dict[tuple, list[int]] = defaultdict(list)
    for k, r in enumerate(rows):
        bk = burst_key(r.get("filename") or "")
        if bk is not None:
            # Not keyed by export: after collapse the primaries of one burst's frames can come
            # from different exports, and the prefix (with its timestamp) is already unique
            # within a year folder.
            by_key[(r.get("folder"), bk[0])].append(k)
    in_filename_burst: set[int] = set()
    for members in by_key.values():
        if len(members) >= 2:
            bursts.append(_make("filename", members, rows))
            in_filename_burst.update(members)

    seq = []
    for k, r in enumerate(rows):
        t = _time(r)
        if (t is None or graphic[k] or k in in_filename_burst or r.get("phash64") is None
                or (k in dup_group_of and k not in dup_keepers)):
            continue
        seq.append((t, r.get("item_uid") or "", k))
    seq.sort()

    chain: list[int] = []
    for (t_prev, _u, a), (t, _v, b) in zip(seq, seq[1:]):
        ra, rb = rows[a], rows[b]
        ma, mb = ra.get("model"), rb.get("model")
        linked = (
            t - t_prev <= cfg.burst_window_s
            and hamming64(int(ra["phash64"]), int(rb["phash64"])) <= cfg.burst_phash_max
            and (ma is None or mb is None or ma == mb)
            and not (a in dup_group_of and dup_group_of.get(a) == dup_group_of.get(b))
        )
        if linked:
            if not chain:
                chain = [a]
            chain.append(b)
        else:
            if len(chain) >= 2:
                bursts.append(_make("time", chain, rows))
            chain = []
    if len(chain) >= 2:
        bursts.append(_make("time", chain, rows))

    bursts.sort(key=lambda b: (min(b.members), b.source))
    return bursts


def burst_extras(bursts: list[Burst], rows: list[dict]) -> dict[int, tuple[float, str]]:
    """``burst_extra`` (score, reason) for every non-best frame."""
    out: dict[int, tuple[float, str]] = {}
    for b in bursts:
        best = rows[b.best]
        for k in b.members[1:]:
            mine = _lap(rows[k])
            ratio = _lap(best) / mine if mine > 0 else float("inf")
            sharper = f"{ratio:.1f}x sharper" if ratio != float("inf") else "sharper"
            out[k] = (BURST_EXTRA, f"burst of {len(b.members)} in {b.duration_s:.1f} s; "
                                   f"#{best.get('item_id')} is {sharper}")
    return out
