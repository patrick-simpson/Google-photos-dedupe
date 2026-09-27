"""The local review queue (``<home>/state/review.sqlite``): the ONLY code that writes it.

Two kinds of callers share this file, often at the same time from different processes:

* the MCP server, on behalf of Claude, which may only *propose* items (and withdraw its own
  open proposals), and
* the review site, on behalf of the user, who approves, rejects, resets, adds items directly
  and marks items deleted.

Safety rules enforced here (not in the callers), so a bug or a prompt-injected Claude cannot
bypass them:

* ``propose`` inserts status ``'proposed'`` only. It never touches an existing row, never
  re-proposes a uid the user rejected, and stops at ``MAX_OPEN_PROPOSALS`` open proposals.
* ``withdraw`` removes only ``'proposed'`` rows created by that same proposer.
* There is no Claude-facing way to approve, reject or mark deleted; those are user methods.
* Every reason is sanitised (control, bidi and zero-width characters removed; 3..300 chars).

Concurrency: the DB is in WAL mode with a busy timeout, the connection is in autocommit mode
(``isolation_level=None``) and every write runs inside an explicit ``BEGIN IMMEDIATE``. Taking
the write lock up front matters in WAL: a deferred transaction that reads and then tries to
write can fail at once with SQLITE_BUSY_SNAPSHOT instead of waiting for the busy timeout.

Every write appends rows to ``events`` (the audit log). The ``state.rev`` counter is bumped
by triggers in ``schema.REVIEW_DDL`` on every change to ``queue`` or ``group_decisions``, so
the site can poll it cheaply.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from gpclean.schema import open_review

log = logging.getLogger(__name__)

MAX_OPEN_PROPOSALS = 2000
REASON_MIN = 3
REASON_MAX = 300
USER = "user"
STATUSES = ("proposed", "approved", "rejected")
LIST_STATUSES = (*STATUSES, "deleted", "all")
DECISIONS = ("approve", "reject", "reset")
GROUP_ACTIONS = ("dismissed", "keeper", "clear")

# A proposer is recorded by server code, never taken from tool input; the pattern is a guard
# against accidentally passing 'user' (or anything else) to the Claude-only entry points.
_PROPOSER_RE = re.compile(r"^claude(:[A-Za-z0-9._-]{1,64})?$")
_CATEGORY_RE = re.compile(r"^[a-z_]{1,32}$")
_BATCH_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_GROUP_KEY_RE = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")   # normally 40-hex sha1
_UID_MAX = 1000
# SQLite's host-parameter limit is large on modern builds, but chunking keeps us safe anywhere.
_CHUNK = 500

# Characters that can hide, reorder or smuggle text in the review UI, so they never reach the
# database. Written as escapes only (never literal characters) so the class reads the same in
# every editor and cannot itself be a Trojan Source trick:
#   C0/C1 controls and DEL (tab and line breaks are handled by _SPACE_RE instead),
#   U+00AD soft hyphen, U+061C Arabic letter mark, U+180E Mongolian vowel separator,
#   U+200B-U+200F zero-width chars and LRM/RLM, U+2028/U+2029 line/paragraph separators,
#   U+202A-U+202E bidi embeddings/overrides, U+2060-U+2064 word joiner and invisible
#   operators, U+2066-U+2069 bidi isolates, U+FEFF BOM, U+FFF9-U+FFFB interlinear annotation,
#   and the tag block U+E0000-U+E007F (invisible "ASCII smuggling" text).
_UNSAFE_RE = re.compile(
    "[\\u0000-\\u0008\\u000e-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u180e\\u200b-\\u200f"
    "\\u2028\\u2029\\u202a-\\u202e\\u2060-\\u2064\\u2066-\\u2069\\ufeff\\ufff9-\\ufffb"
    "\\U000e0000-\\U000e007f]"
)
_SPACE_RE = re.compile(r"[\t\n\v\f\r ]+")

T = TypeVar("T")


def strip_unsafe(text: str) -> str:
    """Remove control/bidi/zero-width characters and collapse whitespace to single spaces.

    Tabs and line breaks become spaces (so words stay apart); everything else unsafe is
    deleted. The result is stripped at both ends.
    """
    text = _UNSAFE_RE.sub("", text)
    return _SPACE_RE.sub(" ", text).strip()


def sanitise_reason(reason: object) -> str:
    """Return a cleaned reason or raise ValueError if it is not 3..300 characters after cleaning."""
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    cleaned = strip_unsafe(reason)
    if not (REASON_MIN <= len(cleaned) <= REASON_MAX):
        raise ValueError(f"reason must be {REASON_MIN}..{REASON_MAX} characters")
    return cleaned


def _now() -> str:
    """Current UTC time as ISO-8601 with a trailing 'Z' (seconds precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_uid(uid: object) -> str:
    if not isinstance(uid, str) or not uid or len(uid) > _UID_MAX:
        raise ValueError("item_uid must be a non-empty string")
    return uid


def _check_uids(uids: Iterable[str]) -> list[str]:
    """Validate and de-duplicate uids, keeping first-seen order."""
    if isinstance(uids, str):
        raise ValueError("uids must be a list of strings")
    return list(dict.fromkeys(_check_uid(u) for u in uids))


def _check_category(category: object) -> str | None:
    if category is None:
        return None
    if not isinstance(category, str) or not _CATEGORY_RE.match(category):
        raise ValueError("invalid category")
    return category


def _check_proposer(proposer: object) -> str:
    if not isinstance(proposer, str) or not _PROPOSER_RE.match(proposer):
        raise ValueError("proposer must look like 'claude:<client>'")
    return proposer


def _check_batch(batch_id: object) -> str:
    if not isinstance(batch_id, str) or not _BATCH_RE.match(batch_id):
        raise ValueError("invalid batch_id")
    return batch_id


def _chunks(seq: list[T], n: int = _CHUNK) -> Iterable[list[T]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """True for SQLITE_BUSY / SQLITE_LOCKED style errors (safe to retry)."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        # Primary result code is the low byte (extended codes such as BUSY_SNAPSHOT = 517).
        return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


class ReviewDB:
    """Read/write access to the review queue. Safe to share between threads of one process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn = self._open_with_retry()
        # One connection per ReviewDB; the site's threads share it, so serialise all use.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ plumbing

    def _open_with_retry(self) -> sqlite3.Connection:
        """Open (creating if needed) the review DB, retrying briefly while it is locked.

        When the site and the MCP server start together on a fresh home, both may try to
        create the file and switch it to WAL at the same moment; SQLite then reports
        "database is locked" to the loser (busy_timeout does not cover the journal-mode
        switch). A few short retries let the other process finish creating it.
        """
        attempts = 5
        for attempt in range(attempts):
            try:
                return open_review(self.path)
            except sqlite3.OperationalError as exc:
                if attempt == attempts - 1 or not _is_busy(exc):
                    raise
                log.warning("review db locked while opening; retrying")
                time.sleep(0.2)
        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        """Close the connection. The object must not be used afterwards."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ReviewDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _write(self, fn: Callable[[sqlite3.Connection, str], T]) -> T:
        """Run ``fn(conn, now)`` inside BEGIN IMMEDIATE ... COMMIT, retrying once if busy.

        busy_timeout already makes BEGIN IMMEDIATE wait up to 5 s for the write lock; the one
        retry covers the rare case where a long writer in another process outlasts that.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if attempt == 1 and _is_busy(exc):
                        log.warning("review db busy; retrying write once")
                        time.sleep(0.2)
                        continue
                    raise
                try:
                    result = fn(self._conn, _now())
                    self._conn.execute("COMMIT")
                    return result
                except BaseException as exc:
                    # SQLite rolls back by itself on some errors (disk full, I/O error); a
                    # second ROLLBACK would then raise and hide the real cause.
                    if self._conn.in_transaction:
                        try:
                            self._conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            log.warning("review db rollback failed")
                    if (attempt == 1 and isinstance(exc, sqlite3.OperationalError)
                            and _is_busy(exc)):
                        log.warning("review db busy during write; retrying once")
                        time.sleep(0.2)
                        continue
                    raise
            raise AssertionError("unreachable")  # pragma: no cover

    def _read(self, sql: str, params: Iterable[object] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    @staticmethod
    def _event(conn: sqlite3.Connection, now: str, actor: str, action: str,
               uid: str | None, detail: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO events(ts, actor, action, item_uid, detail) VALUES (?, ?, ?, ?, ?)",
            (now, actor, action, uid,
             json.dumps(detail, sort_keys=True, ensure_ascii=False) if detail else None))

    @staticmethod
    def _status_of(conn: sqlite3.Connection, uids: list[str]) -> dict[str, sqlite3.Row]:
        """Current rows for ``uids`` (inside the caller's transaction)."""
        out: dict[str, sqlite3.Row] = {}
        for part in _chunks(uids):
            marks = ",".join("?" * len(part))
            for row in conn.execute(
                    f"SELECT * FROM queue WHERE item_uid IN ({marks})", part):
                out[row["item_uid"]] = row
        return out

    # ------------------------------------------------------------------ reads

    def rev(self) -> int:
        """Change counter; bumped on every queue / group-decision change."""
        row = self._read("SELECT value FROM state WHERE key = 'rev'")
        return int(row[0][0]) if row else 0

    def list(self, status: str, *, offset: int = 0, limit: int = 100) -> list[dict]:
        """Queue rows by status: proposed | approved | rejected | deleted | all.

        ``approved`` includes rows already marked deleted (deletion mode shows both);
        ``deleted`` is the approved rows marked deleted. Ordered by proposal time.
        """
        if status not in LIST_STATUSES:
            raise ValueError("status must be one of " + ", ".join(LIST_STATUSES))
        if not (isinstance(limit, int) and 1 <= limit <= 100_000):
            raise ValueError("limit out of range")
        if not (isinstance(offset, int) and offset >= 0):
            raise ValueError("offset out of range")
        where, params = {
            "all": ("1", ()),
            "deleted": ("deleted = 1", ()),
        }.get(status, ("status = ?", (status,)))
        rows = self._read(
            f"SELECT * FROM queue WHERE {where} ORDER BY proposed_at, item_uid LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return [dict(r) for r in rows]

    def get(self, uids: list[str]) -> dict[str, dict]:
        """Queue rows for the given uids (missing uids are simply absent)."""
        uids = _check_uids(uids)
        out: dict[str, dict] = {}
        for part in _chunks(uids):
            marks = ",".join("?" * len(part))
            for row in self._read(f"SELECT * FROM queue WHERE item_uid IN ({marks})", part):
                out[row["item_uid"]] = dict(row)
        return out

    def uids(self, statuses: Iterable[str] = STATUSES) -> set[str]:
        """All uids whose status is in ``statuses`` (used by search's exclude_queued)."""
        statuses = list(statuses)
        if any(s not in STATUSES for s in statuses):
            raise ValueError("unknown status")
        if not statuses:
            return set()
        marks = ",".join("?" * len(statuses))
        return {r[0] for r in self._read(
            f"SELECT item_uid FROM queue WHERE status IN ({marks})", statuses)}

    def counts(self) -> dict:
        """Counts for the site header and MCP stats."""
        row = self._read(
            "SELECT"
            " COALESCE(SUM(status = 'proposed'), 0),"
            " COALESCE(SUM(status = 'approved'), 0),"
            " COALESCE(SUM(status = 'rejected'), 0),"
            " COALESCE(SUM(deleted = 1), 0),"
            " COALESCE(SUM(status = 'approved' AND deleted = 0), 0)"
            " FROM queue")[0]
        keys = ("proposed", "approved", "rejected", "deleted", "approved_not_deleted")
        return {k: int(v) for k, v in zip(keys, row)}

    def batches(self) -> list[dict]:
        """Claude proposal batches that still have open proposals (newest first)."""
        rows = self._read(
            "SELECT batch_id, proposed_by, COUNT(*) AS n_open, MIN(proposed_at) AS first_at"
            " FROM queue WHERE status = 'proposed' AND batch_id IS NOT NULL"
            " GROUP BY batch_id, proposed_by ORDER BY first_at DESC, batch_id")
        return [dict(r) for r in rows]

    def events(self, *, limit: int = 100) -> list[dict]:
        """Most recent audit events, newest first."""
        if not (isinstance(limit, int) and 1 <= limit <= 10_000):
            raise ValueError("limit out of range")
        rows = self._read("SELECT rowid AS id, * FROM events ORDER BY rowid DESC LIMIT ?",
                          (limit,))
        return [dict(r) for r in rows]

    def group_decisions(self) -> dict[str, dict]:
        """{group_key: {"action", "keeper_uid", "decided_at"}} for every decided group."""
        rows = self._read("SELECT * FROM group_decisions")
        return {r["group_key"]: {"action": r["action"], "keeper_uid": r["keeper_uid"],
                                 "decided_at": r["decided_at"]} for r in rows}

    # ------------------------------------------------------------------ Claude writes

    def propose(self, items: list[tuple[str, str, str | None]], *, proposer: str,
                batch_id: str) -> dict:
        """Add Claude proposals. Returns {"added", "already", "rejected_skipped", "capped"}.

        Existing rows (any status) are left untouched: ``already`` counts proposed/approved
        ones, ``rejected_skipped`` counts rejected ones. Once ``MAX_OPEN_PROPOSALS`` rows are
        open, the remaining items are not inserted and are counted in ``capped``. All reasons
        are validated before anything is written, so a bad reason rejects the whole call.
        """
        proposer = _check_proposer(proposer)
        batch_id = _check_batch(batch_id)
        clean: list[tuple[str, str, str | None]] = []
        for item in items:
            uid, reason, category = item
            clean.append((_check_uid(uid), sanitise_reason(reason), _check_category(category)))

        def run(conn: sqlite3.Connection, now: str) -> dict:
            res = {"added": 0, "already": 0, "rejected_skipped": 0, "capped": 0}
            n_open = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE status = 'proposed'").fetchone()[0]
            existing = self._status_of(conn, list(dict.fromkeys(u for u, _, _ in clean)))
            seen: set[str] = set()
            for uid, reason, category in clean:
                row = existing.get(uid)
                if row is not None and row["status"] == "rejected":
                    res["rejected_skipped"] += 1
                    continue
                if row is not None or uid in seen:
                    res["already"] += 1
                    continue
                if n_open >= MAX_OPEN_PROPOSALS:
                    res["capped"] += 1
                    continue
                conn.execute(
                    "INSERT INTO queue(item_uid, status, proposed_by, batch_id, reason, category,"
                    " proposed_at) VALUES (?, 'proposed', ?, ?, ?, ?, ?)",
                    (uid, proposer, batch_id, reason, category, now))
                self._event(conn, now, proposer, "propose", uid,
                            {"batch_id": batch_id, "category": category})
                seen.add(uid)
                n_open += 1
                res["added"] += 1
            # One summary row per call, so refused/capped attempts are audited too.
            self._event(conn, now, proposer, "propose_batch", None, {"batch_id": batch_id, **res})
            return res

        res = self._write(run)
        log.info("propose: %s", res)
        return res

    def withdraw(self, uids: list[str], *, proposer: str) -> int:
        """Delete this proposer's own still-open proposals. Returns rows removed."""
        proposer = _check_proposer(proposer)
        uids = _check_uids(uids)

        def run(conn: sqlite3.Connection, now: str) -> int:
            n = 0
            for uid in uids:
                cur = conn.execute(
                    "DELETE FROM queue WHERE item_uid = ? AND status = 'proposed'"
                    " AND proposed_by = ?", (uid, proposer))
                if cur.rowcount:
                    self._event(conn, now, proposer, "withdraw", uid)
                    n += 1
            return n

        return self._write(run)

    # ------------------------------------------------------------------ user writes

    def user_add(self, items: list[tuple[str, str, str | None]]) -> int:
        """The user queues items directly: they become (or stay) ``'approved'``.

        New rows get ``proposed_by='user'``. An existing proposed or rejected row is upgraded
        to approved (the user's latest action wins) but keeps its original proposer and reason,
        so the audit trail still shows who suggested it and why. Returns rows inserted or
        changed.
        """
        clean = [(_check_uid(u), sanitise_reason(r), _check_category(c)) for u, r, c in items]

        def run(conn: sqlite3.Connection, now: str) -> int:
            n = 0
            existing = self._status_of(conn, list(dict.fromkeys(u for u, _, _ in clean)))
            for uid, reason, category in clean:
                row = existing.get(uid)
                if row is None:
                    conn.execute(
                        "INSERT INTO queue(item_uid, status, proposed_by, batch_id, reason,"
                        " category, proposed_at, decided_at)"
                        " VALUES (?, 'approved', 'user', NULL, ?, ?, ?, ?)",
                        (uid, reason, category, now, now))
                    self._event(conn, now, USER, "add", uid, {"category": category})
                elif row["status"] != "approved":
                    conn.execute(
                        "UPDATE queue SET status = 'approved', decided_at = ? WHERE item_uid = ?",
                        (now, uid))
                    self._event(conn, now, USER, "approve", uid, {"via": "add"})
                else:
                    continue
                # Refresh so a uid repeated within the same call is not counted twice.
                existing[uid] = conn.execute(
                    "SELECT * FROM queue WHERE item_uid = ?", (uid,)).fetchone()
                n += 1
            return n

        return self._write(run)

    def decide(self, uids: list[str], decision: str) -> int:
        """User decision on existing rows: approve | reject | reset. Returns rows changed.

        ``reset`` undoes a decision: Claude's rows go back to ``'proposed'``; rows the user
        added are deleted (there is nothing to go back to). Rows already marked deleted are
        skipped; unmark them with ``mark_deleted(..., False)`` first, so the record of what
        was deleted in Google Photos is never lost by a stray keypress.
        """
        if decision not in DECISIONS:
            raise ValueError("decision must be approve, reject or reset")
        uids = _check_uids(uids)

        def run(conn: sqlite3.Connection, now: str) -> int:
            n = 0
            rows = self._status_of(conn, uids)
            for uid in uids:
                row = rows.get(uid)
                if row is None or row["deleted"]:
                    continue
                if decision in ("approve", "reject"):
                    new = "approved" if decision == "approve" else "rejected"
                    if row["status"] == new:
                        continue
                    conn.execute("UPDATE queue SET status = ?, decided_at = ? WHERE item_uid = ?",
                                 (new, now, uid))
                elif row["proposed_by"] == USER:
                    conn.execute("DELETE FROM queue WHERE item_uid = ?", (uid,))
                elif row["status"] != "proposed":
                    conn.execute("UPDATE queue SET status = 'proposed', decided_at = NULL"
                                 " WHERE item_uid = ?", (uid,))
                else:
                    continue
                self._event(conn, now, USER, decision, uid, {"from": row["status"]})
                n += 1
            return n

        return self._write(run)

    def reject_batch(self, batch_id: str) -> int:
        """Reject every still-open proposal of one Claude batch. Returns rows changed."""
        batch_id = _check_batch(batch_id)

        def run(conn: sqlite3.Connection, now: str) -> int:
            uids = [r[0] for r in conn.execute(
                "SELECT item_uid FROM queue WHERE batch_id = ? AND status = 'proposed'",
                (batch_id,))]
            for uid in uids:
                conn.execute("UPDATE queue SET status = 'rejected', decided_at = ?"
                             " WHERE item_uid = ?", (now, uid))
                self._event(conn, now, USER, "reject", uid, {"batch_id": batch_id})
            return len(uids)

        return self._write(run)

    def mark_deleted(self, uids: list[str], deleted: bool) -> int:
        """Record that the user deleted (or un-deleted) approved items in Google Photos.

        Only approved rows can be marked. Returns rows changed.
        """
        if not isinstance(deleted, bool):
            raise ValueError("deleted must be a bool")
        uids = _check_uids(uids)

        def run(conn: sqlite3.Connection, now: str) -> int:
            n = 0
            for uid in uids:
                if deleted:
                    cur = conn.execute(
                        "UPDATE queue SET deleted = 1, deleted_at = ? WHERE item_uid = ?"
                        " AND status = 'approved' AND deleted = 0", (now, uid))
                else:
                    cur = conn.execute(
                        "UPDATE queue SET deleted = 0, deleted_at = NULL WHERE item_uid = ?"
                        " AND deleted = 1", (uid,))
                if cur.rowcount:
                    self._event(conn, now, USER, "deleted" if deleted else "undeleted", uid)
                    n += 1
            return n

        return self._write(run)

    def set_group_decision(self, group_key: str, action: str, keeper_uid: str | None) -> None:
        """Remember a duplicate-group decision: 'keeper' (with keeper_uid), 'dismissed'
        (not duplicates), or 'clear' to forget any earlier decision."""
        if not isinstance(group_key, str) or not _GROUP_KEY_RE.match(group_key):
            raise ValueError("invalid group_key")
        if action not in GROUP_ACTIONS:
            raise ValueError("action must be dismissed, keeper or clear")
        if action == "keeper":
            keeper_uid = _check_uid(keeper_uid)
        else:
            keeper_uid = None

        def run(conn: sqlite3.Connection, now: str) -> None:
            if action == "clear":
                conn.execute("DELETE FROM group_decisions WHERE group_key = ?", (group_key,))
            else:
                conn.execute(
                    "INSERT INTO group_decisions(group_key, action, keeper_uid, decided_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT(group_key) DO UPDATE SET"
                    " action = excluded.action, keeper_uid = excluded.keeper_uid,"
                    " decided_at = excluded.decided_at",
                    (group_key, action, keeper_uid, now))
            self._event(conn, now, USER, "group_" + action, keeper_uid, {"group_key": group_key})

        self._write(run)
