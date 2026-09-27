// gpclean review site. Vanilla JS, no dependencies, served from 127.0.0.1 only.
//
// Safety rules for this file (also checked by tests/test_site.py):
// * Every piece of text from the bundle or the queue (file names, album names, captions,
//   reasons written by Claude) is untrusted. It is rendered ONLY through textContent /
//   createTextNode / setAttribute, after clean() strips control and bidi characters.
// * No HTML string building, no dynamic code, no inline handlers; the CSP forbids them anyway.
// * Links go only to https://photos.google.com/ (the server already rebuilt them). Each one
//   opens a NEW tab: our COOP same-origin header (and Google Photos' own COOP) cut the link
//   to a named tab, so window.open(url, "gphotos") cannot reuse it. The user closes each
//   Google Photos tab (Ctrl+W) after pressing # there.
"use strict";

(function () {
  // ------------------------------------------------------------------ basics

  const TOKEN = document.querySelector('meta[name="csrf-token"]').getAttribute("content");
  // Same character classes as review_db.strip_unsafe (controls, bidi, zero-width, tags).
  const UNSAFE = /[\u0000-\u0008\u000e-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb\u{e0000}-\u{e007f}]/gu;
  const PRESETS = ["receipt", "whiteboard", "meme", "parking spot", "chat screenshot", "document"];
  const PAGE = 100;

  function clean(v) {
    if (v === null || v === undefined) return "";
    return String(v).replace(UNSAFE, "").replace(/[\t\n\v\f\r ]+/g, " ");
  }

  // Build an element. attrs: "class", "text", on<event> (listener) or any attribute
  // (set with setAttribute). Children: nodes or strings (turned into text nodes).
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "text") e.textContent = clean(v);
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      e.append(kid instanceof Node ? kid : document.createTextNode(clean(kid)));
    }
    return e;
  }

  function $(id) { return document.getElementById(id); }

  function fmtSize(n) {
    if (!Number.isFinite(n)) return "";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(n / 1024)) + " KB";
  }

  function isoDate(d) {
    const pad = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function addDays(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return isoDate(d);
  }

  function safeHref(u) {
    return typeof u === "string" && /^https:\/\/photos\.google\.com\/[A-Za-z0-9_.%\/-]*$/.test(u) ? u : null;
  }

  // ------------------------------------------------------------------ state

  const S = {
    tab: "dups", sub: "proposed", stats: null, rev: -1, counts: null, textSearch: "none",
    cards: [], focus: -1, sel: new Set(), lastSel: -1, pinned: false, busy: false,
    popupsBlocked: false, openN: 5, bundleTag: "", batches: [], marking: false,
    dups: { kind: "", offset: 0 },
    junk: { category: "screenshot", min: 0.5, year: "", offset: 0 },
    search: { q: "", category: "", date_from: "", date_to: "", year: "", filename: "", offset: 0, ran: false },
    queue: { offset: 0 },
  };

  // Claude proposals already seen, as {batch_id: n_open when last seen}. A plain count cannot
  // tell "approved 5, then Claude proposed 5 more" from "nothing changed".
  const SEEN_KEY = "gpclean.seenBatches";
  function loadSeen() {
    try {
      const v = JSON.parse(localStorage.getItem(SEEN_KEY) || "{}");
      return v && typeof v === "object" && !Array.isArray(v) ? v : {};
    } catch (e) { return {}; }
  }
  function saveSeen() {
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(S.seen)); } catch (e) { /* private mode */ }
  }
  S.seen = loadSeen();

  // Batches only shrink by the user's (or Claude's withdraw) actions, so lowering each seen
  // count to the current n_open (and dropping finished batches) keeps later additions "new".
  function trimSeen() {
    const cur = {};
    for (const b of S.batches) cur[b.batch_id] = b.n_open;
    const next = {};
    for (const [id, n] of Object.entries(S.seen)) {
      if (id in cur) next[id] = Math.min(Number(n) || 0, cur[id]);
    }
    if (JSON.stringify(next) !== JSON.stringify(S.seen)) { S.seen = next; saveSeen(); }
  }

  function markAllSeen() {
    S.seen = {};
    for (const b of S.batches) S.seen[b.batch_id] = b.n_open;
    saveSeen();
  }

  function newProposals() {
    let n = 0;
    for (const b of S.batches) n += Math.max(0, b.n_open - (Number(S.seen[b.batch_id]) || 0));
    return n;
  }

  // ------------------------------------------------------------------ server calls

  async function getJSON(path, params) {
    const url = new URL(path, location.origin);
    for (const [k, v] of Object.entries(params || {})) {
      if (v !== "" && v !== null && v !== undefined) url.searchParams.set(k, String(v));
    }
    const r = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    const data = await r.json().catch(() => ({ error: "bad_response" }));
    return { ok: r.ok, status: r.status, data };
  }

  async function postJSON(path, body) {
    const r = await fetch(path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": TOKEN },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({ error: "bad_response" }));
    return { ok: r.ok, status: r.status, data };
  }

  const ERRORS = {
    whole_group: "Refused: that would delete every copy in a duplicate group. Keep at least one copy.",
    needs_override: "Shared, partner-shared or favorited items, and possible same-item copies, need an explicit override.",
    keeper_approved: "That copy is queued for deletion; undo its approval first.",
    not_deletable: "Refused: these copies are the same Google Photos item (or have no link), so deleting one would delete them all.",
    dismissed: "This group is marked 'not duplicates'. Undo that first.",
    text_search_unavailable: "Text search is not available (no CLIP text model). The other filters still work.",
    unknown_item: "Some items are not in the current bundle.",
    bad_reason_or_category: "The reason must be 3 to 300 characters.",
    bad_csrf_token: "The page is out of date (the server was restarted). Reload the page.",
  };

  function errText(d) {
    const code = d && d.error;
    return ERRORS[code] || ("Request failed: " + clean(code || "unknown error"));
  }

  // POST a change; on "needs_override" ask once, then resend with override: true.
  async function mutate(path, body) {
    let res = await postJSON(path, body);
    if (res.status === 409 && res.data.error === "needs_override") {
      const n = (res.data.ids || []).length;
      const ok = window.confirm(n + " of these items are shared, partner-shared or favorited, or " +
        "may be the same Google Photos item as its duplicate. Deleting them in Google Photos can " +
        "remove them for other people, or delete the only real copy. Queue them anyway?");
      if (!ok) return null;
      res = await postJSON(path, Object.assign({}, body, { override: true }));
    }
    if (!res.ok) {
      toast(errText(res.data), true);
      return null;
    }
    applyState(res.data);
    return res.data;
  }

  // ------------------------------------------------------------------ header, toast, banner

  function applyState(d) {
    if (!d) return;
    if (typeof d.rev === "number") S.rev = d.rev;
    if (d.counts) S.counts = d.counts;
    if (d.text_search) S.textSearch = d.text_search;
    if (Array.isArray(d.batches)) { S.batches = d.batches; trimSeen(); }
    renderSummary();
  }

  function renderSummary() {
    const box = $("summary");
    const b = S.stats ? S.stats.bundle : null;
    const c = S.counts || {};
    const parts = [];
    if (b) parts.push(b.items + " photos", b.dup_groups.total + " duplicate groups");
    // Library items the index has no row for (videos and skipped media such as raw files).
    const ni = S.stats ? S.stats.not_indexed : null;
    const nNotIndexed = ni ? (ni.on_a_day || 0) + (ni.undated || 0) : 0;
    if (nNotIndexed) parts.push(nNotIndexed + " not in the index (videos, skipped files)");
    parts.push((c.proposed || 0) + " proposed", (c.approved || 0) + " approved",
      (c.deleted || 0) + " deleted");
    if (b && b.partial) parts.push("PARTIAL bundle (" + b.missing_shards + " shards missing)");
    box.replaceChildren(document.createTextNode(clean(parts.join(" \u00b7 "))));
    const badge = $("proposal-badge");
    const open = c.proposed || 0;
    badge.hidden = open === 0;
    badge.textContent = String(open);
    const fresh = newProposals();
    badge.classList.toggle("new", fresh > 0);
    badge.title = fresh > 0 ? fresh + " new Claude proposals (" + open + " open)" : open + " open proposals";
  }

  let toastTimer = 0;
  function toast(msg, isError) {
    const t = $("toast");
    t.textContent = clean(msg);
    t.classList.toggle("error", !!isError);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 3000);
  }

  function showBanner(msg) {
    $("banner-text").textContent = clean(msg);
    $("banner").hidden = false;
  }

  function popupBanner() {
    S.popupsBlocked = true;
    const o = location.origin;
    showBanner("Your browser blocked the extra tabs. To allow them in Edge or Chrome, click the " +
      "blocked pop-up icon at the right end of the address bar and choose \"Always allow pop-ups " +
      "and redirects from " + o + "\" (or add " + o + " under Settings > Privacy / Cookies and site " +
      "permissions > Pop-ups and redirects > Allow). Allow only " + o + ". Until then, items open " +
      "one at a time in a single tab.");
  }

  // ------------------------------------------------------------------ preview, links

  function thumbUrl(id, size) {
    return "/thumb/" + id + "/" + size + "?b=" + encodeURIComponent(S.bundleTag);
  }

  function showPreview(id) {
    if (!Number.isInteger(id)) return;
    $("preview-img").setAttribute("src", thumbUrl(id, "p"));
    $("preview").hidden = false;
  }

  function hidePreview(force) {
    if (S.pinned && !force) return;
    S.pinned = false;
    $("preview").hidden = true;
  }

  function openPhotos(it, name) {
    const url = safeHref(it && it.photos_url);
    if (!url) {
      toast("No Google Photos link for this item.", true);
      return null;
    }
    const w = window.open(url, name || "gphotos");
    if (!w) popupBanner();
    return w;
  }

  function photosLink(it) {
    const url = safeHref(it.photos_url);
    if (!url) return el("span", { class: "muted small" }, "no Google Photos link");
    const low = it.link_conf !== "high";
    return el("a", {
      class: "gp-link" + (low ? " low" : ""), href: url, target: "gphotos", rel: "noreferrer",
      title: low ? "Low confidence: this may open a search or a different photo" : "Open in Google Photos",
      onclick: (ev) => { ev.preventDefault(); openPhotos(it); },
    }, low ? "Find in Google Photos (low confidence)" : "Open in Google Photos");
  }

  // ------------------------------------------------------------------ item cards

  function badge(text, cls) { return el("span", { class: "badge " + (cls || "") }, text); }

  // opts: selectable (bool), extra (fn(item) -> nodes), cls (extra class), num (label)
  function card(it, opts) {
    opts = opts || {};
    const diff = new Set(it.diff || []);
    const c = el("article", { class: "card" + (opts.cls ? " " + opts.cls : ""), tabindex: "-1" });
    c._item = it;
    c._opts = opts;
    if (Number.isInteger(it.id)) {
      const img = el("img", { class: "thumb", alt: "", loading: "lazy", src: thumbUrl(it.id, "g") });
      img.addEventListener("mouseenter", () => showPreview(it.id));
      img.addEventListener("mouseleave", () => hidePreview(false));
      c.append(el("div", { class: "thumb-box" }, img, opts.num ? el("span", { class: "num" }, String(opts.num)) : null));
    } else {
      c.append(el("div", { class: "thumb-box missing" }, "not in this bundle"));
    }
    const f = (name, ...text) => el("div", { class: "field " + name + (diff.has(name) ? " diff" : "") }, ...text);
    c.append(f("filename", it.filename || it.uid || ""));
    if (it.id !== null && it.id !== undefined) {
      const when = el("div", { class: "field when" },
        el("span", { class: diff.has("local_date") ? "diff" : "" }, it.local_date || "no date"), " ",
        el("span", { class: diff.has("local_time") ? "diff" : "" }, it.local_time || ""),
        it.day_uncertain ? badge("time zone uncertain", "warn") : null);
      c.append(when);
      c.append(el("div", { class: "field dims" },
        el("span", { class: diff.has("dims") ? "diff" : "" }, (it.width || "?") + "x" + (it.height || "?")), " \u00b7 ",
        el("span", { class: diff.has("size_bytes") ? "diff" : "" }, fmtSize(it.size_bytes)), " \u00b7 ",
        el("span", { class: diff.has("ext") ? "diff" : "" }, (it.ext || "").toUpperCase())));
    }
    const badges = el("div", { class: "badges" });
    if (it.shared) badges.append(badge("shared album", "warn"));
    if (it.partner) badges.append(badge("partner", "warn"));
    if (it.favorited) badges.append(badge("favorite", "warn"));
    if (it.possible_same_item) badges.append(badge("possible same item", "warn"));
    if (it.albums_n) badges.append(badge("in " + it.albums_n + " album" + (it.albums_n > 1 ? "s" : ""), diff.has("albums_n") ? "diff" : ""));
    if (it.match_conf && it.match_conf !== "high") badges.append(badge("pairing: " + it.match_conf, "muted" + (diff.has("match_conf") ? " diff" : "")));
    if (it.is_keeper) badges.append(badge("keeper", "good"));
    if (it.is_best) badges.append(badge("best of burst", "good"));
    if (it.queue) badges.append(badge(it.queue.deleted ? "deleted" : it.queue.status, "status " + it.queue.status));
    if (typeof it.sim === "number") badges.append(badge("match " + it.sim.toFixed(2), "muted"));
    if (badges.childNodes.length) c.append(badges);
    if (it.flags && it.flags.length) {
      c.append(el("ul", { class: "flags" }, it.flags.slice(0, 4).map((fl) =>
        el("li", {}, fl.category + " " + Number(fl.score).toFixed(2) + (fl.reason ? ": " + fl.reason : "")))));
    }
    if (it.albums && it.albums.length) c.append(el("div", { class: "field small muted" }, "albums: " + it.albums.slice(0, 3).join(", ")));
    c.append(el("div", { class: "field" }, photosLink(it)));
    if (opts.extra) c.append(...[].concat(opts.extra(it, c)).filter(Boolean));
    c.addEventListener("click", (ev) => {
      if (ev.target.closest("a, button, input, label, select")) return;
      focusCard(S.cards.indexOf(c));
      if (opts.selectable) toggleSelect(c, ev.shiftKey);
    });
    if (opts.selectable && S.sel.has(it.id)) c.classList.add("selected");
    return c;
  }

  function collectCards() {
    S.cards = Array.from($("view").querySelectorAll(".card"));
  }

  function focusCard(i, scroll) {
    if (!S.cards.length) return;
    i = Math.max(0, Math.min(S.cards.length - 1, i));
    if (S.focus >= 0 && S.cards[S.focus]) S.cards[S.focus].classList.remove("focused");
    S.focus = i;
    const c = S.cards[i];
    c.classList.add("focused");
    if (scroll !== false) c.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (S.pinned && c._item) showPreview(c._item.id);
  }

  function focused() { return S.cards[S.focus] || null; }

  function toggleSelect(c, range) {
    const i = S.cards.indexOf(c);
    const id = c._item.id;
    if (!Number.isInteger(id)) return;
    if (range && S.lastSel >= 0) {
      // Shift-click selects everything between the previous click and this one.
      const [a, b] = S.lastSel < i ? [S.lastSel, i] : [i, S.lastSel];
      for (let k = a; k <= b; k++) {
        const ck = S.cards[k];
        if (!ck._opts.selectable || !Number.isInteger(ck._item.id)) continue;
        S.sel.add(ck._item.id);
        ck.classList.add("selected");
      }
    } else {
      if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
      c.classList.toggle("selected", S.sel.has(id));
    }
    S.lastSel = i;
    updateSelCount();
  }

  function selectedItems() {
    return S.cards.filter((c) => S.sel.has(c._item.id)).map((c) => c._item);
  }

  function updateSelCount() {
    for (const e of document.querySelectorAll("[data-selcount]")) e.textContent = String(S.sel.size);
  }

  function selectAll(on) {
    for (const c of S.cards) {
      if (!c._opts.selectable || !Number.isInteger(c._item.id)) continue;
      if (on) S.sel.add(c._item.id); else S.sel.delete(c._item.id);
      c.classList.toggle("selected", on);
    }
    updateSelCount();
  }

  function pager(res, limit, go) {
    const total = res.total || 0;
    const off = res.offset || 0;
    const shown = Math.min(total, off + limit);
    return el("div", { class: "pager" },
      el("button", { type: "button", disabled: off <= 0, onclick: () => go(Math.max(0, off - limit)) }, "Previous"),
      el("span", { class: "muted" }, total ? (off + 1) + "-" + shown + " of " + total : "nothing here"),
      el("button", { type: "button", disabled: res.next_offset === null || res.next_offset === undefined, onclick: () => go(res.next_offset) }, "Next"));
  }

  function selectionBar(addLabel, onAdd) {
    return el("div", { class: "toolbar" },
      el("button", { type: "button", onclick: () => selectAll(true) }, "Select all on page"),
      el("button", { type: "button", onclick: () => selectAll(false) }, "Clear selection"),
      el("button", { type: "button", class: "primary", onclick: onAdd }, addLabel + " (", el("span", { "data-selcount": "" }, String(S.sel.size)), ")"));
  }

  // ------------------------------------------------------------------ Duplicates

  async function renderDups(view) {
    const res = await getJSON("/api/dups", { kind: S.dups.kind, offset: S.dups.offset, limit: 20 });
    if (!res.ok) return view.replaceChildren(el("p", { class: "error" }, errText(res.data)));
    const d = res.data;
    const kindSel = el("select", { "aria-label": "Kind", onchange: (ev) => { S.dups.kind = ev.target.value; S.dups.offset = 0; render(); } },
      [["", "All groups"], ["exact", "Exact copies"], ["near", "Near-identical"]].map(([v, t]) =>
        el("option", { value: v, selected: S.dups.kind === v }, t)));
    const go = (off) => { S.dups.offset = off; render(); };
    const parts = [el("div", { class: "toolbar" }, kindSel,
      el("span", { class: "muted" }, "Click \"Keep this\" or press 1-9 to choose the copy to keep. The keeper is outlined; values that differ from it are highlighted."))];
    if (d.skipped_buckets) parts.push(el("p", { class: "muted small" }, "Note: " + d.skipped_buckets + " oversized similarity buckets were not compared."));
    for (const g of d.groups) parts.push(groupView(g));
    parts.push(pager(d, 20, go));
    view.replaceChildren(...parts);
  }

  function groupView(g) {
    const dismissed = g.decision === "dismissed";
    const nonKeepers = g.members.filter((m) => !m.is_keeper);
    const allQueued = nonKeepers.every((m) => m.queue && m.queue.status === "approved");
    const sec = el("section", { class: "group" + (dismissed ? " dismissed" : "") });
    sec._group = g;
    sec.append(el("h3", {},
      "Group " + g.group_id + " \u00b7 " + g.kind + " \u00b7 " + g.size + " copies ",
      g.deletable ? badge("deletable", "good") : badge("not deletable here", "warn"),
      dismissed ? badge("marked not duplicates", "muted") : null,
      g.n_approved ? badge(g.n_approved + " queued", "status approved") : null));
    if (!g.deletable) {
      sec.append(el("p", { class: "muted small" }, "These copies share one Google Photos item or have no link, so deleting one in Google Photos would delete them all."));
    }
    const row = el("div", { class: "members" });
    g.members.forEach((m, idx) => {
      row.append(card(m, {
        cls: m.is_keeper ? "keeper" : "", num: idx + 1,
        extra: () => m.is_keeper ? el("div", { class: "keeper-label" }, "Keeper")
          : el("button", { type: "button", onclick: () => setKeeper(g, m) }, "Keep this"),
      }));
    });
    sec.append(row);
    const queueBtn = el("button", {
      type: "button", class: "primary", disabled: !g.deletable || dismissed || allQueued,
      title: !g.deletable ? "Not deletable: the copies are one Google Photos item" : "",
      onclick: async () => {
        const r = await mutate("/api/dups/queue", { group_ids: [g.group_id] });
        if (r) { toast("Queued " + r.changed + " copies for deletion."); render(true); }
      },
    }, allQueued ? "Non-keepers queued" : "Queue non-keepers");
    const dismissBtn = el("button", {
      type: "button",
      onclick: async () => {
        const r = await mutate("/api/dups/dismiss", { group_key: g.group_key, undo: dismissed });
        if (r) render(true);
      },
    }, dismissed ? "Undo 'not duplicates'" : "Not duplicates");
    sec.append(el("div", { class: "toolbar" }, queueBtn, dismissBtn));
    return sec;
  }

  async function setKeeper(g, m) {
    const r = await mutate("/api/dups/keeper", { group_key: g.group_key, uid: m.uid });
    if (r) render(true);
  }

  // ------------------------------------------------------------------ Junk

  async function renderJunk(view) {
    const J = S.junk;
    const res = await getJSON("/api/junk", { category: J.category, min_score: J.min, year: J.year, offset: J.offset, limit: PAGE });
    if (!res.ok) return view.replaceChildren(el("p", { class: "error" }, errText(res.data)));
    const d = res.data;
    const chips = el("div", { class: "chips" }, (S.stats.junk_categories || []).map((cat) =>
      el("button", {
        type: "button", class: "chip" + (cat === J.category ? " active" : ""),
        onclick: () => { J.category = cat; J.offset = 0; S.sel.clear(); render(); },
      }, cat.replace("_", " ") + " ", el("span", { class: "count" }, String(d.counts[cat] || 0)))));
    const valueLabel = el("span", { class: "muted" }, Number(J.min).toFixed(2));
    const slider = el("input", {
      type: "range", min: "0", max: "1", step: "0.05", value: String(J.min), "aria-label": "Minimum score",
      oninput: (ev) => { valueLabel.textContent = Number(ev.target.value).toFixed(2); },
      onchange: (ev) => { J.min = Number(ev.target.value); J.offset = 0; render(); },
    });
    const years = Object.keys((S.stats.bundle && S.stats.bundle.years) || {});
    const yearSel = el("select", { "aria-label": "Year", onchange: (ev) => { J.year = ev.target.value; J.offset = 0; render(); } },
      el("option", { value: "" }, "All years"), years.map((y) => el("option", { value: y, selected: String(J.year) === y }, y)));
    const grid = el("div", { class: "grid" }, d.rows.map((it) => card(it, { selectable: true })));
    view.replaceChildren(
      chips,
      el("div", { class: "toolbar" }, el("label", {}, "Minimum score ", slider, " ", valueLabel), yearSel),
      selectionBar("Add selected", () => addSelected("junk: " + J.category + " (score >= " + Number(J.min).toFixed(2) + ")", J.category)),
      grid, pager(d, PAGE, (off) => { J.offset = off; render(); }));
  }

  async function addSelected(reason, category) {
    let items = selectedItems();
    if (!items.length && focused()) items = [focused()._item];
    const ids = items.map((it) => it.id).filter(Number.isInteger);
    if (!ids.length) return toast("Select some photos first (click, shift-click, or x).", true);
    const r = await mutate("/api/queue/add", { ids, reason, category: category || null });
    if (r) {
      toast("Added " + r.changed + " to the deletion queue (approved).");
      S.sel.clear();
      render(true);
    }
  }

  // ------------------------------------------------------------------ Search

  function renderSearchForm() {
    const Q = S.search;
    const textOk = S.textSearch === "ready";
    const statusText = {
      ready: "Text search ready.", loading: "Loading the CLIP text model...",
      not_loaded: "Text search not started.", none: "This bundle has no CLIP embeddings: text search is off, filters work.",
      unavailable: "Text search unavailable (CLIP model missing). Filters still work.",
    }[S.textSearch] || "";
    const q = el("input", { type: "search", value: Q.q, placeholder: textOk ? "Describe the photos, e.g. receipt" : "text search unavailable", disabled: !textOk, "aria-label": "Text query", maxlength: "200" });
    const fn = el("input", { type: "search", value: Q.filename, placeholder: "file name contains", "aria-label": "File name contains", maxlength: "200" });
    const from = el("input", { type: "date", value: Q.date_from, "aria-label": "From date" });
    const to = el("input", { type: "date", value: Q.date_to, "aria-label": "To date" });
    const cat = el("select", { "aria-label": "Junk category" }, el("option", { value: "" }, "Any photo"),
      el("option", { value: "any", selected: Q.category === "any" }, "Any junk score"),
      (S.stats.junk_categories || []).map((c) => el("option", { value: c, selected: Q.category === c }, c)));
    const run = () => {
      Object.assign(Q, { q: q.value.trim(), filename: fn.value.trim(), date_from: from.value, date_to: to.value, category: cat.value, offset: 0, ran: true });
      S.sel.clear();
      render();
    };
    const form = el("form", { class: "search-form", onsubmit: (ev) => { ev.preventDefault(); run(); } },
      q, fn, cat, el("label", {}, "from ", from), el("label", {}, "to ", to),
      el("button", { type: "submit", class: "primary" }, "Search"));
    const chips = el("div", { class: "chips" }, PRESETS.map((p) => el("button", {
      type: "button", class: "chip" + (Q.q === p ? " active" : ""), disabled: !textOk,
      onclick: () => { q.value = p; run(); },
    }, p)));
    return [el("p", { class: "muted small" }, statusText), form, chips];
  }

  async function renderSearch(view) {
    const Q = S.search;
    const head = renderSearchForm();
    if (!Q.ran) return view.replaceChildren(...head, el("p", { class: "muted" }, "Pick a preset or enter filters, then Search."));
    const res = await getJSON("/api/search", { q: Q.q, filename: Q.filename, date_from: Q.date_from, date_to: Q.date_to, category: Q.category, offset: Q.offset, limit: PAGE });
    if (!res.ok) return view.replaceChildren(...head, el("p", { class: "error" }, errText(res.data)));
    const d = res.data;
    const label = Q.q || Q.filename || Q.category || "filters";
    view.replaceChildren(...head,
      selectionBar("Add selected", () => addSelected("search: " + label, Q.category && Q.category !== "any" ? Q.category : null)),
      el("div", { class: "grid" }, d.rows.map((it) => card(it, { selectable: true }))),
      pager(d, PAGE, (off) => { Q.offset = off; render(); }));
  }

  // ------------------------------------------------------------------ To delete

  async function decide(items, decision) {
    const uids = items.map((it) => it.uid).filter(Boolean);
    if (!uids.length) return null;
    return mutate("/api/queue/decide", { uids, decision });
  }

  function targetItems() {
    const sel = selectedItems();
    if (sel.length) return sel;
    return focused() ? [focused()._item] : [];
  }

  async function renderQueue(view) {
    const limit = S.sub === "approved" ? 500 : PAGE;
    const res = await getJSON("/api/queue", { status: S.sub, offset: S.queue.offset, limit });
    if (res.ok) applyState(res.data);
    const c = S.counts || {};
    const subs = [["proposed", "Proposed", c.proposed], ["approved", "Approved: deletion mode", c.approved], ["rejected", "Rejected", c.rejected]];
    const subNav = el("div", { class: "subtabs" }, subs.map(([k, t, n]) => el("button", {
      type: "button", class: S.sub === k ? "active" : "",
      onclick: () => { S.sub = k; S.queue.offset = 0; S.sel.clear(); render(); },
    }, t + " (" + (n || 0) + ")")),
      el("a", { class: "csv", href: "/api/export.csv?status=approved", download: "gpclean-approved.csv" }, "Export approved as CSV"));
    if (!res.ok) return view.replaceChildren(subNav, el("p", { class: "error" }, errText(res.data)));
    const d = res.data;
    const go = (off) => { S.queue.offset = off; render(); };
    if (S.sub === "proposed") {
      // Viewing the list clears the "new proposals" highlight on the tab badge.
      markAllSeen();
      renderSummary();
      view.replaceChildren(subNav, ...proposedView(d), pager(d, limit, go));
    } else if (S.sub === "approved") {
      view.replaceChildren(subNav, ...approvedView(d), pager(d, limit, go));
    } else {
      view.replaceChildren(subNav,
        el("div", { class: "toolbar" }, el("button", { type: "button", onclick: async () => { if (await decide(targetItems(), "reset")) render(true); } }, "Back to proposed (u)")),
        el("div", { class: "grid" }, d.rows.map((it) => card(it, { selectable: true, extra: proposalInfo }))),
        pager(d, limit, go));
    }
  }

  function proposalInfo(it) {
    const q = it.queue || {};
    const who = q.proposed_by === "user" ? "you" : q.proposed_by;
    return el("div", { class: "proposal" },
      el("div", { class: "small" }, "by " + who + (q.proposed_at ? " at " + q.proposed_at.replace("T", " ").replace("Z", " UTC") : "")),
      el("div", { class: "reason" }, q.reason || ""));
  }

  function proposedView(d) {
    const out = [];
    if (d.batches && d.batches.length) {
      out.push(el("div", { class: "batches" }, d.batches.map((b) => el("div", { class: "batch" },
        "Batch " + b.batch_id + " by " + b.proposed_by + ": " + b.n_open + " open ",
        el("button", {
          type: "button", onclick: async () => {
            if (!window.confirm("Reject all " + b.n_open + " open proposals of this batch?")) return;
            const r = await mutate("/api/queue/reject_batch", { batch_id: b.batch_id });
            if (r) { toast("Rejected " + r.changed + "."); render(true); }
          },
        }, "Reject whole batch")))));
    }
    out.push(el("div", { class: "toolbar" },
      el("button", { type: "button", onclick: () => selectAll(true) }, "Select all on page"),
      el("button", { type: "button", onclick: () => selectAll(false) }, "Clear selection"),
      el("button", { type: "button", class: "primary", onclick: () => decideAndRefresh("approve") }, "Approve selected (a)"),
      el("button", { type: "button", onclick: () => decideAndRefresh("reject") }, "Reject selected (r)"),
      el("span", { class: "muted" }, el("span", { "data-selcount": "" }, String(S.sel.size)), " selected")));
    out.push(el("div", { class: "grid" }, d.rows.map((it) => card(it, {
      selectable: true,
      extra: (item) => [proposalInfo(item), el("div", { class: "row-actions" },
        el("button", { type: "button", class: "primary", onclick: async () => { if (await decide([item], "approve")) render(true); } }, "Approve"),
        el("button", { type: "button", onclick: async () => { if (await decide([item], "reject")) render(true); } }, "Reject"))],
    }))));
    if (!d.rows.length) out.push(el("p", { class: "muted" }, "No open proposals. Ask Claude (MCP) to review a category, or add items from Junk / Search."));
    return out;
  }

  async function decideAndRefresh(decision) {
    const r = await decide(targetItems(), decision);
    if (r) { S.sel.clear(); render(true); }
  }

  function approvedView(d) {
    const out = [];
    const total = d.total || 0;
    const nDel = d.n_deleted || 0;
    out.push(el("div", { class: "progress-row" },
      el("progress", { max: String(Math.max(total, 1)), value: String(nDel) }),
      el("strong", {}, nDel + "/" + total + " deleted")));
    out.push(el("p", { class: "note" }, "Deleted items stay in Google Photos trash for " + d.trash_days + " days (until " + addDays(d.trash_days) + ")."));
    const nInput = el("input", { type: "number", min: "1", max: "20", value: String(S.openN), "aria-label": "How many to open" });
    nInput.addEventListener("change", () => { S.openN = Math.max(1, Math.min(20, parseInt(nInput.value, 10) || 5)); });
    out.push(el("div", { class: "toolbar" },
      el("button", { type: "button", class: "primary", onclick: () => openNext(S.openN) }, "Open next"), nInput,
      el("span", { class: "muted" }, "Per item: o opens it, press # in Google Photos, close that tab (Ctrl+W), then d here marks it deleted and moves on.")));
    out.push(el("p", { class: "small muted" }, "\u201cSafe to day-select\u201d means selecting the whole day in Google Photos selects exactly the approved photos: " +
      "every photo of that day is approved and nothing else can be on it (no videos or skipped files that day, no uncertain time zone, no undated items that could belong there)."));
    for (const day of d.days || []) {
      // Videos AND skipped media of that day (the API's older name for it is videos_that_day).
      const nNotIndexed = day.n_not_indexed_that_day;
      const head = el("h3", { class: "day" }, day.date || "No date / not in this bundle", " ",
        el("span", { class: "muted small" }, day.n_approved_that_day + " approved \u00b7 " + day.n_indexed_photos_that_day +
          " photos indexed \u00b7 " + nNotIndexed + " not in the index (videos, skipped files) \u00b7 " + day.n_deleted_that_day + " deleted"));
      if (day.safe_to_day_select) {
        head.append(" ", badge("safe to day-select (compare: " + day.n_approved_that_day + " selected)", "good"));
      } else if (day.date) {
        const why = dayBlockerText(day, nNotIndexed);
        const b = badge("select one by one (" + why.join("; ") + ")", "muted");
        b.title = "Not safe to select the whole day: " + why.join("; ");
        head.append(" ", b);
      }
      out.push(head);
      out.push(el("div", { class: "grid" }, day.items.map((it) => card(it, {
        cls: it.queue && it.queue.deleted ? "deleted" : "", selectable: true, extra: deletionControls,
      }))));
    }
    if (!total) out.push(el("p", { class: "muted" }, "Nothing approved yet."));
    return out;
  }

  // Why a day is not "safe to day-select": the server's day_select_blockers codes in words.
  const DAY_BLOCKER_TEXT = {
    no_indexed_photos: () => "no reviewable photos that day",
    not_all_approved: () => "not every photo approved",
    not_indexed_that_day: (day, n) => n + " not in the index that day (videos, skipped files)",
    day_uncertain: (day) => day.n_day_uncertain + " with an uncertain time zone",
    undated_in_year: (day) => day.n_undated_in_year + " undated photos from " + day.date.slice(0, 4),
    undated_unknown_year: (day) => day.n_undated_unknown_year + " undated photos of unknown year",
    unindexed_undated: (day) => day.n_unindexed_undated + " undated videos / skipped files in the library",
  };

  function dayBlockerText(day, nNotIndexed) {
    const codes = day.day_select_blockers || [];
    const out = codes.map((code) => (DAY_BLOCKER_TEXT[code] ? DAY_BLOCKER_TEXT[code](day, nNotIndexed) : "unknown reason"));
    return out.length ? out : ["unknown reason"];
  }

  function deletionControls(it, c) {
    const q = it.queue || {};
    const box = el("input", { type: "checkbox", checked: !!q.deleted });
    box.addEventListener("change", () => markDeleted(c, box.checked, false));
    return [
      el("label", { class: "deleted-box" }, box, " deleted in Google Photos"),
      q.deleted && q.recoverable_until ? el("div", { class: "small muted" }, "recoverable until " + q.recoverable_until) : null,
      el("div", { class: "row-actions" }, el("button", { type: "button", onclick: async () => { if (await decide([it], "reset")) render(true); } }, "Undo approval")),
    ];
  }

  async function markDeleted(c, deleted, advance) {
    const it = c._item;
    // One mark at a time: a second d before the reply would re-send this item and advance
    // twice, skipping the next item in the per-item deletion flow.
    let r;
    S.marking = true;
    try {
      r = await mutate("/api/queue/deleted", { uids: [it.uid], deleted });
    } finally {
      S.marking = false;
    }
    const box = c.querySelector(".deleted-box input");
    if (!r) { if (box) box.checked = !deleted; return; }
    it.queue.deleted = deleted;
    c.classList.toggle("deleted", deleted);
    if (box) box.checked = deleted;
    const prog = $("view").querySelector(".progress-row");
    if (prog && S.counts) {
      prog.querySelector("progress").setAttribute("value", String(S.counts.deleted));
      prog.querySelector("strong").textContent = S.counts.deleted + "/" + S.counts.approved + " deleted";
    }
    if (advance) {
      const next = S.cards.findIndex((k, i) => i > S.focus && !(k._item.queue && k._item.queue.deleted));
      if (next >= 0) focusCard(next);
    }
  }

  function openNext(n) {
    const start = Math.max(0, S.focus);
    const todo = S.cards.filter((c, i) => i >= start && c._item.queue && !c._item.queue.deleted && safeHref(c._item.photos_url));
    if (!todo.length) return toast("Nothing left to open from here.", true);
    const count = S.popupsBlocked ? 1 : Math.min(n, todo.length);
    for (let i = 0; i < count; i++) {
      const w = window.open(todo[i]._item.photos_url, i === 0 ? "gphotos" : "gphotos-" + i);
      if (!w) { popupBanner(); break; }
    }
    focusCard(S.cards.indexOf(todo[0]));
  }

  // ------------------------------------------------------------------ rendering + tabs

  const RENDER = { dups: renderDups, junk: renderJunk, search: renderSearch, queue: renderQueue };

  async function render(keep) {
    const view = $("view");
    const oldFocus = S.focus;
    const scroll = window.scrollY;
    S.busy = true;
    try {
      await RENDER[S.tab](view);
    } catch (e) {
      view.replaceChildren(el("p", { class: "error" }, "Could not load this view. Is gpclean serve still running?"));
    } finally {
      S.busy = false;
    }
    collectCards();
    S.focus = -1;
    if (keep) {
      window.scrollTo(0, scroll);
      if (S.cards.length && oldFocus >= 0) focusCard(Math.min(oldFocus, S.cards.length - 1), false);
    } else {
      window.scrollTo(0, 0);
    }
    updateSelCount();
  }

  function setTab(tab) {
    if (!RENDER[tab]) tab = "dups";
    S.tab = tab;
    S.sel.clear();
    S.lastSel = -1;
    for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("active", b.dataset.tab === tab);
    if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    render();
  }

  // ------------------------------------------------------------------ keyboard

  function groupOfFocus() {
    const c = focused();
    const sec = c ? c.closest(".group") : $("view").querySelector(".group");
    return sec ? sec._group : null;
  }

  function onKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const tag = (ev.target.tagName || "").toLowerCase();
    if (ev.key === "Escape") { hidePreview(true); $("help").hidden = true; return; }
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    const c = focused();
    const it = c ? c._item : null;
    const inQueue = S.tab === "queue";
    switch (ev.key) {
      case "j": focusCard(S.focus + 1); break;
      case "k": focusCard(S.focus <= 0 ? 0 : S.focus - 1); break;
      case " ":
        if (!it) return;
        if (S.pinned) hidePreview(true); else { S.pinned = true; showPreview(it.id); }
        break;
      case "x": if (c && c._opts.selectable) toggleSelect(c, false); break;
      case "o": if (it) openPhotos(it); break;
      case "d": if (S.marking) break; if (inQueue && S.sub === "approved" && it && it.queue && !it.queue.deleted) markDeleted(c, true, true); break;
      case "a":
        if (inQueue) decideAndRefresh("approve");
        else if (S.tab === "junk") addSelected("junk: " + S.junk.category, S.junk.category);
        else if (S.tab === "search") addSelected("search: " + (S.search.q || S.search.filename || "filters"), null);
        break;
      case "r": if (inQueue) decideAndRefresh("reject"); break;
      case "u": if (inQueue) decideAndRefresh("reset"); break;
      case "?": $("help").hidden = !$("help").hidden; break;
      default:
        if (S.tab === "dups" && /^[1-9]$/.test(ev.key)) {
          const g = groupOfFocus();
          const m = g && g.members[Number(ev.key) - 1];
          if (m && !m.is_keeper) setKeeper(g, m);
          break;
        }
        return;
    }
    ev.preventDefault();
  }

  // ------------------------------------------------------------------ live updates

  async function poll() {
    if (document.visibilityState !== "visible" || S.busy) return;
    let res;
    try { res = await getJSON("/api/rev"); } catch (e) { return; }
    if (!res.ok) return;
    const changed = res.data.rev !== S.rev;
    const textChanged = res.data.text_search !== S.textSearch;
    applyState(res.data);
    // Someone else (Claude over MCP, another tab) changed the queue: refresh the queue view.
    if ((changed && S.tab === "queue") || (textChanged && S.tab === "search")) render(true);
  }

  // ------------------------------------------------------------------ start

  async function start() {
    for (const b of document.querySelectorAll("#tabs button")) b.addEventListener("click", () => setTab(b.dataset.tab));
    $("help-button").addEventListener("click", () => { $("help").hidden = !$("help").hidden; });
    $("help-close").addEventListener("click", () => { $("help").hidden = true; });
    $("banner-close").addEventListener("click", () => { $("banner").hidden = true; });
    $("preview").addEventListener("click", () => hidePreview(true));
    document.addEventListener("keydown", onKey);
    const res = await getJSON("/api/stats");
    if (!res.ok) {
      $("view").replaceChildren(el("p", { class: "error" }, errText(res.data)));
      return;
    }
    S.stats = res.data;
    S.bundleTag = res.data.bundle_tag || "";
    S.textSearch = res.data.text_search;
    S.junk.min = res.data.junk_default_min;
    applyState({ rev: res.data.rev, counts: res.data.queue, batches: res.data.batches });
    setTab((location.hash || "#dups").slice(1));
    setInterval(poll, 2000);
  }

  document.addEventListener("DOMContentLoaded", start);
})();
