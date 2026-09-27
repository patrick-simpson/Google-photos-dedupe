"""Tests for gpclean.review_db: every queue safety rule, plus concurrent writers."""

from __future__ import annotations

import multiprocessing
import sqlite3
import time
from pathlib import Path

import pytest

from gpclean import review_db as rdb
from gpclean.review_db import MAX_OPEN_PROPOSALS, ReviewDB, sanitise_reason, strip_unsafe

CLAUDE = "claude:code"
OTHER = "claude:desktop"


@pytest.fixture
def db(tmp_path):
    d = ReviewDB(tmp_path / "state" / "review.sqlite")
    yield d
    d.close()


def uid(n: int) -> str:
    return f"g:AF1QipFAKE{n:030d}"


def items(ns, reason="blurry accidental shot", category="blur"):
    return [(uid(n), reason, category) for n in ns]


def status(db, n):
    return db.get([uid(n)])[uid(n)]["status"]


# ---------------------------------------------------------------- sanitising

def test_strip_unsafe_removes_controls_bidi_zero_width():
    s = "a\u202eb\u200bc\u2066d\u0007e\u0085f\u200fg\ufeffh"
    assert strip_unsafe(s) == "abcdefgh"
    assert strip_unsafe("  line1\nline2\t\tend  ") == "line1 line2 end"


@pytest.mark.parametrize("ch", ["\u061c", "\u2028", "\u2029", "\u2062", "\ufffa",
                                "\U000e0041", "\U000e007f", "\u00ad"])
def test_strip_unsafe_removes_invisible_and_tag_chars(ch):
    # U+E0041 is TAG LATIN CAPITAL A: renders as nothing ("ASCII smuggling").
    assert strip_unsafe(f"ab{ch}cd") == "abcd"


def test_strip_unsafe_keeps_ordinary_unicode():
    assert strip_unsafe("caf\u00e9 \u65e5\u672c \U0001f600") == "caf\u00e9 \u65e5\u672c \U0001f600"


def test_unsafe_pattern_source_is_ascii():
    # The character class must be readable in review, not hidden literal bidi characters.
    assert rdb._UNSAFE_RE.pattern.isascii()
    assert Path(rdb.__file__).read_text(encoding="utf-8").isascii()


@pytest.mark.parametrize("bad", ["", "ab", "  a\u200b\u200bb  ", "x" * 301, None, 12])
def test_sanitise_reason_rejects_bad_lengths_and_types(bad):
    with pytest.raises(ValueError):
        sanitise_reason(bad)


def test_sanitise_reason_bounds():
    assert sanitise_reason(" abc ") == "abc"
    assert sanitise_reason("x" * 300) == "x" * 300
    # Unsafe characters do not count towards the limit.
    assert sanitise_reason("\u202e" * 50 + "y" * 300) == "y" * 300


def test_propose_stores_sanitised_reason_and_rejects_whole_call_on_bad_reason(db):
    db.propose([(uid(1), "screen\u202eshot\nof chat", "screenshot")], proposer=CLAUDE,
               batch_id="b1")
    assert db.get([uid(1)])[uid(1)]["reason"] == "screenshot of chat"
    with pytest.raises(ValueError):
        db.propose([(uid(2), "fine reason", None), (uid(3), "no", None)],
                   proposer=CLAUDE, batch_id="b2")
    assert db.get([uid(2), uid(3)]) == {}


# ---------------------------------------------------------------- propose / Claude rules

def test_propose_inserts_proposed_only_with_batch_and_time(db):
    res = db.propose(items([1, 2]), proposer=CLAUDE, batch_id="batch-1")
    assert res == {"added": 2, "already": 0, "rejected_skipped": 0, "capped": 0}
    row = db.get([uid(1)])[uid(1)]
    assert row["status"] == "proposed"
    assert row["proposed_by"] == CLAUDE
    assert row["batch_id"] == "batch-1"
    assert row["category"] == "blur"
    assert row["proposed_at"].endswith("Z") and row["decided_at"] is None
    assert row["deleted"] == 0


def test_claude_cannot_approve(db):
    # The Claude path has no approval knob at all: propose always lands as 'proposed',
    # and it never upgrades or overwrites an existing row.
    db.propose(items([1]), proposer=CLAUDE, batch_id="b1")
    assert status(db, 1) == "proposed"
    res = db.propose(items([1]), proposer=CLAUDE, batch_id="b2")
    assert res["already"] == 1 and status(db, 1) == "proposed"
    assert db.get([uid(1)])[uid(1)]["batch_id"] == "b1"
    for bad in ("user", "USER", "claude:", "claude:bad name", "", "anthropic"):
        with pytest.raises(ValueError):
            db.propose(items([9]), proposer=bad, batch_id="b3")
    assert db.get([uid(9)]) == {}


def test_propose_never_touches_approved_or_rejected(db):
    db.user_add(items([1], reason="user approved this"))
    db.propose(items([2]), proposer=CLAUDE, batch_id="b1")
    db.decide([uid(2)], "reject")
    res = db.propose(items([1, 2, 3], reason="new claude reason"), proposer=CLAUDE,
                     batch_id="b2")
    assert res == {"added": 1, "already": 1, "rejected_skipped": 1, "capped": 0}
    assert status(db, 1) == "approved"
    assert db.get([uid(1)])[uid(1)]["reason"] == "user approved this"
    assert status(db, 2) == "rejected"
    assert db.get([uid(2)])[uid(2)]["reason"] == "blurry accidental shot"


def test_rejected_uid_is_never_reproposed(db):
    db.propose(items([5]), proposer=CLAUDE, batch_id="b1")
    db.decide([uid(5)], "reject")
    for proposer in (CLAUDE, OTHER):
        res = db.propose(items([5]), proposer=proposer, batch_id="b9")
        assert res["rejected_skipped"] == 1 and res["added"] == 0
    assert status(db, 5) == "rejected"


def test_duplicate_uids_in_one_call(db):
    res = db.propose(items([1, 1, 2]), proposer=CLAUDE, batch_id="b1")
    assert res == {"added": 2, "already": 1, "rejected_skipped": 0, "capped": 0}


def test_cap_on_open_proposals(db, monkeypatch):
    monkeypatch.setattr(rdb, "MAX_OPEN_PROPOSALS", 5)
    res = db.propose(items(range(3)), proposer=CLAUDE, batch_id="b1")
    assert res["added"] == 3
    res = db.propose(items(range(3, 10)), proposer=OTHER, batch_id="b2")
    assert res == {"added": 2, "already": 0, "rejected_skipped": 0, "capped": 5}
    assert db.counts()["proposed"] == 5
    # Deciding frees room: approved rows are no longer "open".
    db.decide([uid(0), uid(1)], "approve")
    res = db.propose(items(range(10, 13)), proposer=CLAUDE, batch_id="b3")
    assert res["added"] == 2 and res["capped"] == 1


def test_default_cap_is_2000(db):
    assert MAX_OPEN_PROPOSALS == 2000
    res = db.propose(items(range(2003)), proposer=CLAUDE, batch_id="big")
    assert res["added"] == 2000 and res["capped"] == 3
    assert db.counts()["proposed"] == 2000


def test_withdraw_only_own_open_proposals(db):
    db.propose(items([1, 2, 3]), proposer=CLAUDE, batch_id="b1")
    db.propose(items([4]), proposer=OTHER, batch_id="b2")
    db.user_add(items([5]))
    db.decide([uid(3)], "approve")
    n = db.withdraw([uid(1), uid(3), uid(4), uid(5), uid(99)], proposer=CLAUDE)
    assert n == 1
    assert db.get([uid(1)]) == {}
    assert status(db, 3) == "approved"      # decided rows stay
    assert status(db, 4) == "proposed"      # another client's proposal stays
    assert status(db, 5) == "approved"      # user rows stay
    with pytest.raises(ValueError):
        db.withdraw([uid(5)], proposer="user")


# ---------------------------------------------------------------- user actions

def test_user_add_inserts_approved_and_upgrades(db):
    assert db.user_add(items([1], reason="duplicate of #3")) == 1
    row = db.get([uid(1)])[uid(1)]
    assert (row["status"], row["proposed_by"]) == ("approved", "user")
    assert row["decided_at"] is not None
    db.propose(items([2]), proposer=CLAUDE, batch_id="b1")
    db.propose(items([3]), proposer=CLAUDE, batch_id="b1")
    db.decide([uid(3)], "reject")
    # Proposed and rejected rows are upgraded (the user's latest action wins);
    # already-approved rows are left alone and not counted.
    assert db.user_add(items([1, 2, 3])) == 2
    assert [status(db, n) for n in (1, 2, 3)] == ["approved"] * 3
    assert db.get([uid(2)])[uid(2)]["proposed_by"] == CLAUDE   # provenance kept
    with pytest.raises(ValueError):
        db.user_add([(uid(4), "x", None)])


def test_decide_approve_reject(db):
    db.propose(items([1, 2]), proposer=CLAUDE, batch_id="b1")
    assert db.decide([uid(1), uid(2), uid(77)], "approve") == 2
    assert db.decide([uid(1)], "approve") == 0          # no-op
    assert db.decide([uid(1)], "reject") == 1
    assert status(db, 1) == "rejected" and status(db, 2) == "approved"
    with pytest.raises(ValueError):
        db.decide([uid(1)], "delete")


def test_reset_semantics(db):
    db.propose(items([1, 2, 3]), proposer=CLAUDE, batch_id="b1")
    db.decide([uid(1)], "approve")
    db.decide([uid(2)], "reject")
    db.user_add(items([4]))
    assert db.decide([uid(1), uid(2), uid(3), uid(4)], "reset") == 3
    row1 = db.get([uid(1)])[uid(1)]
    assert row1["status"] == "proposed" and row1["decided_at"] is None
    assert status(db, 2) == "proposed"
    assert status(db, 3) == "proposed"                  # was already proposed: unchanged
    assert db.get([uid(4)]) == {}                       # user-added row removed


def test_decide_skips_deleted_rows(db):
    db.user_add(items([1]))
    db.mark_deleted([uid(1)], True)
    assert db.decide([uid(1)], "reject") == 0
    assert db.decide([uid(1)], "reset") == 0
    assert status(db, 1) == "approved"
    db.mark_deleted([uid(1)], False)
    assert db.decide([uid(1)], "reject") == 1


def test_reject_batch(db):
    db.propose(items([1, 2, 3]), proposer=CLAUDE, batch_id="b1")
    db.propose(items([4]), proposer=CLAUDE, batch_id="b2")
    db.decide([uid(1)], "approve")
    assert db.reject_batch("b1") == 2
    assert [status(db, n) for n in (1, 2, 3, 4)] == ["approved", "rejected", "rejected",
                                                      "proposed"]
    assert db.reject_batch("b1") == 0
    assert [b["batch_id"] for b in db.batches()] == ["b2"]


def test_mark_deleted_only_approved(db):
    db.propose(items([1]), proposer=CLAUDE, batch_id="b1")
    db.user_add(items([2]))
    assert db.mark_deleted([uid(1), uid(2)], True) == 1
    row = db.get([uid(2)])[uid(2)]
    assert row["deleted"] == 1 and row["deleted_at"].endswith("Z")
    assert db.get([uid(1)])[uid(1)]["deleted"] == 0
    assert db.mark_deleted([uid(2)], True) == 0         # idempotent
    assert db.mark_deleted([uid(2)], False) == 1
    row = db.get([uid(2)])[uid(2)]
    assert row["deleted"] == 0 and row["deleted_at"] is None
    with pytest.raises(ValueError):
        db.mark_deleted([uid(2)], 1)


# ---------------------------------------------------------------- reads

def test_list_and_counts(db):
    db.propose(items([1, 2, 3]), proposer=CLAUDE, batch_id="b1")
    db.user_add(items([4, 5]))
    db.decide([uid(1)], "reject")
    db.mark_deleted([uid(4)], True)
    assert db.counts() == {"proposed": 2, "approved": 2, "rejected": 1, "deleted": 1,
                           "approved_not_deleted": 1}
    assert {r["item_uid"] for r in db.list("proposed")} == {uid(2), uid(3)}
    assert {r["item_uid"] for r in db.list("approved")} == {uid(4), uid(5)}
    assert [r["item_uid"] for r in db.list("deleted")] == [uid(4)]
    assert [r["item_uid"] for r in db.list("rejected")] == [uid(1)]
    assert len(db.list("all")) == 5
    page1 = db.list("all", limit=2)
    page2 = db.list("all", offset=2, limit=2)
    assert len(page1) == 2 and len(page2) == 2
    assert not {r["item_uid"] for r in page1} & {r["item_uid"] for r in page2}
    assert db.uids(["proposed"]) == {uid(2), uid(3)}
    assert db.uids() == {uid(n) for n in range(1, 6)}
    with pytest.raises(ValueError):
        db.list("bogus")


def test_empty_counts(db):
    assert db.counts() == {"proposed": 0, "approved": 0, "rejected": 0, "deleted": 0,
                           "approved_not_deleted": 0}
    assert db.rev() == 0


def test_group_decisions(db):
    key = "a" * 40
    db.set_group_decision(key, "keeper", uid(1))
    assert db.group_decisions()[key]["keeper_uid"] == uid(1)
    db.set_group_decision(key, "dismissed", None)
    d = db.group_decisions()[key]
    assert d["action"] == "dismissed" and d["keeper_uid"] is None
    db.set_group_decision(key, "clear", None)
    assert db.group_decisions() == {}
    with pytest.raises(ValueError):
        db.set_group_decision(key, "keeper", None)
    with pytest.raises(ValueError):
        db.set_group_decision(key, "bogus", None)
    with pytest.raises(ValueError):
        db.set_group_decision("bad key; drop", "dismissed", None)


# ---------------------------------------------------------------- rev + audit

def test_rev_increments_on_every_write(db):
    revs = [db.rev()]

    def step(fn):
        fn()
        revs.append(db.rev())
        assert revs[-1] > revs[-2]

    step(lambda: db.propose(items([1, 2]), proposer=CLAUDE, batch_id="b1"))
    step(lambda: db.withdraw([uid(2)], proposer=CLAUDE))
    step(lambda: db.user_add(items([3])))
    step(lambda: db.decide([uid(1)], "approve"))
    step(lambda: db.decide([uid(1)], "reset"))
    step(lambda: db.reject_batch("b1"))
    step(lambda: db.mark_deleted([uid(3)], True))
    step(lambda: db.mark_deleted([uid(3)], False))
    step(lambda: db.set_group_decision("b" * 40, "dismissed", None))
    # No-op writes change nothing and so do not bump rev.
    before = db.rev()
    db.decide([uid(99)], "approve")
    assert db.rev() == before


def test_events_audit_every_write(db):
    db.propose(items([1, 2]), proposer=CLAUDE, batch_id="b1")
    db.withdraw([uid(2)], proposer=CLAUDE)
    db.user_add(items([3]))
    db.decide([uid(1)], "approve")
    db.mark_deleted([uid(1)], True)
    db.set_group_decision("c" * 40, "keeper", uid(3))
    ev = list(reversed(db.events(limit=50)))
    actions = [(e["actor"], e["action"], e["item_uid"]) for e in ev]
    assert actions == [
        (CLAUDE, "propose", uid(1)), (CLAUDE, "propose", uid(2)),
        (CLAUDE, "propose_batch", None), (CLAUDE, "withdraw", uid(2)),
        ("user", "add", uid(3)), ("user", "approve", uid(1)), ("user", "deleted", uid(1)),
        ("user", "group_keeper", uid(3)),
    ]
    assert all(e["ts"].endswith("Z") for e in ev)
    # The capped/skipped outcome of a propose call is audited too.
    assert '"added": 2' in ev[2]["detail"]


def test_wal_and_timeout_settings(db):
    conn = db._conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.isolation_level is None


def test_write_retries_once_when_busy(db, monkeypatch):
    calls = {"n": 0}
    real = db._conn

    class Flaky:
        """Connection proxy whose first BEGIN IMMEDIATE fails with 'database is locked'."""

        def execute(self, sql, *a):
            if sql == "BEGIN IMMEDIATE" and calls["n"] == 0:
                calls["n"] += 1
                raise sqlite3.OperationalError("database is locked")
            return real.execute(sql, *a)

    monkeypatch.setattr(db, "_conn", Flaky())
    monkeypatch.setattr(rdb.time, "sleep", lambda s: None)
    assert db.propose(items([1]), proposer=CLAUDE, batch_id="b1")["added"] == 1
    assert calls["n"] == 1


def test_reopen_keeps_state(tmp_path):
    path = tmp_path / "review.sqlite"
    with ReviewDB(path) as a:
        a.propose(items([1]), proposer=CLAUDE, batch_id="b1")
        rev = a.rev()
    with ReviewDB(path) as b:
        assert b.rev() == rev and status(b, 1) == "proposed"


# ---------------------------------------------------------------- concurrency

def _claude_worker(path: str, start: int, n: int) -> None:
    """Child process: propose one item at a time, withdrawing every third."""
    with ReviewDB(Path(path)) as d:
        for k in range(start, start + n):
            d.propose([(uid(k), "concurrent proposal", None)], proposer=CLAUDE,
                      batch_id=f"w{start}")
            if k % 3 == 0:
                d.withdraw([uid(k)], proposer=CLAUDE)


def _user_worker(path: str, n: int) -> None:
    """Child process: approve/reject whatever is proposed, and add items of its own."""
    with ReviewDB(Path(path)) as d:
        for k in range(n):
            open_rows = d.list("proposed", limit=10)
            if open_rows:
                d.decide([open_rows[0]["item_uid"]], "approve" if k % 2 else "reject")
            d.user_add([(uid(100_000 + k), "user added item", None)])
            d.mark_deleted([uid(100_000 + k)], True)
            d.counts()


def test_concurrent_processes(tmp_path):
    path = tmp_path / "review.sqlite"
    ReviewDB(path).close()          # create schema up front
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_claude_worker, args=(str(path), 0, 60)),
             ctx.Process(target=_claude_worker, args=(str(path), 1000, 60)),
             ctx.Process(target=_user_worker, args=(str(path), 60))]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert [p.exitcode for p in procs] == [0, 0, 0]
    with ReviewDB(path) as d:
        c = d.counts()
        # 120 proposals minus 40 withdrawn (k % 3 == 0) -- though a proposal decided before
        # its withdrawal survives -- plus 60 user rows.
        total = c["proposed"] + c["approved"] + c["rejected"]
        assert 80 + 60 <= total <= 120 + 60
        assert c["deleted"] == 60
        assert d.rev() > 0


def _fresh_open_worker(path: str, barrier) -> None:
    """Child process: wait for every sibling, then open (and so create) the same new DB."""
    barrier.wait(60)
    with ReviewDB(Path(path)) as d:
        d.counts()


def test_concurrent_first_open_of_fresh_db(tmp_path):
    # The site and the MCP server may create review.sqlite at the same moment on a new home.
    ctx = multiprocessing.get_context("spawn")
    n = 6
    for round_ in range(3):
        path = tmp_path / f"fresh{round_}" / "review.sqlite"
        barrier = ctx.Barrier(n)
        procs = [ctx.Process(target=_fresh_open_worker, args=(str(path), barrier))
                 for _ in range(n)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
        assert [p.exitcode for p in procs] == [0] * n
        with ReviewDB(path) as d:
            assert d.rev() == 0


def test_open_retries_while_locked(tmp_path, monkeypatch):
    real = rdb.open_review
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(path)

    monkeypatch.setattr(rdb, "open_review", flaky)
    monkeypatch.setattr(rdb.time, "sleep", lambda s: None)
    with ReviewDB(tmp_path / "review.sqlite") as d:
        assert d.rev() == 0
    assert calls["n"] == 3


def test_open_does_not_retry_other_errors(tmp_path, monkeypatch):
    calls = {"n": 0}

    def broken(path):
        calls["n"] += 1
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(rdb, "open_review", broken)
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        ReviewDB(tmp_path / "review.sqlite")
    assert calls["n"] == 1


def test_write_error_propagates_when_sqlite_already_rolled_back(db, monkeypatch):
    # Simulate SQLITE_FULL: SQLite ends the transaction itself, then the error surfaces.
    def fn(conn, now):
        conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        db._write(fn)
    assert not db._conn.in_transaction
    assert db.propose(items([1]), proposer=CLAUDE, batch_id="b1")["added"] == 1


# ---------------------------------------------------------------- write guards

def status_in(conn, n):
    return conn.execute("SELECT status FROM queue WHERE item_uid = ?", (uid(n),)).fetchone()[0]


@pytest.mark.parametrize("method", ["user_add", "decide"])
def test_guard_runs_inside_the_write_transaction(db, method):
    db.propose(items([1]), proposer=CLAUDE, batch_id="b1")
    seen = []

    def guard(conn):
        # BEGIN IMMEDIATE is open and nothing has been written yet.
        seen.append((conn.in_transaction, status_in(conn, 1)))

    if method == "user_add":
        assert db.user_add(items([1]), guard=guard) == 1
    else:
        assert db.decide([uid(1)], "approve", guard=guard) == 1
    assert seen == [(True, "proposed")] and status(db, 1) == "approved"


class Refused(Exception):
    pass


@pytest.mark.parametrize("method", ["user_add", "decide"])
def test_guard_exception_rolls_back_and_propagates(db, method):
    db.propose(items([1, 2]), proposer=CLAUDE, batch_id="b1")
    rev, n_events = db.rev(), len(db.events(limit=1000))

    def guard(conn):
        raise Refused("no")

    with pytest.raises(Refused):
        if method == "user_add":
            db.user_add(items([1, 3]), guard=guard)
        else:
            db.decide([uid(1), uid(2)], "approve", guard=guard)
    assert db.rev() == rev and len(db.events(limit=1000)) == n_events
    assert status(db, 1) == status(db, 2) == "proposed" and db.get([uid(3)]) == {}
    # The connection is usable again (the transaction was really closed).
    assert db.user_add(items([3])) == 1


def test_guard_must_be_callable(db):
    with pytest.raises(TypeError):
        db.user_add(items([1]), guard="not a function")
    assert db.get([uid(1)]) == {}


def test_approved_among(db):
    db.user_add(items([1, 2]))
    db.propose(items([3]), proposer=CLAUDE, batch_id="b1")
    db.mark_deleted([uid(2)], True)
    want = {uid(1), uid(2)}
    assert db.approved_among([uid(n) for n in (1, 2, 3, 4)]) == want
    got = []
    db.decide([uid(3)], "reject", guard=lambda conn: got.append(
        db.approved_among([uid(n) for n in (1, 2, 3)], conn)))
    assert got == [want]
    with pytest.raises(ValueError):
        db.approved_among("g:not-a-list")


def _guarded_worker(path: str, n: int, barrier, results) -> None:
    """Child process: approve uid(n) unless its partner (1 <-> 2) is already approved,
    checking inside the transaction with a slow guard."""
    with ReviewDB(Path(path)) as d:
        def guard(conn):
            time.sleep(0.3)          # the sibling is surely waiting for the write lock now
            if d.approved_among([uid(1), uid(2)], conn):
                raise Refused("partner already approved")

        barrier.wait(60)
        try:
            d.user_add(items([n]), guard=guard)
            results.put((n, "ok"))
        except Refused:
            results.put((n, "refused"))


def test_guard_serialises_two_processes(tmp_path):
    path = tmp_path / "review.sqlite"
    ReviewDB(path).close()
    ctx = multiprocessing.get_context("spawn")
    for _round in range(2):
        barrier, results = ctx.Barrier(2), ctx.Queue()
        procs = [ctx.Process(target=_guarded_worker, args=(str(path), n, barrier, results))
                 for n in (1, 2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
        assert [p.exitcode for p in procs] == [0, 0]
        got = dict(results.get(timeout=10) for _ in procs)
        assert sorted(got.values()) == ["ok", "refused"], got
        with ReviewDB(path) as d:
            assert len(d.approved_among([uid(1), uid(2)])) == 1
            d.decide([uid(1), uid(2)], "reset")      # user rows are removed: next round
