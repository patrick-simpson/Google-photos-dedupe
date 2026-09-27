"""Collapse several Takeout records of one Google Photos library item into one index item.

A library item can appear more than once in the zips: in its year folder *and* in album
folders, and once per export when two exports overlap. Those records are the same item (one
url, one delete in Google Photos), so they must become one index item, not a duplicate group.

- Same validated url -> one item. The primary record is the year-folder one over an album
  copy, then the newest export, then the lowest (zipkey, member_idx); the others become
  aliases (``album_copy`` / ``double_export`` / ``same_url``).
- Records without a url (no or unpaired sidecar) collapse when (sha256, NFC-casefolded
  filename, EXIF capture day or None) agree: into the single url item with that key if there
  is exactly one (``double_export_nourl`` / ``album_copy``), else among themselves. Two
  different urls are never merged: that is what duplicate groups are for. The key uses only
  facts inside the file, so a copy without its sidecar still finds the item.

``item_uid`` is ``g:<id from the url>`` or ``s:<sha256 hex>:<filename>``; colliding ``s:``
uids get ``:2``, ``:3``... in a deterministic order, so review decisions keyed by uid survive a
re-merge.
"""

from __future__ import annotations

import logging
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from gpclean.merge.localtime import parse_exif_dt

log = logging.getLogger(__name__)


@dataclass
class Item:
    """One library item: its primary record plus aliases and album membership."""

    rec: dict
    aliases: list[tuple[dict, str]] = field(default_factory=list)  # (record, reason)
    albums: set[str] = field(default_factory=set)
    uid: str = ""


def _norm_name(filename: str) -> str:
    return unicodedata.normalize("NFC", filename).casefold()


def _intrinsic_day(rec: dict) -> str | None:
    """The capture day written *inside* the file (EXIF DateTimeOriginal, else DateTime).

    Not ``local_date``: for a record with a sidecar that may come from ``photoTakenTime``,
    which a url-less copy of the same bytes (no sidecar) cannot know, so the two keys would
    never match for images without EXIF (PNGs, messenger saves, screenshots).
    """
    d = parse_exif_dt(rec.get("exif_dt")) or parse_exif_dt(rec.get("exif_dt_any"))
    return d.strftime("%Y-%m-%d") if d is not None else None


def _nourl_key(rec: dict) -> tuple:
    """Collapse key built only from facts byte-identical records share."""
    return (bytes(rec["sha256"]), _norm_name(rec["filename"]), _intrinsic_day(rec))


def _precedence(records: list[dict]):
    """Sort key: year folder first, then newest export, then lowest (zipkey, member_idx)."""
    exports = sorted({r["export_id"] for r in records})
    newest_first = {e: -k for k, e in enumerate(exports)}
    return lambda r: (0 if r.get("folder_kind") == "year" else 1, newest_first[r["export_id"]],
                      r["zipkey"], r["member_idx"])


def _reason(alias: dict, primary: dict, *, has_url: bool) -> str:
    if alias.get("folder_kind") == "album":
        return "album_copy"
    if alias["export_id"] != primary["export_id"]:
        return "double_export" if has_url else "double_export_nourl"
    return "same_url" if has_url else "same_file_nourl"


def url_id(url: str) -> str:
    """The item id at the end of a canonical ``https://photos.google.com/photo/<id>`` url."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def collapse(records: list[dict], albums_by_url: dict[str, set[str]] | None = None) -> list[Item]:
    """Group records into library items (see module docstring). Returns items with uids set.

    ``records`` need ``url, sha256, filename, exif_dt, exif_dt_any, folder, folder_kind,
    export_id, zipkey, member_idx`` (+ the flags merged below).
    """
    albums_by_url = albums_by_url or {}
    key = _precedence(records) if records else None
    items: list[Item] = []

    by_url: dict[str, list[dict]] = defaultdict(list)
    nourl: list[dict] = []
    for r in records:
        (by_url[r["url"]] if r.get("url") else nourl).append(r)

    url_items_by_key: dict[tuple, list[Item]] = defaultdict(list)
    for url in sorted(by_url):
        recs = sorted(by_url[url], key=key)
        item = Item(rec=dict(recs[0]))
        item.aliases = [(r, _reason(r, recs[0], has_url=True)) for r in recs[1:]]
        item.albums = set(albums_by_url.get(url, ()))
        items.append(item)
        # Every record of the url item offers its key, so a url-less copy of *any* of them
        # (they are normally byte-identical) finds it.
        for k in {_nourl_key(r) for r in recs}:
            url_items_by_key[k].append(item)

    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for r in nourl:
        k = _nourl_key(r)
        owners = url_items_by_key.get(k, [])
        if len(owners) == 1:
            owner = owners[0]
            owner.aliases.append((r, _reason(r, owner.rec, has_url=False)))
        else:
            # No url item, or several (ambiguous: which item would this copy belong to?).
            clusters[k].append(r)
    for k in sorted(clusters, key=lambda k: (k[0], k[1], k[2] or "")):
        recs = sorted(clusters[k], key=key)
        if len(url_items_by_key.get(k, [])) > 1:
            # Ambiguous between urls: keep each record as its own item (they will form an
            # exact-duplicate group that is shown but not deletable on its own).
            items.extend(Item(rec=dict(r)) for r in recs)
            continue
        item = Item(rec=dict(recs[0]))
        item.aliases = [(r, _reason(r, recs[0], has_url=False)) for r in recs[1:]]
        items.append(item)

    for item in items:
        if item.rec.get("folder_kind") == "album":
            item.albums.add(item.rec["folder"])
        for r, _reason_ in item.aliases:
            if r.get("folder_kind") == "album":
                item.albums.add(r["folder"])
            # Flags describe the library item; any record that knows one counts.
            for flag in ("favorited", "shared", "partner", "archived"):
                if r.get(flag):
                    item.rec[flag] = 1
    assign_uids(items)
    log.info("collapse: records=%d items=%d aliases=%d", len(records), len(items),
             sum(len(i.aliases) for i in items))
    return items


def assign_uids(items: list[Item]) -> None:
    """Set ``item.uid`` for every item; ``s:`` collisions get ``:2``, ``:3``... suffixes."""
    by_base: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        r = item.rec
        if r.get("url"):
            item.uid = "g:" + url_id(r["url"])
        else:
            by_base[f"s:{bytes(r['sha256']).hex()}:{r['filename']}"].append(item)
    taken = {i.uid for i in items if i.uid}
    for base, group in by_base.items():
        group.sort(key=lambda i: (i.rec.get("local_date") or "", i.rec["folder"],
                                  i.rec["export_id"], i.rec["zip_name"], i.rec["member"]))
        n = 1
        for item in group:
            uid = base if n == 1 else f"{base}:{n}"
            while uid in taken:  # paranoia: a suffixed uid equal to another item's base
                n += 1
                uid = f"{base}:{n}"
            item.uid = uid
            taken.add(uid)
            n += 1
