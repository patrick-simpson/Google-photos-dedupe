"""Library search shared by the MCP ``search`` tool and the review site's Search tab.

Filters are pushed into SQL (indexed where it matters); the optional CLIP text query is a
single vectorised float32 matrix-vector product over the memory-mapped embeddings. Both are
comfortably fast for 100k items. Paging happens after filtering and sorting, so ``total`` is
the exact number of matches.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import numpy as np

from gpclean.bundle_read import JUNK_DEFAULT_MIN, Bundle
from gpclean.review_db import ReviewDB
from gpclean.schema import JUNK_CATEGORIES

log = logging.getLogger(__name__)

SIM_THRESHOLD = 0.18      # cosine below this is hidden for text queries (PLAN section 6)
MAX_LIMIT = 200
MAX_TEXT = 200
MAX_FLAGS = 5
SORTS = ("score", "date", "similarity")
_GROUP_RE = re.compile(r"^(dup|burst):([0-9]{1,12})$")
_EMB_CHUNK = 16384        # rows per float16 -> float32 conversion step (bounds peak memory)


class SearchUnavailable(ValueError):
    """A text query was asked for but there is no embedder or the bundle has no embeddings."""


@dataclass
class SearchParams:
    """Search inputs. Every field is optional; ``validate()`` enforces types and ranges.

    ``category`` is a junk category or "any" (any category); ``min_score`` defaults to
    ``JUNK_DEFAULT_MIN`` when a category is given. Dates are inclusive ``YYYY-MM-DD`` on the
    item's local date. ``exclude_queued`` hides every item already in the review queue
    (proposed, approved or rejected). ``sort=None`` picks similarity for a text query, score
    for a category filter, and date otherwise.
    """

    query: str | None = None
    category: str | None = None
    min_score: float | None = None
    date_from: str | None = None
    date_to: str | None = None
    year: int | None = None
    filename_contains: str | None = None
    origin_contains: str | None = None
    group: str | None = None
    has_gps: bool | None = None
    exclude_queued: bool = True
    sort: str | None = None
    limit: int = 50
    offset: int = 0

    def validate(self) -> None:
        """Raise ValueError for anything out of range; normalise blank strings to None."""
        for name in ("query", "filename_contains", "origin_contains"):
            v = getattr(self, name)
            if v is not None:
                if not isinstance(v, str) or len(v) > MAX_TEXT:
                    raise ValueError(f"{name} must be a string of at most {MAX_TEXT} chars")
                setattr(self, name, v.strip() or None)
        if self.category is not None and self.category not in (*JUNK_CATEGORIES, "any"):
            raise ValueError("unknown category")
        if self.min_score is not None:
            if isinstance(self.min_score, bool) or not isinstance(self.min_score, (int, float)) \
                    or not 0.0 <= float(self.min_score) <= 1.0:
                raise ValueError("min_score must be 0..1")
        for name in ("date_from", "date_to"):
            v = getattr(self, name)
            if v is not None:
                if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    raise ValueError(f"{name} must be YYYY-MM-DD")
                date.fromisoformat(v)   # raises ValueError for e.g. 2021-02-30
        if self.year is not None and (isinstance(self.year, bool) or not isinstance(self.year, int)
                                      or not 1800 <= self.year <= 2200):
            raise ValueError("year out of range")
        if self.group is not None and (not isinstance(self.group, str)
                                       or not _GROUP_RE.match(self.group)):
            raise ValueError("group must be 'dup:<id>' or 'burst:<id>'")
        if self.has_gps is not None and not isinstance(self.has_gps, bool):
            raise ValueError("has_gps must be a bool")
        # Strict bool: a JSON "false" string (or 0/1) would otherwise be truthy.
        if not isinstance(self.exclude_queued, bool):
            raise ValueError("exclude_queued must be a bool")
        if self.sort is not None and self.sort not in SORTS:
            raise ValueError("sort must be score, date or similarity")
        if self.sort == "similarity" and not self.query:
            raise ValueError("sort=similarity needs a query")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) \
                or not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError(f"limit must be 1..{MAX_LIMIT}")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) \
                or not 0 <= self.offset <= 10_000_000:
            raise ValueError("offset out of range")


def _where(p: SearchParams) -> tuple[str, list[object], str, list[object]]:
    """SQL WHERE clause for the filters, plus the expression used as the item's score.

    The score is the category's score when a category is chosen, otherwise the item's
    combined score (max over categories, PLAN section 5).
    """
    clauses: list[str] = []
    params: list[object] = []
    min_score = p.min_score
    if p.category is not None and min_score is None:
        min_score = JUNK_DEFAULT_MIN
    if p.category not in (None, "any"):
        clauses.append("i.item_id IN (SELECT item_id FROM scores WHERE category = ? AND score >= ?)")
        params += [p.category, float(min_score)]
        score_sql = "(SELECT score FROM scores s WHERE s.item_id = i.item_id AND s.category = ?)"
        score_params: list[object] = [p.category]
    else:
        if min_score is not None:
            clauses.append("i.item_id IN (SELECT item_id FROM scores WHERE score >= ?)")
            params.append(float(min_score))
        score_sql = "(SELECT MAX(score) FROM scores s WHERE s.item_id = i.item_id)"
        score_params = []
    if p.date_from:
        clauses.append("i.local_date >= ?")
        params.append(p.date_from)
    if p.date_to:
        clauses.append("i.local_date <= ?")
        params.append(p.date_to)
    if p.year is not None:
        clauses.append("i.year = ?")
        params.append(p.year)
    if p.filename_contains:
        # instr() instead of LIKE: no wildcard characters to escape in user text.
        clauses.append("instr(gp_casefold(i.filename), ?) > 0")
        params.append(p.filename_contains.casefold())
    if p.origin_contains:
        clauses.append("instr(gp_casefold(i.origin_folder), ?) > 0")
        params.append(p.origin_contains.casefold())
    if p.group:
        kind, gid = _GROUP_RE.match(p.group).groups()
        table, col = ("dup_members", "group_id") if kind == "dup" else ("burst_members", "burst_id")
        clauses.append(f"i.item_id IN (SELECT item_id FROM {table} WHERE {col} = ?)")
        params.append(int(gid))
    if p.has_gps is True:
        clauses.append("(i.lat IS NOT NULL AND i.lon IS NOT NULL)")
    elif p.has_gps is False:
        clauses.append("(i.lat IS NULL OR i.lon IS NULL)")
    return " AND ".join(clauses) or "1", params, score_sql, score_params


def _similarities(bundle: Bundle, text_embedder, query: str) -> np.ndarray:
    """Cosine similarity of every embedding row to the query (float32, shape (N,))."""
    emb = bundle.embeddings()
    if text_embedder is None or emb is None:
        raise SearchUnavailable("text search needs CLIP embeddings and a text model")
    q = np.asarray(text_embedder.embed(query), dtype=np.float32).reshape(-1)
    if q.shape[0] != emb.shape[1]:
        raise SearchUnavailable("text model does not match the bundle's embeddings")
    norm = float(np.linalg.norm(q))
    if norm > 0:
        q = q / norm
    out = np.empty(emb.shape[0], dtype=np.float32)
    # Convert in chunks: a float32 copy of 100k x 512 would be 200 MB at once.
    for a in range(0, emb.shape[0], _EMB_CHUNK):
        out[a:a + _EMB_CHUNK] = np.asarray(emb[a:a + _EMB_CHUNK], dtype=np.float32) @ q
    return out


def _flags(scores: dict[str, tuple[float, str]]) -> list[dict]:
    """Top categories by score (highest first) for display."""
    top = sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))[:MAX_FLAGS]
    return [{"category": c, "score": round(s, 3), "reason": r} for c, (s, r) in top if s > 0]


def search(bundle: Bundle, review: ReviewDB | None, p: SearchParams,
           text_embedder=None) -> dict:
    """Run a search. Returns {"total", "offset", "next_offset", "rows"}.

    Each row is an item dict plus ``score`` (see ``_where``), ``flags`` (top categories with
    score and reason), ``sim`` (cosine to the query, or None), ``queue`` (the item's review
    status: proposed / approved / rejected / deleted, or None) and ``dup_group`` /
    ``is_keeper`` / ``burst`` / ``is_best`` when the item belongs to a group or burst.
    """
    p.validate()
    sort = p.sort or ("similarity" if p.query else "score" if p.category else "date")
    where, params, score_sql, score_params = _where(p)
    cand = bundle.fetchall(
        f"SELECT i.item_id, i.item_uid, i.emb_row, {score_sql} AS score"
        f" FROM items i WHERE {where}"
        # Base order for date sort; score and similarity re-sort stably on top of it.
        " ORDER BY i.local_date IS NULL, i.local_date, i.local_time, i.item_id",
        (*score_params, *params))

    if p.exclude_queued and review is not None and cand:
        queued = review.uids()
        if queued:
            cand = [r for r in cand if r["item_uid"] not in queued]

    sims: dict[int, float] = {}
    if p.query:
        all_sims = _similarities(bundle, text_embedder, p.query)
        n_rows = all_sims.shape[0]
        kept = []
        for r in cand:
            row = r["emb_row"]
            if row is None or not 0 <= row < n_rows:
                continue            # no embedding -> cannot match a text query
            s = float(all_sims[row])
            if s >= SIM_THRESHOLD:
                sims[r["item_id"]] = s
                kept.append(r)
        cand = kept

    if sort == "similarity":
        cand.sort(key=lambda r: -sims[r["item_id"]])
    elif sort == "score":
        cand.sort(key=lambda r: -(r["score"] or 0.0))

    total = len(cand)
    page = cand[p.offset:p.offset + p.limit]
    ids = [r["item_id"] for r in page]
    items = {it["item_id"]: it for it in bundle.items(ids)}
    scores = bundle.scores_many(ids)
    member = bundle.memberships(ids)
    status = review.get([it["item_uid"] for it in items.values()]) if review and items else {}

    rows = []
    for r in page:
        it = items[r["item_id"]]
        q = status.get(it["item_uid"])
        it.update(
            score=r["score"],
            flags=_flags(scores.get(it["item_id"], {})),
            sim=round(sims[it["item_id"]], 4) if it["item_id"] in sims else None,
            queue=("deleted" if q["deleted"] else q["status"]) if q else None,
            dup_group=None, is_keeper=None, burst=None, is_best=None,
        )
        it.update(member.get(it["item_id"], {}))
        rows.append(it)
    end = p.offset + len(rows)
    log.debug("search: %d matches, %d returned", total, len(rows))
    return {"total": total, "offset": p.offset,
            "next_offset": end if end < total else None, "rows": rows}
