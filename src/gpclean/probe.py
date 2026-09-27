"""``gpclean probe``: one-off measurements of the runner, the Drive read path and the export.

docs/PLAN.md §2 "Probe". The public log gets aggregates only (ints, floats, bools through
:mod:`gpclean.publiclog`); everything with names in it (zip names, folder names, sample
photo URLs) goes to ``probe/<run>/probe.json`` and ``probe/<run>/urls-sample.txt`` on Drive.

Every measurement is a separate section: a section that fails is recorded (by exception
type) and the probe carries on, and sections are skipped once the time budget (~50 min,
inside the job's 60-minute timeout) runs low. A quota hit stops the probe with exit 3.
With ``clip_model=none`` the CLIP benchmark is skipped.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import random
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from gpclean import ci, publiclog
from gpclean.publiclog import Ev
from gpclean.rclone import QuotaError

log = logging.getLogger(__name__)

PHASE = ci.PHASE_PROBE
BUDGET_S = 50 * 60
N_READ = 300          # members per range-read test (sequential and random)
N_PROCESS = 300       # photos for the full-processing (no CLIP) test
N_DRAFT = 50          # JPEGs for the draft-size check
N_URLS = 20           # sample URLs for the manual link check (private)
CLIP_PROCS = 4
CLIP_IMAGES = 32      # per process
_SIDECAR_KEYS = ("title", "taken_ts", "creation_ts", "lat", "url", "description", "people",
                 "origin_folder", "device_type", "from_shared_album", "from_partner",
                 "favorited", "archived", "trashed")


class _Budget:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()


def _net_rx_bytes() -> int | None:
    """Bytes received on all non-loopback interfaces (Linux), to measure rclone's downloads.

    Our reads from ``rclone serve http`` go over loopback; rclone's own downloads from Google
    arrive on the real interface, so the ratio of the two is the over-read factor.
    """
    try:
        lines = Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]
    except OSError:
        return None
    total = 0
    for line in lines:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        fields = rest.split()
        if fields:
            total += int(fields[0])
    return total


def _system() -> dict:
    """nproc, RAM, free disk and CPU flags."""
    info: dict = {"nproc": os.cpu_count() or 0}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                info["ram_mb"] = int(line.split()[1]) // 1024
    except OSError:
        pass
    for key, path in (("disk_root_free_gb", "/"),
                      ("disk_tmp_free_gb", tempfile.gettempdir())):
        try:
            info[key] = round(shutil.disk_usage(path).free / 1e9, 1)
        except OSError:
            pass
    try:
        flags = set()
        for line in Path("/proc/cpuinfo").read_text(encoding="ascii").splitlines():
            if line.startswith("flags"):
                flags.update(line.partition(":")[2].split())
                break
        info["avx2"] = "avx2" in flags
        info["avx512"] = any(f.startswith("avx512") for f in flags)
    except OSError:
        pass
    return info


def _read_members(src, entries: list, *, one_by_one: bool, draft: dict | None = None) -> dict:
    """Read the members' bytes through ``src``; return timing and byte counters."""
    zf = src.zipfile()
    infos = zf.infolist()
    if not one_by_one:
        src.plan_span(entries)
    before = dict(src.stats)
    rx0 = _net_rx_bytes()
    n_bytes = 0
    t0 = time.monotonic()
    for e in entries:
        if one_by_one:
            src.plan_span([e])
        data = zf.read(infos[e.member_idx])
        n_bytes += len(data)
        if draft is not None and len(draft["sizes"]) < N_DRAFT and e.name.lower().endswith(
                (".jpg", ".jpeg")):
            _draft_check(data, draft)
    dt = max(time.monotonic() - t0, 1e-6)
    rx1 = _net_rx_bytes()
    after = src.stats
    fetched = after["bytes_fetched"] - before.get("bytes_fetched", 0)
    out = {"members": len(entries), "seconds": round(dt, 3),
           "mb_per_s": round(n_bytes / dt / 1e6, 2), "photos_per_s": round(len(entries) / dt, 2),
           "client_bytes": fetched, "member_bytes": n_bytes,
           "requests": after["requests"] - before.get("requests", 0)}
    if rx0 is not None and rx1 is not None and fetched:
        out["over_read_ratio"] = round((rx1 - rx0) / fetched, 3)
    return out


def _draft_check(data: bytes, draft: dict) -> None:
    """Does ``Image.draft`` shrink a JPEG the way decode() expects (long edge >= 640)?"""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data), formats=["JPEG", "MPO"]) as im:
            full = im.size
            im.draft("RGB", (640, 640))
            drafted = im.size
            im.load()
            bad = im.size != drafted or (max(full) >= 640 and max(drafted) < 640)
    except Exception:
        draft["errors"] += 1
        return
    draft["sizes"].append(1)
    if bad:
        draft["mismatches"] += 1


def _clip_bench_worker(name: str, n: int, seed: int) -> float:
    """Images/s of one process with one torch thread (top level: picklable for spawn)."""
    import numpy as np
    from PIL import Image

    from gpclean.clipmodel import get_image_embedder

    publiclog.disable_public()
    embedder = get_image_embedder(name, threads=1)
    rng = np.random.default_rng(seed)
    images = [Image.fromarray(rng.integers(0, 256, (480, 640, 3), dtype=np.uint8), "RGB")
              for _ in range(n)]
    embedder.embed(images[:2])  # warm-up: first call pays lazy initialisation
    t0 = time.monotonic()
    embedder.embed(images)
    return n / max(time.monotonic() - t0, 1e-6)


def _clip_bench(name: str) -> float:
    """Aggregate CLIP images/s over CLIP_PROCS processes x 1 thread."""
    from gpclean.clipmodel import fetch_model

    fetch_model(name)  # once, before the workers start
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(CLIP_PROCS) as pool:
        rates = pool.starmap(_clip_bench_worker,
                             [(name, CLIP_IMAGES, i) for i in range(CLIP_PROCS)])
    return round(sum(rates), 2)


def _sidecar_and_exif(meta: Path, details: dict, public: dict) -> None:
    """Sidecar key presence, pairing-rule hits and EXIF presence from a scanned shard."""
    from gpclean.schema import open_readonly
    from gpclean.takeout.sidecar import MediaRef, SidecarRef, pair

    conn = open_readonly(meta)
    try:
        items = [dict(r) for r in conn.execute("SELECT * FROM items_raw")]
        sidecars = [dict(r) for r in conn.execute("SELECT * FROM sidecars_raw")]
        videos = [dict(r) for r in conn.execute("SELECT * FROM videos_raw")]
        info = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM shard_info")}
    finally:
        conn.close()
    export = info.get("export_id") or ""
    presence = {f"sc_{k}": sum(1 for s in sidecars if s.get(k) not in (None, "", 0))
                for k in _SIDECAR_KEYS}
    public.update(presence, sidecars=len(sidecars))
    media = [MediaRef(key=("i", r["member_idx"]), export_id=export, folder=r["folder"],
                      filename=r["filename"]) for r in items]
    media += [MediaRef(key=("v", r["member_idx"]), export_id=export, folder=r["folder"],
                       filename=r["filename"]) for r in videos]
    refs = [SidecarRef(key=s["member_idx"], export_id=export, folder=s["folder"],
                       json_name=s["json_name"], title=s.get("title"), taken_ts=s.get("taken_ts"))
            for s in sidecars]
    rules = Counter(p.rule for k, p in pair(media, refs).items() if k[0] == "i")
    public.update({f"pair_{rule.lower()}": n for rule, n in sorted(rules.items())})
    public.update(
        exif_dt=sum(1 for r in items if r.get("exif_dt")),
        exif_offset=sum(1 for r in items if r.get("exif_offset")),
        exif_camera=sum(1 for r in items if r.get("has_camera_exif")),
        exif_gps=sum(1 for r in items if r.get("exif_lat") is not None),
        item_errors=sum(1 for r in items if r.get("err")),
    )
    # Private only: folder names and sample links for the manual spot check (PLAN M8).
    origins = Counter(s["origin_folder"] for s in sidecars if s.get("origin_folder"))
    details["top_origin_folders"] = origins.most_common(50)
    details["url_samples"] = [
        {"title": s.get("title"), "folder": s["folder"], "url": s["url"]}
        for s in sidecars if s.get("url")][:N_URLS]


def _zip_survey(rc, env, zips: list[dict], budget: _Budget, details: dict, public: dict,
                tmp: Path) -> None:
    """Tails, member classes, compression, range-read speed and a no-CLIP processing run."""
    from gpclean.scan import HttpOpener, plan_shards, scan_shard
    from gpclean.takeout.members import classify
    from gpclean.takeout.zipsource import ZipSource

    kinds: Counter = Counter()
    compression: Counter = Counter()
    per_zip = []
    surveyed = []
    with rc.serve_http(env.source) as base:
        for z in zips:
            if budget.left() < 30 * 60:
                break  # keep time for the read and processing tests
            url = ci.zip_url(base, z["name"])
            t0 = time.monotonic()
            try:
                tail, entries = ci.read_zip_entries(url, z["size"])
            except Exception as exc:
                ci.raise_if_quota(rc, exc)
                per_zip.append({"name": z["name"], "error": type(exc).__name__})
                continue
            classes = [(e, classify(e.name, include_albums=env.include_albums,
                                    file_size=e.file_size)) for e in entries]
            n_images = 0
            for e, mc in classes:
                kinds[mc.kind if mc.kind != "skip" else f"skip_{mc.reason}"] += 1
                if mc.kind == "image":
                    n_images += 1
                    compression[{0: "stored", 8: "deflated"}.get(e.compress_type, "other")] += 1
            per_zip.append({"name": z["name"], "size": z["size"], "entries": len(entries),
                            "images": n_images, "tail_bytes": len(tail),
                            "zip64": b"PK\x06\x06" in tail,
                            "tail_seconds": round(time.monotonic() - t0, 3)})
            surveyed.append((z, url, tail, classes, n_images))
        details["zips"] = per_zip
        public.update({f"m_{k}"[:32]: n for k, n in sorted(kinds.items())})
        public.update({f"comp_{k}": n for k, n in sorted(compression.items())})
        public["zips_surveyed"] = len(surveyed)
        public["zip64_zips"] = sum(1 for p in per_zip if p.get("zip64"))
        if not surveyed:
            return

        # The read tests use the zip with the most photos.
        z, url, tail, classes, _ = max(surveyed, key=lambda t: t[4])
        images = [e for e, mc in classes if mc.kind == "image"]
        draft = {"sizes": [], "mismatches": 0, "errors": 0}
        if budget.left() > 20 * 60 and images:
            with ZipSource.open_http(url, z["size"], tail) as src:
                seq = _read_members(src, images[:N_READ], one_by_one=False, draft=draft)
                details["read_sequential"] = seq
                public.update(seq_mb_s=seq["mb_per_s"], seq_photos_s=seq["photos_per_s"],
                              seq_requests=seq["requests"])
                if "over_read_ratio" in seq:
                    public["seq_over_read"] = seq["over_read_ratio"]
            sample = random.Random(0).sample(images, min(N_READ, len(images)))
            with ZipSource.open_http(url, z["size"], tail) as src:
                rnd = _read_members(src, sample, one_by_one=True)
                details["read_random"] = rnd
                public.update(rnd_mb_s=rnd["mb_per_s"], rnd_photos_s=rnd["photos_per_s"],
                              rnd_requests=rnd["requests"])
                if "over_read_ratio" in rnd:
                    public["rnd_over_read"] = rnd["over_read_ratio"]
            public.update(draft_checked=len(draft["sizes"]), draft_mismatch=draft["mismatches"],
                          draft_errors=draft["errors"])
        details["requests_per_zip_read"] = public.get("seq_requests")

        if budget.left() > 15 * 60:
            from gpclean.config import ScanConfig

            cfg = ScanConfig(include_albums=env.include_albums, clip_model="none",
                             photos_per_shard=N_PROCESS)
            entries = [e for e, _ in classes]
            spec = plan_shards(entries, cfg, zipkey=z["zipkey"], zip_name=z["name"])[0]
            meta, pack = tmp / "probe.meta.sqlite", tmp / "probe.pack.sqlite"
            t0 = time.monotonic()
            info = scan_shard(HttpOpener(url, z["size"], tail), spec, entries, cfg,
                              meta_path=meta, pack_path=pack, workers=os.cpu_count() or 1,
                              embedder_name="none",
                              deadline=time.monotonic() + max(60.0, budget.left() - 10 * 60))
            dt = max(time.monotonic() - t0, 1e-6)
            public.update(proc_photos=int(info["n_items"]),
                          proc_photos_s=round(int(info["n_items"]) / dt, 2),
                          proc_pack_kb_per_photo=round(
                              int(info["pack_size"]) / max(1, int(info["n_items"])) / 1024, 1))
            details["process"] = {k: info[k] for k in ("n_items", "n_err", "n_sidecars",
                                                       "n_videos", "bytes_read", "pack_size")}
            _sidecar_and_exif(meta, details, public)


def _probe(env: ci.CiEnv) -> int:
    budget = _Budget(BUDGET_S)
    rc = ci.make_rclone(env)
    store = ci.make_store(env, rc)
    details: dict = {"run": env.run_tag, "folder": env.folder, "sections": {}}
    public: dict = {}
    failed = 0

    # Auth first, and fail closed: nothing else runs with a wrongly scoped token.
    if not ci.scope_check(env, rc):
        publiclog.event(Ev.FAILED, phase=PHASE, scope_ok=0)
        return ci.EXIT_FAIL
    public["scope_ok"] = True

    def section(name: str, fn, min_left: float = 60.0) -> None:
        nonlocal failed
        if budget.left() < min_left:
            details["sections"][name] = "skipped"
            return
        t0 = time.monotonic()
        try:
            fn()
            details["sections"][name] = round(time.monotonic() - t0, 2)
        except QuotaError:
            raise
        except Exception as exc:
            failed += 1
            details["sections"][name] = "error:" + type(exc).__name__
            log.warning("probe section %s failed", name, exc_info=True)

    def system() -> None:
        info = _system()
        details["system"] = info
        public.update(info)

    rel_dir = f"probe/{env.run_id}"

    def write_readback() -> None:
        store.mkdirs([rel_dir])
        with tempfile.TemporaryDirectory(prefix="gpclean-probe-") as tmp:
            src, back = Path(tmp) / "a", Path(tmp) / "b"
            src.write_bytes(os.urandom(1024))
            store.put(src, f"{rel_dir}/rw-test.bin")
            store.get(f"{rel_dir}/rw-test.bin", back)
            public["rw_ok"] = back.read_bytes() == src.read_bytes()

    zips: list[dict] = []

    def listing() -> None:
        kept, dup_names = ci.split_duplicates(ci.list_zip_files(rc, env.source))
        zips.extend(kept)
        public["dup_names"] = dup_names  # the plan would skip these (see ci.split_duplicates)
        from gpclean.takeout.names import export_id

        for z in zips:
            z["export_id"] = export_id(z["name"])
        details["n_exports"] = len({z["export_id"] for z in zips})
        public.update(n_zips=len(zips), total_gb=round(sum(z["size"] for z in zips) / 1e9, 3),
                      n_exports=details["n_exports"])

    section("system", system)
    section("write_readback", write_readback)
    section("listing", listing)
    with tempfile.TemporaryDirectory(prefix="gpclean-probe-") as tmp:
        section("zips", lambda: _zip_survey(rc, env, zips, budget, details, public, Path(tmp)),
                min_left=25 * 60)
    if env.clip_model != "none":
        for name in ("b32", "b16"):
            section(f"clip_{name}",
                    lambda name=name: public.__setitem__(f"clip_{name}_img_s", _clip_bench(name)),
                    min_left=8 * 60)

    details["public"] = public
    details["failed_sections"] = failed
    urls = details.pop("url_samples", [])
    with tempfile.TemporaryDirectory(prefix="gpclean-probe-") as tmp:
        store.mkdirs([rel_dir])
        pj = Path(tmp) / "probe.json"
        pj.write_text(json.dumps(details, indent=1, sort_keys=True, default=str) + "\n",
                      encoding="utf-8")
        store.put(pj, f"{rel_dir}/probe.json")
        ut = Path(tmp) / "urls-sample.txt"
        ut.write_text("".join(f"{u['url']}\t{u['folder']}\t{u['title']}\n" for u in urls),
                      encoding="utf-8")
        store.put(ut, f"{rel_dir}/urls-sample.txt")

    # Public: aggregates only, a few keys per line.
    items = [(k, v) for k, v in sorted(public.items())
             if type(v) in (int, float, bool)]
    for i in range(0, len(items), 8):
        publiclog.event(Ev.COUNT, phase=PHASE, **dict(items[i:i + 8]))
    publiclog.event(Ev.PHASE_DONE, phase=PHASE, failed_sections=failed)
    return ci.EXIT_OK


def cli_probe() -> int:
    """``gpclean probe``: measure; aggregates to the public log, details to Drive."""
    return ci.run_step(PHASE, _probe)
