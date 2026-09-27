"""The local review site (``gpclean serve --home <home>``): a small, hardened stdlib web app.

It serves one page (``static/index.html`` + ``app.js`` + ``style.css``) and a JSON API over
the current bundle (read-only) and the review queue (``review_db.ReviewDB``, the only writer).
Everything runs on 127.0.0.1; nothing is ever fetched from the internet.

Security controls (docs/PLAN.md section 8, design-security W-1..W-13), all enforced here:

* W-1  ``ThreadingHTTPServer`` bound to 127.0.0.1 only; a ``BaseHTTPRequestHandler`` subclass
  (never ``SimpleHTTPRequestHandler``, which serves the working directory). Static files come
  from a fixed in-package dict loaded once via ``importlib.resources``; no path joins.
* W-2  ``Host`` must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (DNS rebinding) -> 421.
* W-3  ``Sec-Fetch-Site``, when sent, must be ``same-origin`` or ``none`` -> 403.
* W-4  Every state change is a POST with ``Content-Type: application/json``, an ``Origin``
  equal to the page origin, and ``X-CSRF-Token`` equal (``hmac.compare_digest``) to a
  per-launch secret delivered in a ``<meta>`` tag of index.html. GET never mutates.
* W-5  No CORS headers at all.
* W-6  CSP / nosniff / no-referrer / CORP / COOP on every response (errors included).
* W-7  Thumbnails only as ``/thumb/<int>/<g|p>``; blobs come from ``Bundle.thumb``.
* W-8  Text fields are stripped of control/bidi characters here and again in the browser,
  which renders them with ``textContent`` only.
* W-9  Google Photos links are rebuilt from a validated id (``validate_photos_url``) or are
  a filename search link marked ``link_conf: "low"``.
* W-11 CSV cells that a spreadsheet would treat as a formula are prefixed with ``'``.
* W-13 Approving every member of a duplicate group is refused; shared, partner-shared and
  favorited items, and members of non-deletable ("possible same item") groups, need
  ``"override": true`` in that request. The check runs as a ``ReviewDB`` guard inside the
  write transaction (after ``BEGIN IMMEDIATE``), so neither parallel requests nor a second
  ``gpclean serve`` on the same home can slip an approval in between check and write.

Deletion mode's "safe to day-select" badge (PLAN section 8) is shown for a day only when
selecting that whole day in Google Photos would select exactly the approved photos: every
indexed photo of the day is approved, the bundle knows of no library item on that day that is
missing from the index (``videos_by_day``, which despite its name counts videos AND skipped
media such as raw files), no photo of the day has an uncertain day, no undated indexed photo
belongs to that year, and no unindexed item is undated. Each day lists the reasons it is not
safe (``day_select_blockers``) so the page can say why.

Error responses carry a fixed code such as ``{"error": "bad_param", "param": "limit"}``; raw
input is never echoed back. Requests are logged to the private log without query strings.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import logging.handlers
import os
import re
import secrets
import socket
import socketserver
import sqlite3
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from gpclean.bundle_read import DUP_KINDS, JUNK_DEFAULT_MIN, Bundle
from gpclean.localinit import current_bundle, home_paths
from gpclean.review_db import DECISIONS, LIST_STATUSES, ReviewDB, strip_unsafe
from gpclean.schema import JUNK_CATEGORIES
from gpclean.search import MAX_LIMIT as SEARCH_MAX_LIMIT
from gpclean.search import SearchParams, SearchUnavailable, search
from gpclean.takeout.names import parse_media_name, year_of_folder
from gpclean.takeout.sidecar import validate_photos_url
from gpclean.version import TRASH_DAYS

log = logging.getLogger(__name__)

BIND_HOST = "127.0.0.1"
MAX_BODY = 1024 * 1024          # POST bodies are small JSON objects; 1 MiB is plenty
MAX_IDS = 2000                  # ids/uids per POST
MAX_GROUPS = 200                # dup groups per "queue non-keepers" request
QUEUE_MAX_LIMIT = 10_000
CSRF_PLACEHOLDER = "__GPCLEAN_CSRF_TOKEN__"
DEFAULT_ADD_REASON = "added in the review site"
DUP_REASON_NAME_MAX = 250       # keeps "duplicate of <name>" under review_db's 300-char cap

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
)
NO_STORE = "no-store"
THUMB_CACHE = "private, max-age=3600"

# path -> (package file, content type). The only files the site can ever serve.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
_THUMB_RE = re.compile(r"^/thumb/([0-9]{1,12})/([gp])$")
_ALLOWED_FETCH_SITES = ("same-origin", "none")
# CSV cells starting with these are formulas (or can become one) in Excel/Sheets/LibreOffice.
_CSV_DANGEROUS = ("=", "+", "-", "@", "\t", "\r", "\n")
CSV_COLUMNS = ("item_uid", "filename", "local_date", "local_time", "width", "height",
               "size_bytes", "category", "reason", "proposed_by", "status", "deleted", "url")
# Reasons a day is not "safe to day-select", in the order the page reports them.
DAY_BLOCKERS = ("no_indexed_photos", "not_all_approved", "not_indexed_that_day",
                "day_uncertain", "undated_in_year", "undated_unknown_year",
                "unindexed_undated")
# Fields compared between duplicates so the UI can highlight what differs from the keeper.
_DIFF_FIELDS = ("dims", "size_bytes", "ext", "local_date", "local_time", "filename",
                "match_conf", "link_conf", "albums_n", "shared", "partner", "favorited")


class ApiError(Exception):
    """An error response with a fixed machine-readable code (never raw user input)."""

    def __init__(self, status: int, code: str, **extra: object):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------- small helpers

def _clean(value: object) -> object:
    """strip_unsafe() for display strings; other values pass through."""
    return strip_unsafe(value) if isinstance(value, str) else value


def _json_list(text: object) -> list[str]:
    """Decode a JSON array of strings stored in the index (albums, people); [] if malformed."""
    if not isinstance(text, str) or not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [strip_unsafe(x) for x in data if isinstance(x, str)][:50]


def csv_cell(value: object) -> object:
    """Neutralise spreadsheet formulas: prefix ``'`` to text starting with = + - @ tab CR LF."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_CSV_DANGEROUS):
        return "'" + value
    return value


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_list(v: object, name: str, max_n: int = MAX_IDS) -> list[int]:
    if not isinstance(v, list) or not v or len(v) > max_n \
            or not all(_is_int(x) and 0 < x < 2**53 for x in v):
        raise ApiError(400, "bad_param", param=name)
    return list(dict.fromkeys(v))


def _str_list(v: object, name: str, max_n: int = MAX_IDS) -> list[str]:
    if not isinstance(v, list) or not v or len(v) > max_n \
            or not all(isinstance(x, str) and 0 < len(x) <= 1000 for x in v):
        raise ApiError(400, "bad_param", param=name)
    return list(dict.fromkeys(v))


def _strict_bool(v: object, name: str) -> bool:
    if not isinstance(v, bool):
        raise ApiError(400, "bad_param", param=name)
    return v


def _short_str(v: object, name: str, max_len: int = 100) -> str:
    if not isinstance(v, str) or not 0 < len(v) <= max_len:
        raise ApiError(400, "bad_param", param=name)
    return v


class _Query:
    """Typed access to a URL query string; every failure is a 400 naming the parameter."""

    def __init__(self, raw: str):
        try:
            self._q = parse_qs(raw, keep_blank_values=False, max_num_fields=50)
        except ValueError as exc:   # too many fields
            raise ApiError(400, "bad_query") from exc

    def str(self, name: str, max_len: int = 200) -> str | None:
        vals = self._q.get(name)
        if not vals:
            return None
        v = vals[0].strip()
        if len(v) > max_len:
            raise ApiError(400, "bad_param", param=name)
        return v or None

    def int(self, name: str, default: int | None, lo: int, hi: int) -> int | None:
        v = self.str(name, 20)
        if v is None:
            return default
        if not re.fullmatch(r"-?[0-9]{1,15}", v) or not lo <= int(v) <= hi:
            raise ApiError(400, "bad_param", param=name)
        return int(v)

    def float(self, name: str, default: float | None, lo: float, hi: float) -> float | None:
        v = self.str(name, 20)
        if v is None:
            return default
        try:
            f = float(v)
        except ValueError:
            raise ApiError(400, "bad_param", param=name) from None
        if not lo <= f <= hi:     # also rejects NaN
            raise ApiError(400, "bad_param", param=name)
        return f

    def enum(self, name: str, allowed: tuple[str, ...], default: str | None) -> str | None:
        v = self.str(name, 40)
        if v is None:
            return default
        if v not in allowed:
            raise ApiError(400, "bad_param", param=name)
        return v

    def date(self, name: str) -> str | None:
        v = self.str(name, 10)
        if v is None:
            return None
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError
            date.fromisoformat(v)
        except ValueError:
            raise ApiError(400, "bad_param", param=name) from None
        return v


def _page(total: int, offset: int, n: int) -> dict:
    end = offset + n
    return {"total": total, "offset": offset, "next_offset": end if end < total else None}


def _recoverable_until(deleted_at: object) -> str | None:
    """deleted_at (ISO UTC) + TRASH_DAYS as YYYY-MM-DD, or None."""
    if not isinstance(deleted_at, str):
        return None
    try:
        when = datetime.strptime(deleted_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return (when + timedelta(days=TRASH_DAYS)).date().isoformat()


def _load_static() -> dict[str, bytes]:
    """Read the packaged static files once (a fixed set; nothing is looked up by request)."""
    base = resources.files("gpclean.site").joinpath("static")
    names = {name for name, _ in STATIC_FILES.values()}
    return {name: base.joinpath(name).read_bytes() for name in names}


def _read_account_index(config_json: Path) -> int | None:
    """Optional ``photos_account_index`` (the N in photos.google.com/u/N/) from config.json."""
    try:
        data = json.loads(config_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    v = data.get("photos_account_index") if isinstance(data, dict) else None
    if _is_int(v) and 0 <= v <= 99:
        return v
    if v is not None:
        log.warning("ignoring invalid photos_account_index in config.json")
    return None


class _Serialised:
    """Wrap a text embedder so request threads call ``embed`` one at a time."""

    def __init__(self, embedder, lock: threading.Lock):
        self._embedder = embedder
        self._lock = lock

    def embed(self, query: str):
        with self._lock:
            return self._embedder.embed(query)


# ---------------------------------------------------------------------------- the app

class SiteApp:
    """Request-independent state and the API logic (the handler only does HTTP)."""

    LIST_STEP = 100_000          # review_db.list's maximum page (tests lower it)

    def __init__(self, home: Path, port: int, *, text_embedder=None):
        paths = home_paths(home)
        self.home = paths["home"]
        self.port = port
        self.bundle = Bundle(current_bundle(home))
        self.review = ReviewDB(paths["review_db"])
        self.account_index = _read_account_index(paths["config_json"])
        self.csrf_token = secrets.token_urlsafe(32)
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        # A short tag of this bundle, appended to thumbnail URLs so the browser cache can
        # never show another bundle's thumbnail for the same item id.
        self.bundle_tag = hashlib.sha256(
            f"{self.bundle.path}|{self.bundle.meta.get('created_at')}".encode()).hexdigest()[:10]
        static = _load_static()
        index = static["index.html"].decode("utf-8")
        if CSRF_PLACEHOLDER not in index:
            raise RuntimeError("index.html lacks the CSRF placeholder")
        static["index.html"] = index.replace(CSRF_PLACEHOLDER, self.csrf_token).encode("utf-8")
        self.static = static
        # W-13's check-then-write must be atomic: ThreadingHTTPServer runs POSTs in parallel,
        # and a second ``gpclean serve`` may run on the same home. The check is therefore a
        # guard that ReviewDB runs inside its BEGIN IMMEDIATE transaction (see _guard).
        self._undated: tuple[dict[int, int], int, int] | None = None   # see _undated_counts
        self._text_lock = threading.Lock()
        self._embed_lock = threading.Lock()
        self.text_embedder = _Serialised(text_embedder, self._embed_lock) \
            if text_embedder is not None else None
        if text_embedder is not None:
            self.text_status = "ready"
        elif self.bundle.embeddings() is None:
            self.text_status = "none"          # --no-clip bundle: filters only
        else:
            self.text_status = "not_loaded"

    def close(self) -> None:
        """Close the bundle and the review DB."""
        self.bundle.close()
        self.review.close()

    # ------------------------------------------------------------------ CLIP text model

    def start_text_loader(self) -> None:
        """Load the CLIP text tower in a background thread (the site works without it)."""
        with self._text_lock:
            if self.text_status != "not_loaded":
                return
            self.text_status = "loading"
        threading.Thread(target=self._load_text_model, name="gpclean-clip-text",
                         daemon=True).start()

    def _load_text_model(self) -> None:
        # Offline only: the model must already be in the cache (``gpclean fetch-model``).
        os.environ["HF_HUB_OFFLINE"] = "1"
        name = self.bundle.meta.get("clip_model") or self.bundle.manifest.get("clip_model")
        try:
            from gpclean import clipmodel

            if name not in clipmodel.MODELS:
                raise ValueError("bundle has no known CLIP model")
            embedder = clipmodel.TextEmbedder(name)
        except Exception as exc:  # noqa: BLE001 - any failure just disables text search
            log.warning("CLIP text search unavailable (%s)", type(exc).__name__)
            with self._text_lock:
                self.text_status = "unavailable"
            return
        with self._text_lock:
            self.text_embedder = _Serialised(embedder, self._embed_lock)
            self.text_status = "ready"
        log.info("CLIP text model loaded")

    # ------------------------------------------------------------------ item shaping

    def photos_link(self, url: object, filename: object) -> tuple[str | None, str | None]:
        """(photos_url, link_conf) for an item: validated url, filename search, or nothing."""
        prefix = "https://photos.google.com/"
        if self.account_index is not None:
            prefix += f"u/{self.account_index}/"
        good = validate_photos_url(url) if url else None
        if good:
            return prefix + good[len("https://photos.google.com/"):], "high"
        if isinstance(filename, str) and filename:
            stem = strip_unsafe(parse_media_name(filename).base)
            if stem:
                return prefix + "search/" + quote(stem, safe=""), "low"
        return None, None

    def items_out(self, items: list[dict]) -> list[dict]:
        """Shape bundle item dicts for the browser (display fields, flags, queue, links)."""
        if not items:
            return []
        ids = [it["item_id"] for it in items]
        scores = self.bundle.scores_many(ids)
        member = self.bundle.memberships(ids)
        queue = self.review.get([it["item_uid"] for it in items])
        same = self._possible_same_items(ids)
        return [self._item_out(it, scores.get(it["item_id"], {}), member.get(it["item_id"], {}),
                               queue.get(it["item_uid"]), it["item_id"] in same)
                for it in items]

    def _possible_same_items(self, ids: list[int]) -> set[int]:
        """Ids that belong to a non-deletable dup group (PLAN section 5: "possible same item").

        Such copies may be one Google Photos item, so deleting "one" can delete the only real
        photo; approving them needs a per-item override like shared items.
        """
        ids = list(dict.fromkeys(ids))
        out: set[int] = set()
        for a in range(0, len(ids), 500):
            part = ids[a:a + 500]
            marks = ",".join("?" * len(part))
            out.update(r[0] for r in self.bundle.fetchall(
                "SELECT m.item_id FROM dup_members m JOIN dup_groups g"
                " ON g.group_id = m.group_id WHERE g.deletable = 0"
                f" AND m.item_id IN ({marks})", part))
        return out

    def _item_out(self, it: dict, scores: dict, member: dict, q: dict | None,
                  possible_same: bool = False) -> dict:
        url, conf = self.photos_link(it.get("url"), it.get("filename"))
        # A url attached by a low-confidence sidecar pairing may point at another photo.
        if conf == "high" and it.get("match_conf") != "high":
            conf = "low"
        flags = [{"category": c, "score": round(s, 3), "reason": _clean(r)}
                 for c, (s, r) in sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))
                 if s > 0]
        shared, partner, fav = (bool(it.get(k)) for k in ("shared", "partner", "favorited"))
        return {
            "id": it["item_id"], "uid": it["item_uid"], "filename": _clean(it.get("filename")),
            "ext": it.get("ext"), "local_date": it.get("local_date"),
            "local_time": it.get("local_time"), "day_uncertain": bool(it.get("day_uncertain")),
            "year": it.get("year"), "width": it.get("width"), "height": it.get("height"),
            "size_bytes": it.get("file_size"), "match_conf": it.get("match_conf"),
            "match_rule": it.get("match_rule"), "shared": shared, "partner": partner,
            "favorited": fav, "archived": bool(it.get("archived")),
            "protected": shared or partner or fav or possible_same,
            "possible_same_item": possible_same,
            "albums_n": it.get("albums_n") or 0, "albums": _json_list(it.get("albums")),
            "origin_folder": _clean(it.get("origin_folder")),
            "description": _clean(it.get("description")),
            "has_gps": it.get("lat") is not None and it.get("lon") is not None,
            "is_graphic": bool(it.get("is_graphic")),
            "photos_url": url, "link_conf": conf, "flags": flags,
            "queue": self._queue_out(q),
            "dup_group": member.get("dup_group"), "is_keeper": member.get("is_keeper"),
            "burst": member.get("burst"), "is_best": member.get("is_best"),
        }

    @staticmethod
    def _queue_out(q: dict | None) -> dict | None:
        if q is None:
            return None
        return {"status": q["status"], "deleted": bool(q["deleted"]),
                "proposed_by": q["proposed_by"], "batch_id": q["batch_id"],
                "reason": _clean(q["reason"]), "category": q["category"],
                "proposed_at": q["proposed_at"], "decided_at": q["decided_at"],
                "deleted_at": q["deleted_at"],
                "recoverable_until": _recoverable_until(q["deleted_at"])}

    def _state(self) -> dict:
        """Fields every POST response carries so the page can refresh its header.

        ``batches`` (open Claude proposal batches) lets the page count NEW proposals per batch,
        which a plain count cannot (approve 5, Claude proposes 5 more: still 5).
        """
        return {"rev": self.review.rev(), "counts": self.review.counts(),
                "batches": self.review.batches()}

    # ------------------------------------------------------------------ GET endpoints

    def _stat_int(self, key: str) -> int | None:
        """A non-negative integer merge statistic, or None when the bundle does not have it."""
        rows = self.bundle.fetchall("SELECT value FROM stats WHERE key = ?", (key,))
        try:
            v = json.loads(rows[0][0]) if rows else None
        except (TypeError, ValueError):
            return None
        return v if _is_int(v) and v >= 0 else None

    def _undated_counts(self) -> tuple[dict[int, int], int, int]:
        """(undated indexed photos per year, undated indexed photos of unknown year,
        library items missing from the index that have no date).

        An undated photo has no place on any day, but Google Photos still shows it somewhere
        in its year, so no day of that year can be proven complete; one of unknown year (no
        date and not in a "Photos from YYYY" folder) blocks every day. ``unindexed_undated``
        is a single bundle-wide count (older bundles: ``videos_undated``, else 0). The bundle
        is read-only, so this is computed once.
        """
        if self._undated is None:
            by_year: dict[int, int] = {}
            unknown = 0
            for r in self.bundle.fetchall(
                    "SELECT year, folder FROM items WHERE local_date IS NULL"):
                year = r[0] if _is_int(r[0]) else year_of_folder(r[1] or "")
                if year is None:
                    unknown += 1
                else:
                    by_year[year] = by_year.get(year, 0) + 1
            stat = self._stat_int("unindexed_undated")
            if stat is None:
                stat = self._stat_int("videos_undated") or 0
            self._undated = (by_year, unknown, stat)
        return self._undated

    def api_stats(self, q: _Query) -> dict:
        stats = self.bundle.stats()
        undated_by_year, undated_unknown, unindexed_undated = self._undated_counts()
        return {
            "bundle": stats,
            # Library items the index has no row for (videos and skipped media such as raw
            # files): ``bundle.videos`` counts those with a day; this names them clearly.
            "not_indexed": {"on_a_day": stats.get("videos", 0), "undated": unindexed_undated},
            # Indexed photos without a date: each blocks day-select for every day of its year.
            "undated_indexed": {
                "by_year": {str(y): n for y, n in sorted(undated_by_year.items())},
                "unknown_year": undated_unknown},
            "queue": self.review.counts(), "rev": self.review.rev(),
            "batches": self.review.batches(), "trash_days": TRASH_DAYS,
            "text_search": self.text_status, "port": self.port, "bundle_tag": self.bundle_tag,
            "junk_categories": list(JUNK_CATEGORIES), "junk_default_min": JUNK_DEFAULT_MIN,
            "photos_account_index": self.account_index,
            "created_at": self.bundle.meta.get("created_at"),
            "clip_model": self.bundle.meta.get("clip_model"),
        }

    def api_rev(self, q: _Query) -> dict:
        return {**self._state(), "text_search": self.text_status}

    def _group_decision(self, g: dict, decisions: dict) -> tuple[str | None, int]:
        """(decision action, effective keeper item_id) for a dup group."""
        d = decisions.get(g["group_key"])
        keeper = g["seed_item_id"]
        if d and d["action"] == "keeper":
            by_uid = {m["item_uid"]: m["item_id"] for m in g["members"]}
            keeper = by_uid.get(d["keeper_uid"], keeper)
        return (d["action"] if d else None), keeper

    def _group_out(self, g: dict, decisions: dict) -> dict:
        action, keeper = self._group_decision(g, decisions)
        members = self.items_out(g["members"])
        raw = {m["item_id"]: m for m in g["members"]}
        for m in members:
            src = raw[m["id"]]
            m.update(dist=src.get("dist"), sig_mad=src.get("sig_mad"),
                     sig_block=src.get("sig_block"), sig_chroma=src.get("sig_chroma"),
                     is_keeper=int(m["id"] == keeper), dims=f"{m['width']}x{m['height']}")
        keeper_out = next(m for m in members if m["is_keeper"])
        for m in members:
            m["diff"] = [] if m is keeper_out else \
                [f for f in _DIFF_FIELDS if m.get(f) != keeper_out.get(f)]
        n_approved = sum(1 for m in members if m["queue"] and m["queue"]["status"] == "approved")
        return {"group_id": g["group_id"], "group_key": g["group_key"], "kind": g["kind"],
                "size": g["size"], "deletable": bool(g["deletable"]), "decision": action,
                "keeper_id": keeper, "keeper_uid": keeper_out["uid"],
                "n_approved": n_approved, "members": members}

    def api_dups(self, q: _Query) -> dict:
        kind = q.enum("kind", DUP_KINDS, None)
        offset = q.int("offset", 0, 0, 10_000_000)
        limit = q.int("limit", 20, 1, 200)
        groups = self.bundle.dup_groups(kind=kind, offset=offset, limit=limit)
        decisions = self.review.group_decisions()
        out = [self._group_out(g, decisions) for g in groups]
        # Oversized similarity buckets were never compared (PLAN section 5); say so.
        skipped = self._stat_int("skipped_buckets") or 0
        return {**_page(self.bundle.n_dup_groups(kind), offset, len(out)), "groups": out,
                "skipped_buckets": skipped}

    def api_junk(self, q: _Query) -> dict:
        category = q.enum("category", JUNK_CATEGORIES, "screenshot")
        min_score = q.float("min_score", JUNK_DEFAULT_MIN, 0.0, 1.0)
        year = q.int("year", None, 1800, 2200)
        offset = q.int("offset", 0, 0, 10_000_000)
        limit = q.int("limit", 100, 1, 500)
        rows, total = self.bundle.junk(category, min_score=min_score, year=year,
                                       offset=offset, limit=limit)
        items = self.items_out(rows)
        for it, row in zip(items, rows, strict=True):
            it["score"] = round(row["score"], 3)
            it["score_reason"] = _clean(row["reason"])
        where, params = "s.score >= ?", [min_score]
        if year is not None:
            where += " AND i.year = ?"
            params.append(year)
        counts = {c: 0 for c in JUNK_CATEGORIES}
        for r in self.bundle.fetchall(
                "SELECT s.category, COUNT(*) FROM scores s JOIN items i ON i.item_id = s.item_id"
                f" WHERE {where} GROUP BY s.category", params):
            counts[r[0]] = int(r[1])
        return {**_page(total, offset, len(items)), "rows": items, "category": category,
                "min_score": min_score, "year": year, "counts": counts}

    def api_search(self, q: _Query) -> dict:
        category = q.enum("category", (*JUNK_CATEGORIES, "any"), None)
        p = SearchParams(
            query=q.str("q"), category=category,
            min_score=q.float("min_score", None, 0.0, 1.0),
            date_from=q.date("date_from"), date_to=q.date("date_to"),
            year=q.int("year", None, 1800, 2200), filename_contains=q.str("filename"),
            exclude_queued=q.enum("exclude_queued", ("0", "1"), "0") == "1",
            sort=q.enum("sort", ("score", "date", "similarity"), None),
            offset=q.int("offset", 0, 0, 10_000_000),
            limit=q.int("limit", 100, 1, SEARCH_MAX_LIMIT))
        if p.query and self.text_embedder is None:
            raise ApiError(503, "text_search_unavailable", text_search=self.text_status)
        try:
            res = search(self.bundle, self.review, p, text_embedder=self.text_embedder)
        except SearchUnavailable:
            raise ApiError(503, "text_search_unavailable", text_search=self.text_status) \
                from None
        except ValueError:
            raise ApiError(400, "bad_query") from None
        items = self.items_out(res["rows"])
        for it, row in zip(items, res["rows"], strict=True):
            it["sim"] = row.get("sim")
            it["score"] = row.get("score")
        return {"total": res["total"], "offset": res["offset"],
                "next_offset": res["next_offset"], "rows": items,
                "text_search": self.text_status}

    def api_item(self, q: _Query) -> dict:
        item_id = q.int("id", None, 1, 2**53)
        it = self.bundle.item(item_id) if item_id is not None else None
        if it is None:
            raise ApiError(404, "unknown_item")
        out = self.items_out([it])[0]
        g = self.bundle.dup_group_of(it["item_id"])
        b = self.bundle.burst_of(it["item_id"])
        out["dup"] = None if g is None else {
            "group_id": g["group_id"], "group_key": g["group_key"], "kind": g["kind"],
            "deletable": bool(g["deletable"]),
            "member_ids": [m["item_id"] for m in g["members"]]}
        out["burst_info"] = None if b is None else {
            "burst_id": b["burst_id"], "best_item_id": b["best_item_id"],
            "member_ids": [m["item_id"] for m in b["members"]]}
        out.update(make=_clean(it.get("make")), model=_clean(it.get("model")),
                   people=_json_list(it.get("people")), folder=_clean(it.get("folder")))
        return out

    def _all_queue_rows(self, status: str) -> list[dict]:
        """Every queue row of ``status`` (review_db.list pages at 100k rows at most)."""
        step, rows = self.LIST_STEP, []
        while True:
            part = self.review.list(status, offset=len(rows), limit=step)
            rows += part
            if len(part) < step:
                return rows

    def _queue_light(self, status: str) -> list[dict]:
        """Queue rows with just enough bundle data to sort and count them.

        Each entry is {"q": queue row, "id", "local_date", "local_time"}; id is None for a uid
        this bundle does not have. Building full item dicts (scores, memberships...) for every
        row made /api/queue cost seconds at 100k approved items; only the page needs them.
        """
        rows = self._all_queue_rows(status)
        uids = list(dict.fromkeys(r["item_uid"] for r in rows))
        light: dict[str, tuple] = {}
        for a in range(0, len(uids), 500):
            part = uids[a:a + 500]
            marks = ",".join("?" * len(part))
            for r in self.bundle.fetchall(
                    "SELECT item_uid, item_id, local_date, local_time FROM items"
                    f" WHERE item_uid IN ({marks})", part):
                light[r[0]] = (r[1], r[2], r[3])
        out = []
        for r in rows:
            item_id, day, when = light.get(r["item_uid"], (None, None, None))
            out.append({"q": r, "id": item_id, "local_date": day, "local_time": when})
        if status == "approved":
            # Deletion mode walks the library in local-day order, like Google Photos.
            out.sort(key=lambda e: (e["local_date"] is None, e["local_date"] or "",
                                    e["local_time"] or "", e["id"] is None, e["id"] or 0))
        return out

    def _missing_out(self, r: dict) -> dict:
        """Display dict for a queue row whose item is not in this bundle."""
        return {"id": None, "uid": r["item_uid"], "filename": None, "missing": True,
                "photos_url": None, "link_conf": None, "flags": [], "protected": False,
                "possible_same_item": False, "local_date": None, "local_time": None,
                "day_uncertain": False, "queue": self._queue_out(r)}

    def _page_out(self, light: list[dict]) -> list[dict]:
        """Full display dicts for one page of light queue rows (same order)."""
        full = {it["uid"]: it for it in self.items_out(
            self.bundle.items([e["id"] for e in light if e["id"] is not None]))}
        return [full.get(e["q"]["item_uid"]) or self._missing_out(e["q"]) for e in light]

    def _day_stats(self, dates: list[str]) -> dict[str, tuple[int, int]]:
        """{date: (indexed photos that day, day_uncertain photos that day)}."""
        out: dict[str, tuple[int, int]] = {}
        dates = list(dict.fromkeys(dates))
        for a in range(0, len(dates), 500):
            part = dates[a:a + 500]
            marks = ",".join("?" * len(part))
            for r in self.bundle.fetchall(
                    "SELECT local_date, COUNT(*), COALESCE(SUM(day_uncertain), 0) FROM items"
                    f" WHERE local_date IN ({marks}) GROUP BY local_date", part):
                out[r[0]] = (int(r[1]), int(r[2]))
        return out

    def api_queue(self, q: _Query) -> dict:
        status = q.enum("status", LIST_STATUSES, "proposed")
        offset = q.int("offset", 0, 0, 10_000_000)
        limit = q.int("limit", 500, 1, QUEUE_MAX_LIMIT)
        rows = self._queue_light(status)
        page = self._page_out(rows[offset:offset + limit])
        res: dict = {**_page(len(rows), offset, len(page)), "status": status,
                     **self._state(), "trash_days": TRASH_DAYS}
        if status != "approved":
            res["rows"] = page
            return res
        # Day groups: counts cover ALL approved rows of each day, not just this page.
        per_day: dict[str | None, list[dict]] = {}
        for e in rows:
            per_day.setdefault(e["local_date"], []).append(e)
        not_indexed = self.bundle.videos_by_day()
        known = self._day_stats([d for d in per_day if d])
        days = []
        for d in dict.fromkeys(it["local_date"] for it in page):
            all_day = per_day[d]
            n_indexed, n_uncertain = known.get(d, (0, 0)) if d else (0, 0)
            n_approved = sum(1 for e in all_day if e["id"] is not None)
            n_not_indexed = not_indexed.get(d, 0) if d else 0
            day = {
                "date": d, "items": [it for it in page if it["local_date"] == d],
                "n_indexed_photos_that_day": n_indexed, "n_approved_that_day": n_approved,
                "n_deleted_that_day": sum(1 for e in all_day if e["q"]["deleted"]),
                # Items of that day missing from the index (videos AND skipped media);
                # ``videos_that_day`` is the same number under its original, narrower name.
                "n_not_indexed_that_day": n_not_indexed, "videos_that_day": n_not_indexed,
                "n_day_uncertain": n_uncertain,
            }
            if d:
                day.update(self._day_blockers(d, day))
            else:
                # Undated / missing-from-bundle rows: there is no day to select at all.
                day.update(safe_to_day_select=False, day_select_blockers=[],
                           n_undated_in_year=0, n_undated_unknown_year=0,
                           n_unindexed_undated=0)
            days.append(day)
        res["days"] = days
        res["n_deleted"] = sum(1 for e in rows if e["q"]["deleted"])
        return res

    def _day_blockers(self, d: str, day: dict) -> dict:
        """``safe_to_day_select`` for one dated day group, with the reasons it is not safe.

        Returns {"safe_to_day_select", "day_select_blockers" (codes from DAY_BLOCKERS),
        "n_undated_in_year", "n_undated_unknown_year", "n_unindexed_undated"}.
        """
        by_year, unknown_year, unindexed_undated = self._undated_counts()
        n_undated_year = by_year.get(int(d[:4]), 0)
        n_indexed = day["n_indexed_photos_that_day"]
        checks = {
            "no_indexed_photos": n_indexed == 0,
            "not_all_approved": n_indexed > 0 and day["n_approved_that_day"] != n_indexed,
            "not_indexed_that_day": day["n_not_indexed_that_day"] > 0,
            "day_uncertain": day["n_day_uncertain"] > 0,
            "undated_in_year": n_undated_year > 0,
            "undated_unknown_year": unknown_year > 0,
            "unindexed_undated": unindexed_undated > 0,
        }
        blockers = [code for code in DAY_BLOCKERS if checks[code]]
        return {"safe_to_day_select": not blockers, "day_select_blockers": blockers,
                "n_undated_in_year": n_undated_year, "n_undated_unknown_year": unknown_year,
                "n_unindexed_undated": unindexed_undated}

    def export_csv(self, q: _Query) -> bytes:
        status = q.enum("status", LIST_STATUSES, "approved")
        buf = io.StringIO()
        # BOM so Excel opens the file as UTF-8; QUOTE_MINIMAL handles commas/quotes/newlines.
        buf.write("\ufeff")
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(CSV_COLUMNS)
        rows = self._queue_light(status)
        # Plain item rows are enough here (no scores / memberships); fetched in chunks.
        for a in range(0, len(rows), 2000):
            part = rows[a:a + 2000]
            items = {it["item_uid"]: it for it in
                     self.bundle.items([e["id"] for e in part if e["id"] is not None])}
            for e in part:
                qd, it = e["q"], items.get(e["q"]["item_uid"], {})
                url = self.photos_link(it.get("url"), it.get("filename"))[0] if it else None
                w.writerow([csv_cell(v) for v in (
                    qd["item_uid"], _clean(it.get("filename")), it.get("local_date"),
                    it.get("local_time"), it.get("width"), it.get("height"),
                    it.get("file_size"), qd["category"], _clean(qd["reason"]),
                    qd["proposed_by"], qd["status"], int(qd["deleted"]), url)])
        return buf.getvalue().encode("utf-8")

    def thumb(self, item_id: int, size: str) -> bytes | None:
        return self.bundle.thumb(item_id, size)

    # ------------------------------------------------------------------ safety checks

    def _whole_group_violations(self, new_uids: set[str], item_ids: list[int],
                                conn: sqlite3.Connection | None = None) -> list[int]:
        """Dup groups whose EVERY member would be approved after approving ``new_uids``.

        ``conn`` is the review DB connection a guard received: the current approvals are then
        read inside that write transaction.
        """
        gids = sorted({m["dup_group"] for m in self.bundle.memberships(item_ids).values()
                       if m.get("dup_group") is not None})
        if not gids:
            return []
        members: dict[int, set[str]] = {}
        marks = ",".join("?" * len(gids))
        for r in self.bundle.fetchall(
                "SELECT m.group_id, i.item_uid FROM dup_members m JOIN items i"
                f" ON i.item_id = m.item_id WHERE m.group_id IN ({marks})", gids):
            members.setdefault(r[0], set()).add(r[1])
        everyone = sorted(set().union(*members.values()))
        approved = self.review.approved_among(everyone, conn) | new_uids
        return [g for g, uids in sorted(members.items()) if uids <= approved]

    def _check_approval(self, items: list[dict], override: bool,
                        conn: sqlite3.Connection | None = None) -> None:
        """W-13: refuse whole-group deletion; protected items need an explicit override.

        Protected: shared, partner-shared, favorited, or a member of a non-deletable dup
        group (possibly the same Google Photos item as its duplicate). Raises ApiError(409).
        Writers run it through :meth:`_guard`, i.e. inside the review DB's write transaction.
        """
        ids = [it["item_id"] for it in items]
        bad = self._whole_group_violations({it["item_uid"] for it in items}, ids, conn)
        if bad:
            raise ApiError(409, "whole_group", group_ids=bad)
        same = self._possible_same_items(ids)
        protected = [it["item_id"] for it in items
                     if it.get("shared") or it.get("partner") or it.get("favorited")
                     or it["item_id"] in same]
        if protected and not override:
            raise ApiError(409, "needs_override", ids=protected)

    def _guard(self, items: list[dict], override: bool):
        """A ReviewDB guard running :meth:`_check_approval` inside the write transaction.

        ReviewDB calls it after BEGIN IMMEDIATE, while this process holds the database write
        lock, so the approvals it reads cannot change (from this or another ``gpclean serve``
        process) before the approval is committed. Its ApiError rolls the write back.
        """
        def guard(conn: sqlite3.Connection) -> None:
            self._check_approval(items, override, conn)
        return guard

    def _items_for_uids(self, uids: list[str]) -> list[dict]:
        ids = self.bundle.uids_to_ids(uids)
        if len(ids) != len(uids):
            raise ApiError(404, "unknown_item")
        return self.bundle.items([ids[u] for u in uids])

    def _group_by_key(self, key: object) -> dict:
        key = _short_str(key, "group_key", 80)
        rows = self.bundle.fetchall("SELECT group_id FROM dup_groups WHERE group_key = ?", (key,))
        g = self.bundle.dup_group(rows[0][0]) if rows else None
        if g is None:
            raise ApiError(404, "unknown_group")
        return g

    # ------------------------------------------------------------------ POST endpoints

    def post_queue_add(self, body: dict) -> dict:
        ids = _int_list(body.get("ids"), "ids")
        override = _strict_bool(body.get("override", False), "override")
        reason = body.get("reason", DEFAULT_ADD_REASON)
        category = body.get("category")
        items = self.bundle.items(ids)
        if len(items) != len(ids):
            raise ApiError(404, "unknown_item")
        try:
            n = self.review.user_add([(it["item_uid"], reason, category) for it in items],
                                     guard=self._guard(items, override))
        except ValueError:
            raise ApiError(400, "bad_reason_or_category") from None
        return {"ok": True, "changed": n, **self._state()}

    def post_queue_decide(self, body: dict) -> dict:
        uids = _str_list(body.get("uids"), "uids")
        decision = body.get("decision")
        if decision not in DECISIONS:
            raise ApiError(400, "bad_param", param="decision")
        override = _strict_bool(body.get("override", False), "override")
        if decision == "approve":
            items = self._items_for_uids(uids)
            n = self.review.decide(uids, decision, guard=self._guard(items, override))
        else:
            n = self.review.decide(uids, decision)
        return {"ok": True, "changed": n, **self._state()}

    def post_reject_batch(self, body: dict) -> dict:
        batch_id = _short_str(body.get("batch_id"), "batch_id", 80)
        try:
            n = self.review.reject_batch(batch_id)
        except ValueError:
            raise ApiError(400, "bad_param", param="batch_id") from None
        return {"ok": True, "changed": n, **self._state()}

    def post_deleted(self, body: dict) -> dict:
        uids = _str_list(body.get("uids"), "uids")
        deleted = _strict_bool(body.get("deleted"), "deleted")
        n = self.review.mark_deleted(uids, deleted)
        return {"ok": True, "changed": n, **self._state()}

    def post_dups_keeper(self, body: dict) -> dict:
        g = self._group_by_key(body.get("group_key"))
        uid = _short_str(body.get("uid"), "uid", 1000)
        if uid not in {m["item_uid"] for m in g["members"]}:
            raise ApiError(400, "not_member")
        # A keeper that deletion mode would delete is a contradiction; undo its approval first.
        row = self.review.get([uid]).get(uid)
        if row is not None and row["status"] == "approved":
            raise ApiError(409, "keeper_approved")
        self.review.set_group_decision(g["group_key"], "keeper", uid)
        return {"ok": True, **self._state()}

    def post_dups_dismiss(self, body: dict) -> dict:
        g = self._group_by_key(body.get("group_key"))
        undo = _strict_bool(body.get("undo", False), "undo")
        self.review.set_group_decision(g["group_key"], "clear" if undo else "dismissed", None)
        return {"ok": True, **self._state()}

    def post_dups_queue(self, body: dict) -> dict:
        gids = _int_list(body.get("group_ids"), "group_ids", MAX_GROUPS)
        override = _strict_bool(body.get("override", False), "override")
        decisions = self.review.group_decisions()
        groups = []
        for gid in gids:
            g = self.bundle.dup_group(gid)
            if g is None:
                raise ApiError(404, "unknown_group")
            groups.append(g)
        refused = [g["group_id"] for g in groups if not g["deletable"]]
        if refused:
            raise ApiError(409, "not_deletable", group_ids=refused)
        dismissed = [g["group_id"] for g in groups
                     if self._group_decision(g, decisions)[0] == "dismissed"]
        if dismissed:
            raise ApiError(409, "dismissed", group_ids=dismissed)
        todo: list[tuple[dict, str]] = []
        for g in groups:
            _, keeper = self._group_decision(g, decisions)
            keeper_item = next(m for m in g["members"] if m["item_id"] == keeper)
            name = strip_unsafe(keeper_item.get("filename") or "")[:DUP_REASON_NAME_MAX]
            reason = f"duplicate of {name or '#' + str(keeper)}"
            todo += [(m, reason) for m in g["members"] if m["item_id"] != keeper]
        n = self.review.user_add([(m["item_uid"], reason, "dup_extra") for m, reason in todo],
                                 guard=self._guard([m for m, _ in todo], override))
        return {"ok": True, "changed": n, **self._state()}


# ---------------------------------------------------------------------------- HTTP layer

_GET_ROUTES = {
    "/api/stats": "api_stats", "/api/rev": "api_rev", "/api/dups": "api_dups",
    "/api/junk": "api_junk", "/api/search": "api_search", "/api/queue": "api_queue",
    "/api/item": "api_item",
}
_POST_ROUTES = {
    "/api/queue/add": "post_queue_add", "/api/queue/decide": "post_queue_decide",
    "/api/queue/reject_batch": "post_reject_batch", "/api/queue/deleted": "post_deleted",
    "/api/dups/keeper": "post_dups_keeper", "/api/dups/queue": "post_dups_queue",
    "/api/dups/dismiss": "post_dups_dismiss",
}


class SiteHandler(BaseHTTPRequestHandler):
    """HTTP plumbing and the request checks; the API itself lives in ``SiteApp``."""

    server_version = "gpclean"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30                 # a stalled client must not pin a thread forever
    server: "SiteServer"

    # --------------------------------------------------------------- responses

    def end_headers(self) -> None:
        # Every response, including errors, carries the security headers (W-6).
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _send(self, status: int, body: bytes, ctype: str, cache: str = NO_STORE,
              extra: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in extra:
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_api_error(self, err: ApiError) -> None:
        # The body of an unread or rejected request may still be on the socket; closing the
        # connection keeps it from being parsed as the next request.
        self.close_connection = True
        if err.code == "forbidden" and self._is_document_navigation():
            # A click on a link to the site from another page (e.g. a chat) is refused (W-3);
            # tell a person what to do instead of showing raw JSON. Fixed text, no input.
            port = self.server.app.port
            text = (f"gpclean: this page cannot be opened from a link on another website.\n"
                    f"Open http://127.0.0.1:{port}/ directly (type it into the address bar, "
                    f"or use the link printed by gpclean serve).\n")
            self._send(err.status, text.encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send_json(err.status, {"error": err.code, **err.extra})

    def _is_document_navigation(self) -> bool:
        mode = (self.headers.get("Sec-Fetch-Mode") or "").strip().lower()
        dest = (self.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        return self.command == "GET" and mode == "navigate" and dest == "document"

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        """Replace the stdlib HTML error page (which echoes request text) with fixed JSON."""
        self.close_connection = True
        try:
            phrase = HTTPStatus(code).phrase.lower().replace(" ", "_")
        except ValueError:
            phrase = "error"
        self._send_json(code, {"error": phrase})

    # --------------------------------------------------------------- logging

    def log_request(self, code: object = "-", size: object = "-") -> None:
        # Route label only: query strings can hold search text or filename filters.
        log.debug("%s %s %s", self.command, getattr(self, "_route", "-"), code)

    def log_error(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        log.info("http protocol error")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    # --------------------------------------------------------------- checks

    def _check_common(self) -> str:
        """W-2/W-3 checks shared by every method; returns the (allowed) Host."""
        hosts = self.headers.get_all("Host") or []
        host = hosts[0].strip().lower() if len(hosts) == 1 else ""
        if host not in self.server.app.allowed_hosts:
            raise ApiError(421, "misdirected_request")
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site.strip().lower() not in _ALLOWED_FETCH_SITES:
            raise ApiError(403, "forbidden")
        return host

    def _read_json_body(self, host: str) -> dict:
        """W-4 checks for POST, then the parsed JSON object."""
        app = self.server.app
        if self.headers.get("Origin") != f"http://{host}":
            raise ApiError(403, "bad_origin")
        token = self.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(token.encode("utf-8", "replace"),
                                   app.csrf_token.encode("ascii")):
            raise ApiError(403, "bad_csrf_token")
        if self.headers.get_content_type() != "application/json" or \
                (self.headers.get_content_charset() or "utf-8").lower() not in ("utf-8", "utf8"):
            raise ApiError(415, "unsupported_media_type")
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required")
        length = self.headers.get("Content-Length")
        if length is None:
            raise ApiError(411, "length_required")
        if not re.fullmatch(r"[0-9]{1,10}", length.strip()):
            raise ApiError(400, "bad_request")
        n = int(length)
        if n > MAX_BODY:
            raise ApiError(413, "too_large")
        raw = self.rfile.read(n)
        if len(raw) != n:
            raise ApiError(400, "bad_request")

        def no_constants(_: str) -> None:
            raise ValueError("NaN/Infinity not allowed")

        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=no_constants)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError(400, "bad_json") from None
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json")
        return body

    # --------------------------------------------------------------- dispatch

    def _handle(self, method: str) -> None:
        self._route = "-"
        app = self.server.app
        try:
            host = self._check_common()
            parts = urlsplit(self.path)
            path = parts.path
            if method == "GET":
                if self.headers.get("Content-Length", "0").strip() != "0" \
                        or self.headers.get("Transfer-Encoding"):
                    # We never read GET bodies; drop the connection so leftover bytes cannot
                    # be parsed as a second request.
                    self.close_connection = True
                if path in STATIC_FILES:
                    self._route = path
                    name, ctype = STATIC_FILES[path]
                    self._send(200, app.static[name], ctype)
                    return
                if path == "/favicon.ico":
                    # No icon; answer quietly instead of logging a 404 on every page load.
                    self._route = path
                    self._send(204, b"", "image/x-icon")
                    return
                m = _THUMB_RE.match(path)
                if m:
                    self._route = "/thumb"
                    data = app.thumb(int(m.group(1)), m.group(2))
                    if data is None:
                        raise ApiError(404, "not_found")
                    self._send(200, data, "image/webp", THUMB_CACHE)
                    return
                if path == "/api/export.csv":
                    self._route = path
                    body = app.export_csv(_Query(parts.query))
                    self._send(200, body, "text/csv; charset=utf-8", NO_STORE, (
                        ("Content-Disposition", 'attachment; filename="gpclean-queue.csv"'),))
                    return
                handler = _GET_ROUTES.get(path)
                if handler is None:
                    raise ApiError(404, "not_found")
                self._route = path
                self._send_json(200, getattr(app, handler)(_Query(parts.query)))
                return
            handler = _POST_ROUTES.get(path)
            if handler is None:
                raise ApiError(404, "not_found")
            self._route = path
            body = self._read_json_body(host)
            self._send_json(200, getattr(app, handler)(body))
        except ApiError as err:
            self._send_api_error(err)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            log.error("request failed on %s (%s)", self._route, type(exc).__name__)
            log.debug("request failure details", exc_info=True)
            self._send_api_error(ApiError(500, "internal_error"))

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        """Static files, thumbnails, CSV export and the read-only JSON API."""
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        """The state-changing JSON API (CSRF-protected)."""
        self._handle("POST")


class SiteServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that owns a ``SiteApp`` and closes it on ``server_close``."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets another process bind the same port and steal requests;
    # there we ask for exclusive use instead. On Linux it only skips TIME_WAIT on restart.
    allow_reuse_address = os.name != "nt"
    app: SiteApp

    def server_bind(self) -> None:
        """Bind without HTTPServer's reverse-DNS lookup (slow on some Windows networks)."""
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name = BIND_HOST
        self.server_port = self.server_address[1]

    def server_close(self) -> None:
        super().server_close()
        app = getattr(self, "app", None)
        if app is not None:
            app.close()


def make_server(home: Path, port: int = 8765, *, text_embedder=None) -> SiteServer:
    """Create (bind, not start) the review site for ``home`` on 127.0.0.1:``port``.

    ``port=0`` picks a free port (tests); the real port is ``server.server_address[1]``.
    ``text_embedder`` (tests) replaces the CLIP text model; otherwise call
    ``server.app.start_text_loader()`` to load it in the background.
    """
    server = SiteServer((BIND_HOST, int(port)), SiteHandler, bind_and_activate=True)
    try:
        server.app = SiteApp(Path(home), server.server_address[1], text_embedder=text_embedder)
    except BaseException:
        server.socket.close()
        raise
    return server


def _setup_site_logging(logs_dir: Path) -> None:
    """Log to <home>/state/logs/site.log (rotating); the console only gets the URL."""
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            logs_dir / "site.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)


def cli_serve(home, port, no_browser) -> int:
    """``gpclean serve``: run the review site until Ctrl+C."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    home = Path(home)
    _setup_site_logging(home_paths(home)["logs_dir"])
    try:
        server = make_server(home, port)
    except FileNotFoundError as exc:
        print(f"gpclean serve: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"gpclean serve: cannot listen on 127.0.0.1:{port} ({exc.strerror or exc}). "
              "Is another gpclean serve running? Try --port.", file=sys.stderr)
        return 2
    server.app.start_text_loader()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"gpclean review site: {url}  (press Ctrl+C to stop)", flush=True)
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - the URL is printed anyway
            log.info("could not open a browser")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        server.server_close()
    return 0


__all__ = ["make_server", "cli_serve", "SiteApp", "SiteServer", "csv_cell", "CSP"]
