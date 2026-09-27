"""Tests for gpclean.merge.collapse: url collapse, url-less collapse, aliases and item_uid."""

from __future__ import annotations

import hashlib
import random

from shard_factory import fake_url, fake_url_id

from gpclean.merge.collapse import collapse

EA, EB = "takeout-20260101T000000Z", "takeout-20260201T000000Z"


def rec(filename, *, url=None, export=EA, folder="Photos from 2021", zipkey="zA",
        member_idx=0, sha="x", date="2021-07-16", **extra) -> dict:
    return {
        "filename": filename, "url": url, "export_id": export, "folder": folder,
        "folder_kind": "year" if folder.startswith("Photos from") else "album",
        "zipkey": zipkey, "zip_name": zipkey + ".zip", "member_idx": member_idx,
        "member": f"Takeout/Google Photos/{folder}/{filename}",
        "sha256": hashlib.sha256(sha.encode()).digest(), "local_date": date,
        "favorited": 0, "shared": 0, "partner": 0, "archived": 0, **extra,
    }


def _by_uid(items):
    return {i.uid: i for i in items}


def test_year_record_wins_over_album_copy():
    url = fake_url(1)
    album = rec("IMG_1.jpg", url=url, folder="Summer Trip", member_idx=1)
    year = rec("IMG_1.jpg", url=url, member_idx=9)
    items = collapse([album, year], {url: {"Summer Trip", "Best of"}})
    assert len(items) == 1
    it = items[0]
    assert it.rec["folder"] == "Photos from 2021"
    assert [(a["folder"], reason) for a, reason in it.aliases] == [("Summer Trip", "album_copy")]
    assert it.albums == {"Summer Trip", "Best of"}
    assert it.uid == "g:" + fake_url_id(1)


def test_newest_export_wins_then_lowest_member():
    url = fake_url(2)
    old = rec("IMG_2.jpg", url=url, export=EA, zipkey="zA", member_idx=0)
    new = rec("IMG_2.jpg", url=url, export=EB, zipkey="zB", member_idx=5)
    it = collapse([old, new])[0]
    assert it.rec["export_id"] == EB
    assert it.aliases[0][1] == "double_export"
    # Same export and kind: lowest (zipkey, member_idx) is primary; the other is "same_url".
    a = rec("IMG_3.jpg", url=fake_url(3), zipkey="z1", member_idx=7)
    b = rec("IMG_3.jpg", url=fake_url(3), zipkey="z1", member_idx=2)
    it = collapse([a, b])[0]
    assert it.rec["member_idx"] == 2 and it.aliases[0][1] == "same_url"


def test_urlless_copy_collapses_into_url_item_across_exports():
    url = fake_url(4)
    with_url = rec("IMG_4.jpg", url=url, export=EA, sha="s4")
    no_url = rec("img_4.JPG", export=EB, zipkey="zB", sha="s4")
    items = collapse([with_url, no_url])
    assert len(items) == 1
    assert items[0].rec["url"] == url
    assert items[0].aliases[0][1] == "double_export_nourl"


def test_urlless_records_need_same_sha_name_and_exif_day():
    base = rec("IMG_5.jpg", sha="s5", export=EA, exif_dt="2021:07:16 10:00:00")
    other_day = rec("IMG_5.jpg", sha="s5", export=EB, zipkey="zB",
                    exif_dt="2021:07:17 10:00:00")
    other_sha = rec("IMG_5.jpg", sha="zz", export=EB, zipkey="zB", member_idx=1,
                    exif_dt="2021:07:16 10:00:00")
    other_name = rec("IMG_6.jpg", sha="s5", export=EB, zipkey="zB", member_idx=2,
                     exif_dt="2021:07:16 10:00:00")
    items = collapse([base, other_day, other_sha, other_name])
    assert len(items) == 4
    uids = sorted(i.uid for i in items)
    sha5 = hashlib.sha256(b"s5").hexdigest()
    # Same (sha, name), different EXIF days -> distinct items with a deterministic ":2".
    assert f"s:{sha5}:IMG_5.jpg" in uids and f"s:{sha5}:IMG_5.jpg:2" in uids
    assert f"s:{sha5}:IMG_6.jpg" in uids


def test_urlless_copy_without_exif_collapses_despite_sidecar_date():
    """C3 without EXIF (a PNG / messenger save): the url record's local_date comes from its
    sidecar, the url-less copy (sidecar missing in that export) has none. The key uses only
    what is inside the file, so they are still one item, not a fake duplicate."""
    url = fake_url(13)
    with_url = rec("chat.png", url=url, export=EA, sha="png", date="2021-07-16")
    no_url = rec("chat.png", export=EB, zipkey="zB", sha="png", date=None)
    items = collapse([with_url, no_url])
    assert len(items) == 1 and items[0].rec["url"] == url
    assert items[0].aliases == [(no_url, "double_export_nourl")]
    # EXIF DateTime (no DateTimeOriginal) is inside the file too, and counts.
    a = rec("s.jpg", url=fake_url(14), sha="s", date="2021-07-16",
            exif_dt_any="2021:07:15 23:00:00")
    b = rec("s.jpg", export=EB, zipkey="zB", sha="s", date="2021-07-15",
            exif_dt_any="2021:07:15 23:00:00")
    assert len(collapse([a, b])) == 1


def test_urlless_records_collapse_among_themselves():
    a = rec("IMG_7.jpg", sha="s7", export=EA)
    b = rec("IMG_7.jpg", sha="s7", export=EB, zipkey="zB")
    items = collapse([a, b])
    assert len(items) == 1
    assert items[0].rec["export_id"] == EB  # newest export
    assert items[0].aliases[0][1] == "double_export_nourl"


def test_ambiguous_urlless_copy_stays_separate():
    """Byte-identical items under two urls (a real duplicate) plus a url-less copy: the copy
    could belong to either, so it is not collapsed into one of them."""
    a = rec("IMG_8.jpg", url=fake_url(8), sha="s8")
    b = rec("IMG_8.jpg", url=fake_url(9), sha="s8", folder="Photos from 2022", member_idx=1)
    c = rec("IMG_8.jpg", sha="s8", export=EB, zipkey="zB")
    items = collapse([a, b, c])
    assert len(items) == 3
    assert all(not i.aliases for i in items)


def test_flags_are_merged_from_aliases():
    url = fake_url(10)
    a = rec("IMG_9.jpg", url=url, export=EB)
    b = rec("IMG_9.jpg", url=url, export=EA, favorited=1, shared=1)
    it = collapse([a, b])[0]
    assert it.rec["favorited"] == 1 and it.rec["shared"] == 1


def test_album_only_item_counts_its_own_album():
    url = fake_url(11)
    only = rec("IMG_10.jpg", url=url, folder="Shared Trip")
    it = collapse([only], {url: {"Shared Trip"}})[0]
    assert it.albums == {"Shared Trip"} and not it.aliases


def test_uids_do_not_depend_on_input_order():
    recs = [rec("IMG_5.jpg", sha="s5", date=f"2021-07-1{k}", zipkey=f"z{k}",
                exif_dt=f"2021:07:1{k} 10:00:00") for k in range(5)]
    recs.append(rec("IMG_11.jpg", url=fake_url(12)))
    want = {(i.rec["local_date"], i.uid) for i in collapse(list(recs))}
    for seed in range(3):
        shuffled = list(recs)
        random.Random(seed).shuffle(shuffled)
        assert {(i.rec["local_date"], i.uid) for i in collapse(shuffled)} == want
    assert len({uid for _d, uid in want}) == 6
