"""Contact sheets: many thumbnails on one JPEG, so Claude can review 48 photos per image.

Claude's vision cost is ``ceil(w/28) * ceil(h/28)`` tokens per image, so one 1232x924 sheet of
48 photos (~1,452 tokens) is far cheaper than 48 single images. Two layouts share that canvas
limit:

* ``standard``: 8 columns x up to 6 rows of 154 px cells built from the 160 px grid thumbs.
  Each photo is fitted into 150 px and letterboxed on dark grey, leaving 2 px gutters.
* ``high``: up to 20 larger cells built from the 640 px previews, for a closer look at a few
  ambiguous photos (4x3 cells of 308 px, or 5x4 cells of 231 px).

Every cell carries a large number (1..n) in its top-left corner that the text legend refers
to, a red corner mark when the item is already in the review queue and a green "K" when it
is a suggested keeper. Cells without a thumbnail show "no thumb" instead of failing.

Duplicate groups that cannot be proven to hold two different Google Photos items are marked
"dupN?" and their extras "maybe-same-item": deleting such an "extra" in Google Photos may
delete the keeper itself (see :func:`dup_uncertainty`).

Only our own re-encoded thumbnails are drawn and the output is a fresh JPEG with no metadata.
Rendering is deterministic: the same inputs give byte-identical output.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from gpclean.bundle_read import Bundle
from gpclean.review_db import strip_unsafe

log = logging.getLogger(__name__)

DETAILS = ("standard", "high")
MAX_STANDARD = 48
MAX_HIGH = 20
CANVAS_MAX = (1232, 924)
JPEG_QUALITY = 80
FILENAME_MAX = 80          # legend/listing filenames are truncated to this many characters
VISION_TILE = 28           # Claude vision tokens = ceil(w/28) * ceil(h/28)

_GUTTER_BG = (17, 17, 17)       # between cells
_CELL_BG = (34, 34, 34)         # "#222" letterbox
_LABEL_BG = (0, 0, 0)
_LABEL_FG = (255, 255, 255)
_QUEUED = (230, 30, 30)
_KEEPER_BG = (20, 150, 60)
_PLACEHOLDER_FG = (150, 150, 150)
# Our thumbnails are at most 640 px; anything far larger in a pack is not ours, so skip it
# rather than spend memory decoding it.
_MAX_THUMB_PIXELS = 2048 * 2048


@dataclass(frozen=True)
class Layout:
    """Grid geometry for one sheet."""

    cols: int
    rows: int
    cell: int        # square cell size in px (image area + 2 * gutter)
    gutter: int      # px left free on each side of the image inside its cell
    font_px: int
    source: str      # "g" (grid thumb) or "p" (preview)

    @property
    def size(self) -> tuple[int, int]:
        """Canvas (width, height) in px."""
        return self.cols * self.cell, self.rows * self.cell

    @property
    def inner(self) -> int:
        """Largest image edge inside a cell."""
        return self.cell - 2 * self.gutter


def image_tokens(width: int, height: int) -> int:
    """Estimated Claude vision tokens for an image of this size."""
    return math.ceil(width / VISION_TILE) * math.ceil(height / VISION_TILE)


def layout_for(n: int, detail: str) -> Layout:
    """Pick the grid for ``n`` photos; raise ValueError when ``n`` is out of range."""
    if detail not in DETAILS:
        raise ValueError("detail must be 'standard' or 'high'")
    limit = MAX_STANDARD if detail == "standard" else MAX_HIGH
    if not isinstance(n, int) or not 1 <= n <= limit:
        raise ValueError(f"a {detail} contact sheet takes 1..{limit} ids")
    if detail == "standard":
        # Always 1232 wide so cells keep one size; only the number of rows shrinks.
        return Layout(cols=8, rows=math.ceil(n / 8), cell=154, gutter=2, font_px=26, source="g")
    if n <= 12:
        return Layout(cols=4, rows=math.ceil(n / 4), cell=308, gutter=4, font_px=34, source="p")
    return Layout(cols=5, rows=math.ceil(n / 5), cell=231, gutter=3, font_px=30, source="p")


@lru_cache(maxsize=8)
def _font(px: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Pillow's bundled scalable font: identical on every platform, so output is stable."""
    return ImageFont.load_default(size=px)


def _open_thumb(data: bytes | None) -> Image.Image | None:
    """Decode one of our WebP thumbnails to RGB, or None when missing or unreadable."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data), formats=["WEBP", "JPEG", "PNG"]) as im:
            if im.width * im.height > _MAX_THUMB_PIXELS:
                log.warning("thumbnail unexpectedly large; skipped")
                return None
            return im.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        log.warning("unreadable thumbnail (%s)", type(exc).__name__)
        return None


def _fit(im: Image.Image, box: int) -> Image.Image:
    """Scale (up or down) so the long edge equals ``box``, keeping the aspect ratio."""
    w, h = im.size
    scale = box / max(w, h)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    if size == im.size:
        return im
    return im.resize(size, Image.Resampling.LANCZOS)


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font,
           bg: tuple[int, int, int]) -> None:
    """Draw ``text`` on a solid rounded box whose top-left corner is ``xy``."""
    pad = max(3, getattr(font, "size", 12) // 6)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    x, y = xy
    box = (x, y, x + (right - left) + 2 * pad, y + (bottom - top) + 2 * pad)
    draw.rounded_rectangle(box, radius=pad + 1, fill=bg)
    # A 1 px stroke in the text colour makes the default font read as bold.
    draw.text((x + pad - left, y + pad - top), text, font=font, fill=_LABEL_FG,
              stroke_width=1, stroke_fill=_LABEL_FG)


def _draw_cell(canvas: Image.Image, draw: ImageDraw.ImageDraw, lay: Layout, index: int,
               thumb: Image.Image | None, *, queued: bool, keeper: bool,
               placeholder: str) -> None:
    """Paint cell ``index`` (0-based) with its photo, number and marks."""
    col, row = index % lay.cols, index // lay.cols
    x0, y0 = col * lay.cell + lay.gutter, row * lay.cell + lay.gutter
    inner = lay.inner
    draw.rectangle((x0, y0, x0 + inner - 1, y0 + inner - 1), fill=_CELL_BG)
    if thumb is not None:
        fitted = _fit(thumb, inner)
        canvas.paste(fitted, (x0 + (inner - fitted.width) // 2, y0 + (inner - fitted.height) // 2))
    else:
        font = _font(max(12, lay.font_px // 2))
        left, top, right, bottom = draw.textbbox((0, 0), placeholder, font=font)
        draw.text((x0 + (inner - (right - left)) // 2 - left,
                   y0 + (inner - (bottom - top)) // 2 - top),
                  placeholder, font=font, fill=_PLACEHOLDER_FG)

    font = _font(lay.font_px)
    edge = max(3, lay.gutter + 1)
    _label(draw, (x0 + edge, y0 + edge), str(index + 1), font, _LABEL_BG)
    if queued:
        # Red triangle in the top-right corner: "already in the To delete queue".
        s = max(18, inner // 6)
        x1 = x0 + inner - 1
        draw.polygon([(x1 - s, y0), (x1, y0), (x1, y0 + s)], fill=_QUEUED)
    if keeper:
        kfont = _font(lay.font_px)
        left, top, right, bottom = draw.textbbox((0, 0), "K", font=kfont, stroke_width=1)
        pad = max(3, getattr(kfont, "size", 12) // 6)
        w, h = (right - left) + 2 * pad, (bottom - top) + 2 * pad
        _label(draw, (x0 + inner - w - edge, y0 + inner - h - edge), "K", kfont, _KEEPER_BG)


# ---------------------------------------------------------------------------------------------
# Text helpers shared with the MCP server (legend lines, search rows)
# ---------------------------------------------------------------------------------------------

def clean_text(value: object, limit: int = FILENAME_MAX) -> str:
    """Library text made safe for a one-line listing: control/bidi characters stripped,
    ``|`` replaced (it separates columns), truncated to ``limit`` characters."""
    if value is None:
        return ""
    text = strip_unsafe(str(value)).replace("|", "/")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def when(item: dict) -> str:
    """'YYYY-MM-DD HH:MM' (or just the date, or 'undated'); '~' marks an uncertain day."""
    d, t = item.get("local_date"), item.get("local_time")
    if not d:
        return "undated"
    out = f"{d} {t[:5]}" if t else d
    return out + ("~" if item.get("day_uncertain") else "")


def junk_flags(scores: dict[str, tuple[float, str]], top: int = 3) -> list[str]:
    """Top junk categories as 'category:0.95' (highest first, score > 0 only)."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [f"{c}:{s:.2f}" for c, (s, _r) in ranked[:top] if s > 0]


def attr_flags(item: dict) -> list[str]:
    """Flags that make deleting riskier or harder: shared / partner / favorite / no link."""
    out = []
    if item.get("shared"):
        out.append("shared")
    if item.get("partner"):
        out.append("partner")
    if item.get("favorited"):
        out.append("fav")
    if not item.get("url"):
        out.append("nolink")
    return out


MAYBE_SAME_FLAG = "maybe-same-item"


def dup_uncertainty(bundle: Bundle, ids: Iterable[int]) -> tuple[set[int], set[int]]:
    """(uncertain duplicate group ids, maybe-same item ids) for the duplicate groups of ``ids``.

    A group is uncertain when it is not ``deletable`` (fewer than two distinct urls, so the
    whole group may be one library item). A non-keeper member is "maybe the same item" when
    its group is uncertain, or when it or the keeper has no url, or both share one url: the
    same rule merge.bundle uses for its 0.6 "may be the same item" dup_extra score. Deleting
    such a member in Google Photos could delete the keeper, so only the user decides.
    """
    ids = list(dict.fromkeys(int(i) for i in ids))
    groups: set[int] = set()
    same: set[int] = set()
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        marks = ",".join("?" * len(part))
        rows = bundle.fetchall(
            "SELECT m.item_id, m.group_id, m.is_keeper, g.deletable,"
            " COALESCE(i.url, '') AS url,"
            " COALESCE((SELECT ki.url FROM dup_members km JOIN items ki"
            "           ON ki.item_id = km.item_id"
            "           WHERE km.group_id = m.group_id AND km.is_keeper = 1 LIMIT 1), '')"
            "   AS keeper_url"
            " FROM dup_members m JOIN dup_groups g ON g.group_id = m.group_id"
            f" JOIN items i ON i.item_id = m.item_id WHERE m.item_id IN ({marks})", part)
        for item_id, group_id, is_keeper, deletable, url, keeper_url in rows:
            if not deletable:
                groups.add(group_id)
            if not is_keeper and (not deletable or not url or not keeper_url
                                  or url == keeper_url):
                same.add(item_id)
    return groups, same


def group_tag(member: dict | None, uncertain_groups: Collection[int] = ()) -> str:
    """Duplicate-group / burst membership, e.g. 'dup3:keeper,burst1'; '-' when none.

    Groups in ``uncertain_groups`` render as 'dup3?' (they may be a single library item).
    """
    if not member:
        return "-"
    parts = []
    if member.get("dup_group") is not None:
        mark = "?" if member["dup_group"] in uncertain_groups else ""
        parts.append(f"dup{member['dup_group']}{mark}"
                     + (":keeper" if member.get("is_keeper") else ""))
    if member.get("burst") is not None:
        parts.append(f"burst{member['burst']}" + (":best" if member.get("is_best") else ""))
    return ",".join(parts) or "-"


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------

def to_jpeg(im: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    """Encode an RGB image as a baseline JPEG with no metadata (deterministic)."""
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=quality, subsampling=2,
                           optimize=False, progressive=False)
    return buf.getvalue()


def preview_jpeg(bundle: Bundle, item_id: int, max_edge: int = 640) -> bytes | None:
    """The item's 640 px preview (or grid thumb as a fallback) re-encoded as JPEG, or None."""
    im = _open_thumb(bundle.thumb(item_id, "p")) or _open_thumb(bundle.thumb(item_id, "g"))
    if im is None:
        return None
    if max(im.size) > max_edge:
        im = _fit(im, max_edge)
    return to_jpeg(im)


def render_sheet(bundle: Bundle, ids: list[int], *, detail: str = "standard",
                 queued: set[int] = frozenset(), keepers: set[int] = frozenset()
                 ) -> tuple[bytes, list[str]]:
    """Render a contact sheet of ``ids`` (in order). Returns (jpeg_bytes, legend_lines).

    Legend lines look like ``n=<cell> id=<id> <date> <filename> <flags>``; the filename is
    sanitised library text. ``queued`` ids get a red corner mark and ``keepers`` a green K.
    Uncertain duplicate groups show as 'dupN?' and their extras carry 'maybe-same-item'.
    Unknown ids render as a "no item" cell. Raises ValueError for too many / too few ids or
    an unknown ``detail``.
    """
    ids = [int(i) for i in ids]
    lay = layout_for(len(ids), detail)
    canvas = Image.new("RGB", lay.size, _GUTTER_BG)
    draw = ImageDraw.Draw(canvas)
    items = {it["item_id"]: it for it in bundle.items(ids)}
    scores = bundle.scores_many(ids)
    member = bundle.memberships(ids)
    uncertain, maybe_same = dup_uncertainty(bundle, [i for i in ids if i in member])

    legend = []
    for n, item_id in enumerate(ids):
        item = items.get(item_id)
        is_q, is_k = item_id in queued, item_id in keepers
        if item is None:
            thumb, placeholder = None, "no item"
        else:
            thumb = _open_thumb(bundle.thumb(item_id, lay.source))
            if thumb is None and lay.source == "p":
                thumb = _open_thumb(bundle.thumb(item_id, "g"))
            placeholder = "no thumb"
        _draw_cell(canvas, draw, lay, n, thumb, queued=is_q, keeper=is_k,
                   placeholder=placeholder)
        if item is None:
            legend.append(f"n={n + 1} id={item_id} (unknown id)")
            continue
        flags = junk_flags(scores.get(item_id, {})) + attr_flags(item)
        tag = group_tag(member.get(item_id), uncertain)
        if tag != "-":
            flags.append(tag)
        if item_id in maybe_same:
            flags.append(MAYBE_SAME_FLAG)
        if is_q:
            flags.append("QUEUED")
        if is_k:
            flags.append("KEEPER")
        if thumb is None:
            flags.append("no-thumb")
        legend.append(f"n={n + 1} id={item_id} {when(item)} {clean_text(item.get('filename'))}"
                      f" {' '.join(flags) or '-'}")
    return to_jpeg(canvas), legend
