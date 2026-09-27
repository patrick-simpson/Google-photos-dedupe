"""Tests for gpclean.site.server: request hardening, the JSON API, queue safety rules, CSV
export, thumbnails, deletion-mode day badges and the static page rules."""

from __future__ import annotations

import csv
import http.client
import io
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from bundle_factory import StubTextEmbedder, make_bundle, plan_items
from gpclean.review_db import ReviewDB
from gpclean.site import server as site
from gpclean.site.server import CSP, csv_cell, make_server
from gpclean.version import TRASH_DAYS

STATIC = Path(site.__file__).parent / "static"
N = 60
PLAN = plan_items(N)
ITEM = {it["item_id"]: it for it in PLAN.items}
RLO = chr(0x202E)     # right-to-left override (a bidi "Trojan Source" character)


# ---------------------------------------------------------------------------- harness

class Client:
    """Minimal HTTP client that talks to one running test server."""

    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1]
        self.token = server.app.csrf_token
        self.host = f"127.0.0.1:{self.port}"

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None, host: str | None = "default"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                conn.putheader("Host", self.host if host == "default" else host)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def get_json(self, path: str, status: int = 200):
        code, _, body = self.get(path)
        assert code == status, (code, body[:300])
        return json.loads(body)

    def post(self, path: str, obj=None, *, raw: bytes | None = None, headers: dict | None = None,
             drop: tuple[str, ...] = ()):
        h = {"Content-Type": "application/json", "Origin": f"http://{self.host}",
             "X-CSRF-Token": self.token}
        h.update(headers or {})
        for k in drop:
            h.pop(k, None)
        body = raw if raw is not None else json.dumps(obj).encode()
        code, _, data = self.request("POST", path, body=body, headers=h)
        return code, json.loads(data) if data else None


def _write_home(tmp_path: Path, bundle: Path, **extra) -> Path:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "config.json").write_text(json.dumps({"bundle": str(bundle), **extra}),
                                                 encoding="utf-8")
    return home


@pytest.fixture
def start(tmp_path):
    """start(edit_sql=[...], config={...}, embedder=..., home=...) -> Client (auto-stopped)."""
    servers = []

    def _start(*, edit_sql=(), config=None, embedder="stub", home=None, bundle_kw=None):
        if home is None:
            bundle = make_bundle(tmp_path, n_items=N, **(bundle_kw or {}))
            if edit_sql:
                conn = sqlite3.connect(bundle / "index.sqlite")
                for sql in edit_sql:
                    conn.execute(sql)
                conn.commit()
                conn.close()
            home = _write_home(tmp_path, bundle, **(config or {}))
        srv = make_server(home, 0, text_embedder=StubTextEmbedder() if embedder == "stub"
                          else embedder)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        servers.append(srv)
        client = Client(srv)
        client.home = home
        return client

    yield _start
    for srv in servers:
        stop(srv)


def stop(srv) -> None:
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def c(start):
    return start()


def review_of(client) -> ReviewDB:
    return ReviewDB(client.home / "state" / "review.sqlite")


def queue_row(client, uid: str) -> dict | None:
    with review_of(client) as r:
        return r.get([uid]).get(uid)


def uid(i: int) -> str:
    return ITEM[i]["item_uid"]


def approved_days(client) -> dict:
    data = client.get_json("/api/queue?status=approved")
    return {d["date"]: d for d in data["days"]}


# ---------------------------------------------------------------------------- binding / hosts

def test_binds_to_loopback_only(c):
    assert c.server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:{port}",
                                  "127.0.0.1:1", "127.0.0.1", "0.0.0.0:{port}",
                                  "[::1]:{port}", "localhost.evil.example.com:{port}"])
def test_bad_host_is_421(c, host):
    code, headers, body = c.get("/api/stats", host=host.format(port=c.port))
    assert code == 421
    assert headers["content-security-policy"] == CSP
    assert json.loads(body) == {"error": "misdirected_request"}


def test_missing_host_is_421(c):
    assert c.get("/", host=None)[0] == 421


def test_localhost_host_allowed(c):
    assert c.get("/api/stats", host=f"localhost:{c.port}")[0] == 200
    assert c.get("/api/stats", host=f"LOCALHOST:{c.port}")[0] == 200


@pytest.mark.parametrize("value,ok", [("same-origin", True), ("none", True),
                                      ("cross-site", False), ("same-site", False)])
def test_sec_fetch_site(c, value, ok):
    for path in ("/api/stats", "/thumb/3/g", "/"):
        code = c.get(path, headers={"Sec-Fetch-Site": value})[0]
        assert (code == 200) is ok, (path, value, code)
    code, _ = c.post("/api/queue/add", {"ids": [7]}, headers={"Sec-Fetch-Site": value})
    assert (code == 200) is ok


# ---------------------------------------------------------------------------- CSRF / POST rules

def test_post_rejections_do_not_mutate(c):
    rev = c.get_json("/api/rev")["rev"]
    good = {"ids": [7], "reason": "junk: screenshot"}
    cases = [
        (dict(drop=("X-CSRF-Token",)), 403),
        (dict(headers={"X-CSRF-Token": "x" * 43}), 403),
        (dict(headers={"X-CSRF-Token": c.token[:-1]}), 403),
        (dict(drop=("Origin",)), 403),
        (dict(headers={"Origin": "http://evil.example.com"}), 403),
        (dict(headers={"Origin": "null"}), 403),
        (dict(headers={"Origin": f"http://localhost:{c.port}"}), 403),   # Host is 127.0.0.1
        (dict(headers={"Origin": f"https://127.0.0.1:{c.port}"}), 403),
        (dict(headers={"Content-Type": "text/plain"}), 415),
        (dict(headers={"Content-Type": "application/x-www-form-urlencoded"}), 415),
        (dict(drop=("Content-Type",)), 415),
    ]
    for kw, want in cases:
        code, data = c.post("/api/queue/add", good, **kw)
        assert code == want, (kw, code, data)
        assert set(data) == {"error"}
    for raw in (b"{not json", b"[1, 2]", b'"x"', b"\xff\xfe", b'{"ids": NaN}', b""):
        code, data = c.post("/api/queue/add", raw=raw)
        assert code == 400, raw
    assert c.get_json("/api/rev")["rev"] == rev
    assert queue_row(c, uid(7)) is None


def test_post_body_limit(c):
    big = b'{"ids": [7], "reason": "' + b"x" * (site.MAX_BODY + 10) + b'"}'
    code, data = c.post("/api/queue/add", raw=big)
    assert code == 413 and data == {"error": "too_large"}


def test_get_never_mutates_and_post_routes_need_post(c):
    code, _, _ = c.get("/api/queue/add?ids=7")
    assert code == 404
    assert queue_row(c, uid(7)) is None


def test_unknown_routes_and_methods(c):
    for path in ("/nope", "/../../etc/passwd", "/static/app.js", "/api/stats/", "/thumb/"):
        code, headers, body = c.get(path)
        assert code == 404, path
        assert json.loads(body) == {"error": "not_found"}
    code, _ = c.post("/api/nope", {})
    assert code == 404
    code, headers, body = c.request("PUT", "/api/queue/add", body=b"{}")
    assert code == 501
    assert headers["x-content-type-options"] == "nosniff"
    assert b"PUT" not in body          # stdlib error pages echo the method; ours do not


def test_errors_never_reflect_input(c):
    evil = quote("<script>alert(1)</script>")
    for path in (f"/api/junk?category={evil}", f"/api/search?date_from={evil}",
                 f"/api/queue?status={evil}", f"/api/item?id={evil}", f"/{evil}"):
        code, _, body = c.get(path)
        assert code in (400, 404), path
        assert b"script" not in body and b"alert" not in body
    code, data = c.post("/api/queue/decide", {"uids": ["<b>x</b>"], "decision": "<i>"})
    assert code == 400 and "<i>" not in json.dumps(data)


# ---------------------------------------------------------------------------- headers

def test_security_headers_everywhere(c):
    responses = [c.get("/"), c.get("/app.js"), c.get("/style.css"), c.get("/api/stats"),
                 c.get("/thumb/3/g"), c.get("/thumb/999999/g"), c.get("/api/export.csv"),
                 c.get("/api/stats", host="evil.example.com"),
                 c.get("/api/stats", headers={"Sec-Fetch-Site": "cross-site"}),
                 c.get("/api/junk?limit=0")]
    for _code, headers, _ in responses:
        assert headers["content-security-policy"] == CSP
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cross-origin-resource-policy"] == "same-origin"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert not any(k.startswith("access-control-") for k in headers), headers
        assert "python" not in headers.get("server", "").lower()
    assert "frame-ancestors 'none'" in CSP and "script-src 'self'" in CSP
    assert c.get("/api/stats")[1]["cache-control"] == "no-store"
    assert c.get("/")[1]["content-type"].startswith("text/html")
    assert c.get("/app.js")[1]["content-type"].startswith("text/javascript")


# ---------------------------------------------------------------------------- static rules

def test_index_has_csrf_meta_and_no_inline_code(c):
    code, _, body = c.get("/")
    html = body.decode("utf-8")
    assert code == 200
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m and m.group(1) == c.token and len(c.token) >= 40
    assert site.CSRF_PLACEHOLDER not in html
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I)
    assert scripts and all('src="/app.js"' in attrs and not inner.strip()
                           for attrs, inner in scripts)
    assert not re.search(r"<style", html, re.I)
    assert not re.search(r"\sstyle=", html, re.I)
    assert not re.search(r"\son[a-z]+=", html, re.I)
    assert not re.search(r"(src|href)=\"(https?:)?//", html, re.I)


def test_token_changes_per_launch(start, tmp_path):
    a = start()
    b = start(home=a.home)
    assert a.token != b.token
    code, _ = a.post("/api/queue/add", {"ids": [7]}, headers={"X-CSRF-Token": b.token})
    assert code == 403


def test_static_js_has_no_dangerous_sinks():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                "new Function", "Function(", "setTimeout(\"", "javascript:", "http://",
                "srcdoc", "createContextualFragment", "DOMParser"):
        assert bad not in js, bad
    assert 'window.open(url, name || "gphotos")' in js
    assert "textContent" in js and "setAttribute" in js
    # Only ASCII in the source: invisible characters are written as escapes.
    assert js.isascii()
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css
    assert "prefers-color-scheme: dark" in css


def test_static_files_are_a_fixed_map():
    assert set(site.STATIC_FILES) == {"/", "/index.html", "/app.js", "/style.css"}
    assert all("/" not in name for name, _ in site.STATIC_FILES.values())


# ---------------------------------------------------------------------------- read API

def test_stats_and_rev(c):
    s = c.get_json("/api/stats")
    assert s["bundle"]["items"] == N
    assert s["trash_days"] == TRASH_DAYS == 30
    assert s["text_search"] == "ready"
    assert s["queue"]["approved"] == 0
    assert s["port"] == c.port
    assert "screenshot" in s["junk_categories"]
    r = c.get_json("/api/rev")
    assert r["rev"] == s["rev"] and r["counts"]["proposed"] == 0


def test_dups(c):
    d = c.get_json("/api/dups")
    assert d["total"] == 2
    g1, g2 = d["groups"]
    assert [m["id"] for m in g1["members"]] == [2, 3] and g1["keeper_id"] == 2
    assert g1["members"][0]["is_keeper"] == 1 and g1["deletable"] is True
    assert g1["group_key"] and g1["kind"] == "exact"
    assert "filename" in g1["members"][1]["diff"] and g1["members"][0]["diff"] == []
    assert len(g2["members"]) == 3
    near = c.get_json("/api/dups?kind=near")
    assert [g["group_id"] for g in near["groups"]] == [2]
    assert c.get("/api/dups?kind=bogus")[0] == 400
    page = c.get_json("/api/dups?limit=1")
    assert len(page["groups"]) == 1 and page["next_offset"] == 1


def test_junk(c):
    d = c.get_json("/api/junk?category=screenshot")
    shots = [i for i in ITEM if i % 7 == 0]
    assert d["total"] == len(shots) and d["counts"]["screenshot"] == len(shots)
    assert all(r["score"] == 0.95 for r in d["rows"])
    assert d["counts"]["messaging"] == len([i for i in ITEM if i % 9 == 0 and i % 7])
    blur_hi = c.get_json("/api/junk?category=blur&min_score=0.8")
    assert {r["id"] for r in blur_hi["rows"]} == {i for i in ITEM if i % 5 == 0 and i % 3 == 2}
    y = c.get_json("/api/junk?category=screenshot&year=2019")
    assert all(r["year"] == 2019 for r in y["rows"]) and y["total"] < d["total"]
    assert c.get("/api/junk?category=screenshot&min_score=2")[0] == 400
    assert c.get("/api/junk?category=screenshot&min_score=nan")[0] == 400


def test_search_filters_and_text(c):
    d = c.get_json("/api/search?filename=wa")
    assert d["total"] and all("WA" in r["filename"] for r in d["rows"])
    t = c.get_json("/api/search?q=receipt&limit=5")
    assert t["rows"] and all(PLAN.concept_of.get(r["id"]) == "receipt" for r in t["rows"])
    assert all(r["sim"] >= 0.18 for r in t["rows"])
    dated = c.get_json("/api/search?date_from=2020-01-01&date_to=2020-12-31&limit=200")
    assert dated["total"] and all(r["local_date"].startswith("2020") for r in dated["rows"])
    assert c.get("/api/search?date_from=2020-02-30")[0] == 400
    assert c.get("/api/search?limit=100000")[0] == 400


def test_search_without_text_model(start):
    cl = start(embedder=None)
    assert cl.get_json("/api/stats")["text_search"] == "not_loaded"
    code, _, body = cl.get("/api/search?q=receipt")
    assert code == 503 and json.loads(body)["error"] == "text_search_unavailable"
    assert cl.get_json("/api/search?filename=IMG")["total"] > 0


def test_text_loader_failure_is_contained(start, monkeypatch):
    from gpclean import clipmodel

    def boom(name):
        raise RuntimeError("no weights")

    monkeypatch.setattr(clipmodel, "TextEmbedder", boom)
    cl = start(embedder=None)
    cl.server.app.start_text_loader()
    deadline = time.time() + 10
    while cl.server.app.text_status == "loading" and time.time() < deadline:
        time.sleep(0.02)
    assert cl.get_json("/api/rev")["text_search"] == "unavailable"
    assert cl.get_json("/api/junk?category=blur")["total"] > 0


def test_text_loader_success_uses_bundle_model(start, monkeypatch):
    from gpclean import clipmodel

    seen = []

    class Fake(StubTextEmbedder):
        def __init__(self, name):
            seen.append(name)
            super().__init__()

    monkeypatch.setattr(clipmodel, "TextEmbedder", Fake)
    cl = start(embedder=None)
    cl.server.app.start_text_loader()
    deadline = time.time() + 10
    while cl.server.app.text_status != "ready" and time.time() < deadline:
        time.sleep(0.02)
    assert seen == ["b32"]
    assert cl.get_json("/api/search?q=receipt")["total"] > 0


def test_no_embeddings_bundle(start):
    cl = start(embedder=None, bundle_kw={"with_embeddings": False})
    assert cl.get_json("/api/stats")["text_search"] == "none"
    cl.server.app.start_text_loader()          # nothing to load: stays "none"
    assert cl.server.app.text_status == "none"


def test_item(c):
    it = c.get_json("/api/item?id=3")
    assert it["uid"] == uid(3) and it["dup"]["group_id"] == 1
    assert it["dup"]["member_ids"] == [2, 3]
    assert c.get_json("/api/item?id=21")["burst_info"]["best_item_id"] == 21
    assert c.get("/api/item?id=9999")[0] == 404
    assert c.get("/api/item?id=abc")[0] == 400
    assert c.get("/api/item")[0] == 404


# ---------------------------------------------------------------------------- links

def test_photos_links(start):
    evil_url = "javascript:alert(1)"
    cl = start(edit_sql=[f"UPDATE items SET url = '{evil_url}' WHERE item_id = 4",
                         "UPDATE items SET match_conf = 'low' WHERE item_id = 5",
                         f"UPDATE items SET filename = 'IMG{RLO}_x (1).jpg' WHERE item_id = 22"])
    it = cl.get_json("/api/item?id=3")
    assert it["photos_url"] == "https://photos.google.com/photo/" + uid(3)[2:]
    assert it["link_conf"] == "high"
    it = cl.get_json("/api/item?id=11")                        # no url -> filename search
    stem = ITEM[11]["filename"].rsplit(".", 1)[0]
    assert it["photos_url"] == "https://photos.google.com/search/" + quote(stem, safe="")
    assert it["link_conf"] == "low"
    it = cl.get_json("/api/item?id=4")                          # invalid url never linked
    assert it["photos_url"].startswith("https://photos.google.com/search/")
    assert "javascript" not in json.dumps(it)
    assert cl.get_json("/api/item?id=5")["link_conf"] == "low"   # low-confidence pairing
    it = cl.get_json("/api/item?id=22")
    assert RLO not in it["filename"] and RLO not in it["photos_url"]
    assert it["filename"] == "IMG_x (1).jpg"


def test_account_index_rewrite(start):
    cl = start(config={"photos_account_index": 1})
    assert cl.get_json("/api/item?id=3")["photos_url"] == \
        "https://photos.google.com/u/1/photo/" + uid(3)[2:]
    assert cl.get_json("/api/item?id=11")["photos_url"].startswith(
        "https://photos.google.com/u/1/search/")


def test_invalid_account_index_ignored(start):
    cl = start(config={"photos_account_index": "1/../evil"})
    assert cl.get_json("/api/item?id=3")["photos_url"].startswith(
        "https://photos.google.com/photo/")


# ---------------------------------------------------------------------------- queue flows

def test_user_add_is_approved_by_user(c):
    code, data = c.post("/api/queue/add", {"ids": [7, 14], "reason": "junk: screenshot",
                                           "category": "screenshot"})
    assert code == 200 and data["changed"] == 2 and data["counts"]["approved"] == 2
    row = queue_row(c, uid(7))
    assert row["status"] == "approved" and row["proposed_by"] == "user"
    assert row["category"] == "screenshot"
    q = c.get_json("/api/queue?status=approved")
    assert q["total"] == 2
    items = [it for d in q["days"] for it in d["items"]]
    assert {it["id"] for it in items} == {7, 14}
    assert all(it["queue"]["status"] == "approved" for it in items)


def test_add_validation(c):
    assert c.post("/api/queue/add", {"ids": []})[0] == 400
    assert c.post("/api/queue/add", {"ids": ["7"]})[0] == 400
    assert c.post("/api/queue/add", {"ids": [True]})[0] == 400
    assert c.post("/api/queue/add", {"ids": [9999]})[0] == 404
    assert c.post("/api/queue/add", {"ids": [7], "reason": "x"})[0] == 400
    assert c.post("/api/queue/add", {"ids": [7], "category": "DROP TABLE"})[0] == 400
    assert c.post("/api/queue/add", {"ids": [7], "override": "yes"})[0] == 400


def test_protected_items_need_override(c):
    for item_id in (13, 17):                      # shared album / favorited
        code, data = c.post("/api/queue/add", {"ids": [7, item_id], "reason": "junk pick"})
        assert code == 409 and data == {"error": "needs_override", "ids": [item_id]}
        assert queue_row(c, uid(7)) is None       # nothing applied
        code, data = c.post("/api/queue/add", {"ids": [item_id], "reason": "junk pick",
                                               "override": True})
        assert code == 200 and data["changed"] == 1
    it = c.get_json("/api/item?id=13")
    assert it["shared"] and it["protected"]


def test_partner_items_need_override(start):
    cl = start(edit_sql=["UPDATE items SET partner = 1 WHERE item_id = 8"])
    assert cl.post("/api/queue/add", {"ids": [8], "reason": "junk pick"})[0] == 409


def test_every_member_of_group_refused(c):
    code, data = c.post("/api/queue/add", {"ids": [2, 3], "reason": "both copies"})
    assert code == 409 and data["error"] == "whole_group" and data["group_ids"] == [1]
    assert c.post("/api/queue/add", {"ids": [3], "reason": "one copy"})[0] == 200
    for override in (False, True):
        code, data = c.post("/api/queue/add", {"ids": [2], "reason": "last copy",
                                               "override": override})
        assert code == 409 and data["error"] == "whole_group"
    assert queue_row(c, uid(2)) is None


def test_claude_proposals_decide_flow(c):
    with review_of(c) as r:
        r.propose([(uid(i), "looks like junk", None) for i in (7, 13, 2, 3, 14)],
                  proposer="claude:code", batch_id="b1")
        r.propose([(uid(28), "blurry", None), (uid(35), "blurry", None)],
                  proposer="claude:code", batch_id="b2")
    q = c.get_json("/api/queue?status=proposed")
    assert q["total"] == 7 and {b["batch_id"] for b in q["batches"]} == {"b1", "b2"}
    row = next(it for it in q["rows"] if it["id"] == 7)
    assert row["queue"]["proposed_by"] == "claude:code"
    assert row["queue"]["reason"] == "looks like junk"

    code, data = c.post("/api/queue/decide", {"uids": [uid(7)], "decision": "approve"})
    assert code == 200 and data["changed"] == 1
    # A shared item needs the override on approval too.
    code, data = c.post("/api/queue/decide", {"uids": [uid(13)], "decision": "approve"})
    assert code == 409 and data["error"] == "needs_override"
    assert c.post("/api/queue/decide", {"uids": [uid(13)], "decision": "approve",
                                        "override": True})[0] == 200
    # Both members of a dup group: refused as a pair, and the last one alone.
    code, data = c.post("/api/queue/decide", {"uids": [uid(2), uid(3)], "decision": "approve"})
    assert code == 409 and data["error"] == "whole_group"
    assert c.post("/api/queue/decide", {"uids": [uid(2)], "decision": "approve"})[0] == 200
    assert c.post("/api/queue/decide", {"uids": [uid(3)], "decision": "approve"})[0] == 409
    assert c.post("/api/queue/decide", {"uids": [uid(3)], "decision": "reject"})[0] == 200
    assert queue_row(c, uid(3))["status"] == "rejected"
    # reset: Claude's row goes back to proposed.
    assert c.post("/api/queue/decide", {"uids": [uid(3)], "decision": "reset"})[0] == 200
    assert queue_row(c, uid(3))["status"] == "proposed"
    # Whole batch rejection only touches that batch's open proposals.
    code, data = c.post("/api/queue/reject_batch", {"batch_id": "b2"})
    assert code == 200 and data["changed"] == 2
    assert queue_row(c, uid(28))["status"] == "rejected"
    assert queue_row(c, uid(14))["status"] == "proposed"
    rejected = c.get_json("/api/queue?status=rejected")
    assert {it["id"] for it in rejected["rows"]} == {28, 35}
    assert c.post("/api/queue/decide", {"uids": [uid(7)], "decision": "delete"})[0] == 400
    assert c.post("/api/queue/reject_batch", {"batch_id": "bad batch!"})[0] == 400
    assert c.post("/api/queue/decide", {"uids": ["g:unknown"], "decision": "approve"})[0] == 404


def test_user_reset_removes_own_row(c):
    c.post("/api/queue/add", {"ids": [7], "reason": "junk pick"})
    assert c.post("/api/queue/decide", {"uids": [uid(7)], "decision": "reset"})[0] == 200
    assert queue_row(c, uid(7)) is None


def test_deleted_persists_across_restarts(start):
    a = start()
    a.post("/api/queue/add", {"ids": [7, 14], "reason": "junk pick"})
    code, data = a.post("/api/queue/deleted", {"uids": [uid(7)], "deleted": True})
    assert code == 200 and data["changed"] == 1 and data["counts"]["deleted"] == 1
    assert a.post("/api/queue/deleted", {"uids": [uid(7)], "deleted": "yes"})[0] == 400
    stop(a.server)
    b = start(home=a.home)
    q = b.get_json("/api/queue?status=approved")
    items = {it["id"]: it for d in q["days"] for it in d["items"]}
    assert items[7]["queue"]["deleted"] is True and items[14]["queue"]["deleted"] is False
    assert q["n_deleted"] == 1
    until = items[7]["queue"]["recoverable_until"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", until)
    # Deleted rows cannot be reset by a stray keypress (review_db skips them).
    assert b.post("/api/queue/decide", {"uids": [uid(7)], "decision": "reset"})[1]["changed"] == 0
    assert b.post("/api/queue/deleted", {"uids": [uid(7)], "deleted": False})[0] == 200
    assert queue_row(b, uid(7))["deleted"] == 0


def test_queue_param_validation(c):
    assert c.get("/api/queue?status=bogus")[0] == 400
    assert c.get("/api/queue?limit=0")[0] == 400
    assert c.get_json("/api/queue?status=all")["total"] == 0


def test_approved_sorted_by_local_day(c):
    ids = [30, 8, 45, 12, 60]                      # 60 is the undated item
    c.post("/api/queue/add", {"ids": ids, "reason": "junk pick"})
    q = c.get_json("/api/queue?status=approved")
    order = [it["id"] for d in q["days"] for it in d["items"]]
    key = [(ITEM[i]["local_date"] is None, ITEM[i]["local_date"] or "",
            ITEM[i]["local_time"] or "") for i in order]
    assert key == sorted(key) and order[-1] == 60
    assert q["days"][-1]["date"] is None and q["days"][-1]["safe_to_day_select"] is False
    page = c.get_json("/api/queue?status=approved&limit=2")
    assert page["total"] == 5 and page["next_offset"] == 2
    assert sum(len(d["items"]) for d in page["days"]) == 2


# ---------------------------------------------------------------------------- deletion mode

def test_safe_to_day_select(start):
    d11 = ITEM[11]["local_date"]
    cl = start(edit_sql=[f"UPDATE items SET local_date = '{d11}' WHERE item_id = 12",
                         "UPDATE items SET day_uncertain = 1 WHERE item_id = 16"])
    cl.post("/api/queue/add", {"ids": [11, 23], "reason": "junk pick"})
    days = approved_days(cl)
    day = days[d11]
    assert day["n_indexed_photos_that_day"] == 2 and day["n_approved_that_day"] == 1
    assert day["safe_to_day_select"] is False            # item 12 shares the day
    assert days[ITEM[23]["local_date"]]["safe_to_day_select"] is True
    cl.post("/api/queue/add", {"ids": [12], "reason": "junk pick"})
    day = approved_days(cl)[d11]
    assert day["n_approved_that_day"] == 2 and day["safe_to_day_select"] is True
    # A day with a video is never safe (videos are not in the index).
    cl.post("/api/queue/add", {"ids": [10], "reason": "junk pick"})
    day10 = approved_days(cl)[ITEM[10]["local_date"]]
    assert day10["videos_that_day"] == 1 and day10["n_approved_that_day"] == 1
    assert day10["safe_to_day_select"] is False
    # Nor is a day holding an item whose time zone (and so day) is uncertain.
    cl.post("/api/queue/add", {"ids": [16], "reason": "junk pick"})
    day16 = approved_days(cl)[ITEM[16]["local_date"]]
    assert day16["n_day_uncertain"] == 1 and day16["safe_to_day_select"] is False


# ---------------------------------------------------------------------------- duplicates

def test_dups_keeper_and_queue(c):
    g1 = c.get_json("/api/dups")["groups"][0]
    assert c.post("/api/dups/keeper", {"group_key": g1["group_key"], "uid": uid(9)})[0] == 400
    assert c.post("/api/dups/keeper", {"group_key": "f" * 40, "uid": uid(3)})[0] == 404
    code, data = c.post("/api/dups/keeper", {"group_key": g1["group_key"], "uid": uid(3)})
    assert code == 200
    g1 = c.get_json("/api/dups")["groups"][0]
    assert g1["keeper_id"] == 3 and [m["is_keeper"] for m in g1["members"]] == [0, 1]
    code, data = c.post("/api/dups/queue", {"group_ids": [1]})
    assert code == 200 and data["changed"] == 1
    row = queue_row(c, uid(2))
    assert row["status"] == "approved" and row["proposed_by"] == "user"
    assert row["reason"] == "duplicate of " + ITEM[3]["filename"]
    assert row["category"] == "dup_extra"
    assert queue_row(c, uid(3)) is None
    # The keeper can no longer be approved: that would delete every copy.
    assert c.post("/api/queue/add", {"ids": [3], "reason": "x" * 5})[0] == 409


def test_dups_queue_refuses_when_keeper_is_approved(c):
    c.post("/api/queue/add", {"ids": [4], "reason": "junk pick"})      # group 2's keeper
    code, data = c.post("/api/dups/queue", {"group_ids": [2]})
    assert code == 409 and data["error"] == "whole_group"
    assert queue_row(c, uid(5)) is None


def test_dups_queue_not_deletable(start):
    cl = start(edit_sql=["UPDATE dup_groups SET deletable = 0 WHERE group_id = 2"])
    code, data = cl.post("/api/dups/queue", {"group_ids": [1, 2]})
    assert code == 409 and data == {"error": "not_deletable", "group_ids": [2]}
    assert queue_row(cl, uid(3)) is None          # all-or-nothing
    assert cl.get_json("/api/dups")["groups"][1]["deletable"] is False
    assert cl.post("/api/dups/queue", {"group_ids": [99]})[0] == 404
    assert cl.post("/api/dups/queue", {"group_ids": []})[0] == 400


def test_dups_queue_protected_needs_override(start):
    cl = start(edit_sql=["UPDATE items SET favorited = 1 WHERE item_id = 6"])
    code, data = cl.post("/api/dups/queue", {"group_ids": [2]})
    assert code == 409 and data == {"error": "needs_override", "ids": [6]}
    code, data = cl.post("/api/dups/queue", {"group_ids": [2], "override": True})
    assert code == 200 and data["changed"] == 2


def test_dups_dismiss(c):
    key = c.get_json("/api/dups")["groups"][0]["group_key"]
    assert c.post("/api/dups/dismiss", {"group_key": key})[0] == 200
    assert c.get_json("/api/dups")["groups"][0]["decision"] == "dismissed"
    code, data = c.post("/api/dups/queue", {"group_ids": [1]})
    assert code == 409 and data["error"] == "dismissed"
    assert c.post("/api/dups/dismiss", {"group_key": key, "undo": True})[0] == 200
    assert c.get_json("/api/dups")["groups"][0]["decision"] is None
    assert c.post("/api/dups/dismiss", {"group_key": 5})[0] == 400


# ---------------------------------------------------------------------------- CSV

def test_csv_cell():
    for bad in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", "\nx"):
        assert csv_cell(bad) == "'" + bad
    assert csv_cell("IMG_1.jpg") == "IMG_1.jpg"
    assert csv_cell(None) == "" and csv_cell(5) == 5


def test_csv_export_neutralises_formulas(start):
    cl = start(edit_sql=["UPDATE items SET filename = '=HYPERLINK(\"http://x\",\"y\").jpg'"
                         " WHERE item_id = 8"])
    evil_reasons = {7: "=cmd|' /C calc'!A0", 8: "+SUM(1)", 14: "-2+3", 21: "@SUM(A1)",
                    28: "plain, with \"quotes\" and a comma"}
    for i, reason in evil_reasons.items():
        assert cl.post("/api/queue/add", {"ids": [i], "reason": reason})[0] == 200
    code, headers, body = cl.get("/api/export.csv?status=approved")
    assert code == 200 and headers["content-type"].startswith("text/csv")
    assert "attachment" in headers["content-disposition"]
    text = body.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == list(site.CSV_COLUMNS)
    by_uid = {r["item_uid"]: r for r in rows}
    assert set(by_uid) == {uid(i) for i in evil_reasons}
    for i, reason in evil_reasons.items():
        cell = by_uid[uid(i)]["reason"]
        assert cell == ("'" + reason if reason[0] in "=+-@" else reason)
    assert by_uid[uid(8)]["filename"].startswith("'=HYPERLINK")
    r7 = by_uid[uid(7)]
    assert r7["status"] == "approved" and r7["proposed_by"] == "user" and r7["deleted"] == "0"
    assert r7["url"] == "https://photos.google.com/photo/" + uid(7)[2:]
    assert r7["width"] == "1080" and r7["size_bytes"] == str(ITEM[7]["file_size"])
    for r in rows:
        for v in r.values():
            assert not v or v[0] not in "=+-@\t\r"
    assert cl.get("/api/export.csv?status=nope")[0] == 400


# ---------------------------------------------------------------------------- thumbnails

def test_thumbnails(c):
    for size in ("g", "p"):
        code, headers, body = c.get(f"/thumb/3/{size}")
        assert code == 200 and headers["content-type"] == "image/webp"
        assert body[:4] == b"RIFF" and body[8:12] == b"WEBP"
        assert headers["cache-control"].startswith("private")
    assert c.get("/thumb/3/g?b=anything")[0] == 200
    assert c.get("/thumb/3/g")[2] != c.get("/thumb/3/p")[2]
    for path in ("/thumb/99999/g", "/thumb/0/g", "/thumb/3/x", "/thumb/abc/g",
                 "/thumb/-1/g", "/thumb/3/g/", "/thumb/..%2F..%2Findex.sqlite/g",
                 "/thumb/1e3/g", "/thumb/9999999999999/g"):
        assert c.get(path)[0] == 404, path


def test_thumbnail_missing_pack_is_404(start):
    cl = start(bundle_kw={"with_thumbs": False})
    assert cl.get("/thumb/3/g")[0] == 404


# ---------------------------------------------------------------------------- CLI

def test_cli_serve_no_browser(tmp_path, monkeypatch, capsys):
    bundle = make_bundle(tmp_path, n_items=10, with_embeddings=False)
    home = _write_home(tmp_path, bundle)
    opened = []
    monkeypatch.setattr(site.webbrowser, "open", lambda url: opened.append(url))

    def interrupted(self, poll_interval=0.5):
        raise KeyboardInterrupt

    monkeypatch.setattr(site.SiteServer, "serve_forever", interrupted)
    assert site.cli_serve(home, 0, True) == 0
    out = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:\d+/", out)
    assert opened == []
    assert (home / "state" / "logs" / "site.log").exists()


def test_cli_serve_opens_browser(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, n_items=10, with_embeddings=False)
    home = _write_home(tmp_path, bundle)
    opened = []
    monkeypatch.setattr(site.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(site.SiteServer, "serve_forever",
                        lambda self, poll_interval=0.5: (_ for _ in ()).throw(KeyboardInterrupt))
    assert site.cli_serve(home, 0, False) == 0
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")


def test_cli_serve_without_bundle(tmp_path, capsys):
    assert site.cli_serve(tmp_path / "nohome", 0, True) == 2
    assert "gpclean init" in capsys.readouterr().err


def test_queue_rows_for_items_missing_from_bundle(c):
    """Queue rows survive a new bundle; an item the bundle no longer has is still listed."""
    gone = "g:AF1QipFAKEgone000000000000000000000"
    with review_of(c) as r:
        r.user_add([(gone, "from an older bundle", None)])
    q = c.get_json("/api/queue?status=approved")
    day = q["days"][-1]
    assert day["date"] is None and day["safe_to_day_select"] is False
    it = day["items"][0]
    assert it["uid"] == gone and it["id"] is None and it["missing"] is True
    assert it["photos_url"] is None
    code, _, body = c.get("/api/export.csv?status=approved")
    assert gone in body.decode("utf-8-sig")
    # It cannot be (re)approved here: its safety flags are unknown to this bundle.
    assert c.post("/api/queue/decide", {"uids": [gone], "decision": "approve"})[0] == 404


def test_get_with_body_closes_connection(c):
    code, headers, _ = c.request("GET", "/api/rev", body=b"GET /api/stats HTTP/1.1\r\n\r\n")
    assert code == 200 and headers.get("connection") == "close"


def test_favicon_is_quiet(c):
    code, headers, body = c.get("/favicon.ico")
    assert code == 204 and body == b"" and headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------- review fixes

def test_concurrent_approvals_cannot_take_whole_group(c):
    """Two parallel requests approving complementary halves of group 1 ([2, 3]): at most one
    may win (W-13's check and write are atomic)."""
    review = c.server.app.review
    original = review.user_add

    def slow_add(items):
        time.sleep(0.05)              # widen the check-to-write window a racing request needs
        return original(items)

    review.user_add = slow_add
    for _round in range(3):
        barrier = threading.Barrier(2)
        codes: dict[int, int] = {}

        def add(item_id: int) -> None:
            barrier.wait()
            codes[item_id] = c.post("/api/queue/add", {"ids": [item_id],
                                                       "reason": "junk pick"})[0]

        threads = [threading.Thread(target=add, args=(i,)) for i in (2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert sorted(codes.values()) == [200, 409], codes
        rows = [queue_row(c, uid(i)) for i in (2, 3)]
        assert sum(1 for r in rows if r and r["status"] == "approved") == 1
        winner = next(i for i, code in codes.items() if code == 200)
        assert c.post("/api/queue/decide", {"uids": [uid(winner)], "decision": "reset"})[0] == 200


def test_possible_same_item_needs_override(start):
    cl = start(edit_sql=["UPDATE dup_groups SET deletable = 0 WHERE group_id = 2"])
    it = cl.get_json("/api/item?id=5")
    assert it["possible_same_item"] is True and it["protected"] is True
    assert cl.get_json("/api/item?id=3")["possible_same_item"] is False
    code, data = cl.post("/api/queue/add", {"ids": [5, 7], "reason": "junk pick"})
    assert code == 409 and data == {"error": "needs_override", "ids": [5]}
    assert queue_row(cl, uid(7)) is None
    assert cl.post("/api/queue/add", {"ids": [5], "reason": "junk pick",
                                      "override": True})[0] == 200
    with review_of(cl) as r:
        r.propose([(uid(6), "looks like a copy", None)], proposer="claude:code", batch_id="b1")
    code, data = cl.post("/api/queue/decide", {"uids": [uid(6)], "decision": "approve"})
    assert code == 409 and data == {"error": "needs_override", "ids": [6]}
    assert cl.post("/api/queue/decide", {"uids": [uid(6)], "decision": "approve",
                                         "override": True})[0] == 200
    # The last copy is still refused, override or not.
    code, data = cl.post("/api/queue/add", {"ids": [4], "reason": "junk pick", "override": True})
    assert code == 409 and data["error"] == "whole_group"


def test_keeper_cannot_be_an_approved_item(c):
    g1 = c.get_json("/api/dups")["groups"][0]
    assert c.post("/api/queue/add", {"ids": [3], "reason": "junk pick"})[0] == 200
    code, data = c.post("/api/dups/keeper", {"group_key": g1["group_key"], "uid": uid(3)})
    assert code == 409 and data == {"error": "keeper_approved"}
    assert c.get_json("/api/dups")["groups"][0]["keeper_id"] == 2


def test_cross_site_navigation_gets_plain_help_text(c):
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
           "Sec-Fetch-Dest": "document"}
    code, headers, body = c.get("/", headers=nav)
    assert code == 403 and headers["content-type"].startswith("text/plain")
    assert f"http://127.0.0.1:{c.port}/".encode() in body
    assert headers["content-security-policy"] == CSP
    # Non-navigation cross-site requests keep the JSON error.
    code, headers, body = c.get("/api/stats", headers={"Sec-Fetch-Site": "cross-site",
                                                       "Sec-Fetch-Mode": "cors"})
    assert code == 403 and json.loads(body) == {"error": "forbidden"}


def test_rev_and_posts_carry_batches(c):
    assert c.get_json("/api/rev")["batches"] == []
    with review_of(c) as r:
        r.propose([(uid(i), "looks like junk", None) for i in (7, 14)],
                  proposer="claude:code", batch_id="b1")
    batches = c.get_json("/api/rev")["batches"]
    assert [(b["batch_id"], b["n_open"]) for b in batches] == [("b1", 2)]
    code, data = c.post("/api/queue/decide", {"uids": [uid(7)], "decision": "approve"})
    assert code == 200 and [b["n_open"] for b in data["batches"]] == [1]


def test_queue_pages_past_review_list_limit(c, monkeypatch):
    """Every queue row is listed even when review_db.list needs several pages."""
    monkeypatch.setattr(site.SiteApp, "LIST_STEP", 2)
    ids = [30, 8, 45, 12, 60]
    c.post("/api/queue/add", {"ids": ids, "reason": "junk pick"})
    q = c.get_json("/api/queue?status=approved&limit=3")
    assert q["total"] == 5 and q["next_offset"] == 3
    rest = c.get_json("/api/queue?status=approved&offset=3&limit=3")
    got = [it["id"] for d in q["days"] + rest["days"] for it in d["items"]]
    assert sorted(got) == sorted(ids) and got[-1] == 60
    rows = list(csv.DictReader(io.StringIO(
        c.get("/api/export.csv?status=approved")[2].decode("utf-8-sig"))))
    assert len(rows) == 5


def test_static_text_matches_new_tab_behaviour():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "reused tab" not in html and "Ctrl+W" in html
    assert "close that tab (Ctrl+W)" in js
    assert "S.marking" in js and "keeper_approved" in js
