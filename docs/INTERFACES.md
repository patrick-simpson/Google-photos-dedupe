# gpclean module interfaces (implementation contract)

`docs/PLAN.md` explains *why*. This file fixes *what each module exposes*, so modules can be
built in parallel and still fit together. Where this file and PLAN.md disagree, this file wins
for signatures and PLAN.md wins for behaviour. SQLite schemas live in `src/gpclean/schema.py`,
config in `src/gpclean/config.py`, version constants in `src/gpclean/version.py`, and the CLI
surface in `src/gpclean/cli.py`. Those four files are the frozen foundation: do not change them
without coordinating (tell the orchestrator what you need instead).

## Global rules (every module)
- Python 3.12, standard library + pinned deps only (`pillow`, `pillow-heif`, `numpy`, `tzdata`;
  `torch`/`open_clip` only inside `gpclean.clipmodel`; `mcp` only inside `gpclean.mcp_server`).
  No opencv, scipy, imagehash, requests, flask. Do not edit `pyproject.toml` / `uv.lock`.
- **Never print personal data.** Library code uses `logging.getLogger(__name__)` only (which
  goes to the private log in CI). User-facing CLI output for *local* commands may print normally.
  Only `gpclean.publiclog` writes to the CI console, and only numbers.
- Never pickle, never `np.load(allow_pickle=True)`, never `eval`. Never extract zip members to
  disk by their names. Never use a member name as a filesystem path.
- Paths: use `pathlib`. Must work on Windows and Linux. Text files UTF-8.
- Every public function has a docstring. Keep code simple and readable; comment the *why*.
- Tests go in `tests/test_<module>.py`, use `pytest`, synthetic data only (no network except
  tests marked `@pytest.mark.clip` / `@pytest.mark.rclone`, which skip when unavailable).
- Run tests with `uv run pytest tests/test_<module>.py -q`. Do not run `uv sync`/`uv add`.

## Takeout member layout
Zip members look like `Takeout/Google Photos/<folder>/<filename>`. `<folder>` is either a year
folder (`Photos from 2019`) or an album name. Anything else (other products, top-level JSON) is
skipped.

## `gpclean.takeout.names`
```python
PHOTO_EXTS: frozenset[str]        # {"jpg","jpeg","png","heic","heif","webp","gif"}
VIDEO_EXTS: frozenset[str]        # mp4 mov 3gp m4v mkv avi webm mts m2ts wmv mpg mpeg mp
RAW_EXTS: frozenset[str]          # dng cr2 cr3 nef arw rw2 orf raf srw pef
OTHER_IMAGE_EXTS: frozenset[str]  # bmp tif tiff avif jxl
NON_SIDECAR_JSON: frozenset[str]  # metadata.json print-subscriptions.json shared_album_comments.json
                                  # user-generated-memory-titles.json (casefolded basenames)

@dataclass(frozen=True)
class MediaName:
    base: str          # name without dup index and without extension
    dup: int | None    # N from a trailing "(N)" before the extension
    ext: str           # extension including the dot, original case ("" if none)
    @property
    def plain(self) -> str: ...   # base + ext   (the name without "(N)")

def parse_media_name(filename: str) -> MediaName
def is_edited(filename: str) -> bool          # "-edited" suffix incl. "(N)" and truncated forms
def ext_of(filename: str) -> str               # lowercase extension without dot, "" if none
def export_id(zip_name: str) -> str            # "takeout-20260901T120000Z-001.zip" -> "takeout-20260901T120000Z";
                                               # other names -> stem; "" never returned
def year_of_folder(folder: str) -> int | None  # "Photos from 2019" -> 2019
def messaging_app(filename: str) -> str | None # "whatsapp" | "messenger" | "facebook" | "signal"
                                               # | "telegram" | "snapchat" | "reddit" | None
def is_screenshot_name(filename: str) -> bool
def burst_key(filename: str) -> tuple[str, bool] | None
    # ("<shared prefix>", is_cover) for *_BURSTnnn[_COVER].* and PXL_*.RAW-nn.* names
```

## `gpclean.takeout.members`
```python
@dataclass(frozen=True)
class Entry:                   # one central-directory record
    member_idx: int            # index in ZipFile.infolist()
    name: str
    header_offset: int
    compress_size: int
    file_size: int
    crc: int
    compress_type: int
    is_dir: bool

@dataclass(frozen=True)
class MemberClass:
    kind: str                  # "image" | "sidecar" | "video" | "skip"
    folder: str | None         # folder directly under "Google Photos/"
    folder_kind: str | None    # "year" | "album"
    filename: str | None
    reason: str | None         # for kind == "skip": see skipped_raw.reason in schema.py

def list_entries(zf: zipfile.ZipFile) -> list[Entry]          # sorted by header_offset
def classify(name: str, *, include_albums: bool, file_size: int = 0,
             max_member_bytes: int = 200 * 1024 * 1024) -> MemberClass
```
Rules: year folder `^Takeout/Google Photos/Photos from (\d{4})/[^/]+$`; album = any other direct
subfolder of `Google Photos/`. Folders named `Trash`/`Bin` (casefold) -> skip `trash`.
Images (PHOTO_EXTS) in year folders -> `image`; in albums -> `image` only if `include_albums`,
else skip `album_media`. `-edited` -> skip `edited` (checked before image). RAW -> skip `raw`.
OTHER_IMAGE_EXTS -> skip `other_image`. Video in year folder -> `video`; in album -> skip
`album_video`. `*.json` not in NON_SIDECAR_JSON (year or album folder) -> `sidecar`. Images larger
than `max_member_bytes` -> skip `too_large`.

## `gpclean.takeout.sidecar`
```python
def parse_sidecar(data: bytes) -> dict
    # Returns the sidecars_raw columns (except member_idx/member/folder/folder_kind/json_name):
    # title, taken_ts, creation_ts, lat, lon, alt, lat_exif, lon_exif, url, description,
    # people (JSON array string or None), origin_folder, device_type, from_shared_album,
    # from_partner, favorited, archived, trashed, raw (<=64 KiB), err.
    # Never raises: bad JSON -> {"err": "<ExceptionClass>", everything else None}.
    # geo 0.0/0.0 means "no location" -> None. url is kept only if it passes validate_photos_url.

def validate_photos_url(url: str | None) -> str | None
    # accept only https://photos.google.com/photo/<id> (id [A-Za-z0-9_-]{10,200}, optional
    # trailing slash / query dropped); return the REBUILT canonical url or None.

@dataclass(frozen=True)
class MediaRef:
    key: object                 # opaque caller key
    export_id: str
    folder: str
    filename: str
    exif_ts: int | None = None  # UTC-ish epoch from EXIF DateTimeOriginal (for confirmation)

@dataclass(frozen=True)
class SidecarRef:
    key: object
    export_id: str
    folder: str
    json_name: str
    title: str | None
    taken_ts: int | None

@dataclass(frozen=True)
class Pairing:
    sidecar_key: object | None
    rule: str                   # "R1".."R6" or "none"
    conf: str                   # "high" | "low" | "none"

def pair(media: list[MediaRef], sidecars: list[SidecarRef]) -> dict[object, Pairing]
    # Every media key appears in the result. Pairing is done per (export_id, folder).
    # Rules in order R1..R6; each rule assigns only pairs unique on BOTH sides among still
    # unclaimed items. Confirmation: title must agree (with (n) handling / casefold / truncated
    # title prefix) and, when both present, |taken_ts - exif_ts| <= 86400 + 14*3600 (tz slack).
    # Confirmed -> conf "high"; R3/R6 matches confirmed only by title prefix -> "low";
    # unconfirmed or ambiguous -> no pairing (rule "none").
```

## `gpclean.takeout.rangefile` / `gpclean.takeout.zipsource`
```python
class HTTPRangeFile(io.RawIOBase):
    def __init__(self, url: str, size: int, *, timeout: float = 60.0, retries: int = 5,
                 read_through: int = 256 * 1024): ...
    def hint(self, start: int, end: int) -> None     # next GET covers [start, end)
    def skip_to(self, offset: int) -> None           # force reopen at offset (gap holds skipped member)
    bytes_fetched: int; bytes_discarded: int; requests: int
    # readable, seekable; seek(current) is a no-op; forward gaps < read_through are read and
    # discarded (counted), larger gaps or backward seeks reopen with Range: bytes=a-b.
    # Retries with exponential backoff reopening at the current offset.

class ZipSource:
    """A seekable read-only file object for zipfile.ZipFile.

    Serves [cd_offset, size) (EOCD + central directory) from an in-memory tail blob, and the
    rest from a backing file (HTTPRangeFile or a local file)."""
    @classmethod
    def open_local(cls, path: str | Path) -> "ZipSource"
    @classmethod
    def open_http(cls, url: str, size: int, tail: bytes | None = None) -> "ZipSource"
    def tail(self) -> bytes                           # blob to hand to worker processes
    def zipfile(self) -> zipfile.ZipFile               # wraps self in io.BufferedReader(1 MiB)
    def plan_span(self, entries: list[Entry]) -> None  # hint the backing reader for a task
    def skip_gap(self, offset: int) -> None
    stats -> dict  # bytes_fetched, bytes_discarded, requests

def read_tail(fobj, size: int) -> bytes   # EOCD (+ZIP64 locator/record) + central directory
```
Local runs use `ZipSource.open_local(path)` — identical code path above the file object.

## `gpclean.imaging`
```python
# decode.py
@dataclass
class Decoded:
    work: PIL.Image.Image      # RGB, sRGB, orientation applied, long edge <= work_edge (640)
    width: int; height: int    # full-res dims after orientation
    stored_w: int; stored_h: int
    orientation: int
    format: str
    animated: bool
    exif: dict                 # keys: dt, offset, subsec, dt_any, make, model, has_camera_exif,
                               #       lat, lon  (missing -> None)
def configure_pillow(max_pixels: int = 250_000_000) -> None   # register pillow-heif, limits
def decode(data: bytes, *, work_edge: int = 640) -> Decoded   # raises on undecodable input
    # Image.open(BytesIO(data), formats=["JPEG","MPO","PNG","WEBP","GIF","HEIF"]);
    # JPEG/MPO: draft("RGB", (work_edge, work_edge)); HEIF: no exif_transpose (already applied);
    # ICC -> sRGB via ImageCms; alpha flattened on white; P/LA/I;16/CMYK -> RGB; frame 0.

# fingerprint.py
def sha256(data: bytes) -> bytes
def phash64(work: Image) -> int                 # signed int64; 32x32 luma, DCT-II, 8x8 incl DC, > median
def signature(work: Image) -> bytes              # 1152 bytes, SIG_VERSION 1
def hamming(a: int, b: int) -> int
def sig_distance(a: bytes, b: bytes) -> tuple[float, float, float]   # (mad, block_max, chroma_max)
def sig_verify(a: bytes, b: bytes, cfg: MergeConfig) -> bool
def sig_luma_std(sig: bytes) -> float

# features.py
def features(work: Image) -> dict   # lap_var, luma_mean, luma_std, luma_p02, luma_p98,
                                    # frac_dark (<20), frac_bright (>245), flat_frac, colorfulness
# thumbs.py
def thumbs(work: Image, cfg: ScanConfig) -> tuple[bytes, bytes]   # (grid webp, preview webp)

# __init__.py
def process_image(data: bytes, cfg: ScanConfig) -> tuple[dict, bytes, bytes, Image]
    # -> (items_raw columns except member fields & emb, grid, preview, work image for CLIP)
```

## `gpclean.clipmodel`
```python
MODELS: dict[str, ModelSpec]    # "b32", "b16": open_clip name, pretrained tag, hf repo, revision,
                                # filename, sha256, dim
def cli_fetch_model(model: str) -> int
def fetch_model(name: str) -> Path              # download to HF cache if needed; verify sha256
class ImageEmbedder:  def __init__(self, name: str); def embed(self, images: list[Image]) -> np.ndarray  # (n, dim) f16 L2-normed
class TextEmbedder:   def __init__(self, name: str); def embed(self, query: str) -> np.ndarray      # (dim,) f32 L2-normed
class StubEmbedder:   # deterministic, torch-free; same API for both; used by tests and --no-clip-like paths
def get_image_embedder(name: str)   # "none" -> None
```
Weights are pinned by HF commit + sha256; runtime sets `HF_HUB_OFFLINE=1` after fetching.

## `gpclean.scan`
```python
@dataclass(frozen=True)
class ShardSpec:
    zipkey: str; zip_name: str; export_id: str; shard: int
    start: int; end: int            # slice [start, end) of the offset-sorted entry list
    n_images: int

def zipkey_for(drive_id: str, size: int, modtime: str) -> str       # sha256(...)[:12]
def zipkey_local(path: Path) -> str                                  # from name+size+mtime
def plan_shards(entries: list[Entry], cfg: ScanConfig, *, zipkey: str, zip_name: str) -> list[ShardSpec]
    # contiguous ranges over ALL entries (so every entry lands in exactly one shard), each
    # holding about cfg.photos_per_shard image members. Deterministic.
def scan_shard(opener, spec: ShardSpec, entries: list[Entry], cfg: ScanConfig, *,
               meta_path: Path, pack_path: Path, workers: int,
               embedder_name: str) -> dict
    # opener: picklable callable returning a ZipSource (local path or http url+tail).
    # Writes meta (SHARD_DDL) + pack (PACK_DDL) via create/finalize_transport_db.
    # Returns shard_info dict. Spawn pool; per-worker initializer; tasks = runs of <=48 wanted
    # consecutive members; per-item try/except storing err class name.
def cli_run_local(zips, out, include_albums, threshold, clip_model, no_clip, workers,
                  photos_per_shard) -> int
    # scan every *.zip under `zips` into out/work/... and out/thumbs/..., then merge into
    # `out` (a complete bundle directory). Resumable: finished shards are skipped.
```

## `gpclean.merge`
```python
# merge/load.py     : load_shards(meta_paths) -> Loaded (pandas-free, plain lists/np arrays)
# merge/pairing.py  : attach sidecars to items using takeout.sidecar.pair
# merge/collapse.py : url collapse + (sha, filename, date) collapse across exports; aliases
# merge/localtime.py: local_date/time per PLAN §5; tz via zoneinfo
# merge/group.py    : exact + pigeonhole near-dup + edge checks + anti-chaining; brute_force_groups oracle
# merge/bursts.py   : filename + time bursts, best shot
# merge/scores.py   : junk scores
# merge/bundle.py   :
def build_bundle(meta_paths: list[Path], pack_dir: Path | None, out_dir: Path,
                 mcfg: MergeConfig, *, cfg_hash: str, clip_model: str,
                 expected_shards: int | None = None, pack_hashes: dict | None = None) -> dict
    # writes out_dir/index.sqlite, out_dir/embeddings.f16.npy, out_dir/manifest.json (last).
def regroup(bundle: Path, threshold: int) -> dict     # rewrite dup_groups/bursts/scores in place
def cli_regroup(bundle, threshold) -> int
```
Bundle directory layout: `index.sqlite`, `embeddings.f16.npy`, `manifest.json`,
`thumbs/<zipkey>-<shard:04d>.sqlite`. `manifest.json`:
`{"index_schema", "cfg", "extract_version", "code_version", "created_at", "partial": bool,
"missing_shards": int, "clip_model", "counts": {...}, "files": [{"path", "size", "sha256"}]}`.

## `gpclean.bundle_read`
```python
class Bundle:
    def __init__(self, path: Path)                  # opens index read-only+immutable; lazy packs
    meta: dict; manifest: dict
    def item(self, item_id: int) -> dict | None
    def items(self, ids: list[int]) -> list[dict]
    def by_uid(self, uid: str) -> dict | None
    def uids_to_ids(self, uids: list[str]) -> dict[str, int]
    def thumb(self, item_id: int, size: str) -> bytes | None    # size "g" | "p" (webp)
    def embeddings(self) -> np.ndarray | None                    # (N, dim) f16, mmap
    def scores_for(self, item_id: int) -> dict[str, tuple[float, str]]
    def dup_groups(self, *, kind: str | None = None, offset=0, limit=50) -> list[dict]  # with members
    def dup_group_of(self, item_id: int) -> dict | None
    def bursts(self, *, offset=0, limit=50) -> list[dict]
    def burst_of(self, item_id: int) -> dict | None
    def junk(self, category: str, *, min_score: float, year: int | None, offset: int, limit: int) -> tuple[list[dict], int]
    def videos_by_day(self) -> dict[str, int]
    def stats(self) -> dict
    def close(self) -> None
```

## `gpclean.review_db`
```python
class ReviewDB:
    def __init__(self, path: Path)
    def rev(self) -> int
    def propose(self, items: list[tuple[str, str, str | None]], *, proposer: str, batch_id: str) -> dict
        # items: (item_uid, reason, category). Inserts status 'proposed' only. Never touches
        # existing approved/rejected rows; never re-proposes a rejected uid. Enforces
        # MAX_OPEN_PROPOSALS=2000. Returns {"added", "already", "rejected_skipped", "capped"}.
    def withdraw(self, uids: list[str], *, proposer: str) -> int   # only own 'proposed' rows
    def user_add(self, items: list[tuple[str, str, str | None]]) -> int   # status 'approved', proposed_by 'user'
    def decide(self, uids: list[str], decision: str) -> int       # approve | reject | reset(->proposed or delete if user)
    def reject_batch(self, batch_id: str) -> int
    def mark_deleted(self, uids: list[str], deleted: bool) -> int
    def list(self, status: str, *, offset=0, limit=100) -> list[dict]   # status proposed|approved|rejected|deleted|all
    def get(self, uids: list[str]) -> dict[str, dict]
    def counts(self) -> dict
    def set_group_decision(self, group_key: str, action: str, keeper_uid: str | None) -> None
    def group_decisions(self) -> dict[str, dict]
```
Writes use `BEGIN IMMEDIATE`. Every write appends to `events`. Reasons are sanitised
(control/bidi chars stripped, 3..300 chars).

## `gpclean.search`
```python
@dataclass
class SearchParams:  query, category, min_score, date_from, date_to, year, filename_contains,
                     origin_contains, group ("dup:<id>"|"burst:<id>"), has_gps, exclude_queued,
                     sort ("score"|"date"|"similarity"), limit, offset
def search(bundle: Bundle, review: ReviewDB | None, p: SearchParams, text_embedder=None) -> dict
    # {"total": int, "offset": int, "next_offset": int | None, "rows": [item dicts + flags + sim]}
```

## `gpclean.sheet`
```python
def render_sheet(bundle: Bundle, ids: list[int], *, detail: str = "standard",
                 queued: set[int] = frozenset(), keepers: set[int] = frozenset()) -> tuple[bytes, list[str]]
    # JPEG bytes + legend lines "n=<cell> id=<id> <date> <filename> <flags>"
    # standard: 8 cols, 154 px cells, <= 1232x924 ; high: <= 20 cells ~300 px from previews, same canvas
```

## `gpclean.mcp_server`, `gpclean.site.server`, `gpclean.localinit`
```python
# mcp_server.py
def build_server(home: Path) -> "FastMCP"
def cli_mcp(home) -> int
# site/server.py
def make_server(home: Path, port: int = 8765) -> ThreadingHTTPServer   # for tests (port 0 ok)
def cli_serve(home, port, no_browser) -> int
# localinit.py
def home_paths(home: Path) -> dict      # state_dir, review_db, config_json, review_dir, bundle
def current_bundle(home: Path) -> Path  # from state/config.json
def cli_init(home, bundle) -> int       # writes state/config.json; creates review/.claude/settings.json
def cli_mcp_config(home) -> int
def cli_verify_bundle(bundle) -> int
```

## `gpclean.publiclog`
```python
class Ev(enum.Enum): PHASE_START, PROGRESS, PHASE_DONE, CHECKPOINT, COUNT, FAILED, OK
def event(ev: Ev, **values: int | float | bool) -> None   # raises TypeError on any other type
def setup_logging(private_dir: Path | None) -> None        # root logger -> private file (CI) or stderr
def install_excepthook(phase: int = 0) -> None              # prints only "FAILED phase=<n> exc=<Type>"
def github_output(**values: int | bool | str) -> None      # str values must match ^[\w\[\],.:-]{0,4000}$ (ints/JSON int lists)
```
Public writes go to fd 3 only when `GPCLEAN_PUBLIC_FD=3` and fd 3 is open; otherwise stderr.

## `gpclean.rclone` and `gpclean.store`
```python
ALLOWED_VERBS = {"lsjson", "lsf", "cat", "copyto", "copy", "mkdir", "about", "serve", "version"}
DENIED_VERBS = {"sync", "move", "moveto", "delete", "deletefile", "purge", "rmdir", "rmdirs",
                "link", "backend", "config", "dedupe", "cleanup", "bisync", "touch"}
class Rclone:
    def __init__(self, config: Path | None = None, binary: str = "rclone", log_file: Path | None = None)
    def lsjson(self, path: str, *, recursive=False, files_only=False, hash=False) -> list[dict]
    def copyto(self, src: str, dst: str) -> None     # dst must be under the output root
    def copy(self, src: str, dst: str) -> None
    def mkdir(self, path: str) -> None
    def cat(self, path: str, *, offset: int = 0, count: int | None = None) -> bytes
    def serve_http(self, path: str) -> ContextManager[str]   # yields base url on 127.0.0.1
    def access_token(self, remote: str) -> str      # read from (refreshed) config for scope check
class LocalStore / RcloneStore:      # rel paths under an output root
    exists(rel) -> bool; list(rel_dir) -> list[str]; get(rel, local) ; put(local, rel); mkdirs(rels)
```

## `gpclean.ci` / `gpclean.probe`
Inputs from env: `GPCLEAN_FOLDER`, `GPCLEAN_THRESHOLD`, `GPCLEAN_INCLUDE_ALBUMS`, `GPCLEAN_MODE`,
`GPCLEAN_CLIP_MODEL`, `GPCLEAN_WORKERS`, `GPCLEAN_RUN_ID`, `GPCLEAN_ATTEMPT`,
`GPCLEAN_OUT_ROOT` (default `gpclean-output`), `GPCLEAN_REMOTE` (default `gp`),
`RCLONE_CONFIG`, `GPCLEAN_PRIVATE_DIR`, `GPCLEAN_PUBLIC_FD`, `GPCLEAN_SELFTEST_FAIL_SHARD`.
Outputs via `publiclog.github_output` (integers / JSON int lists only).

## Fixtures (`gpclean.fixtures.generate`)
```python
def generate(out: Path, *, seed: int = 0, small: bool = False) -> dict   # writes zips + expected.json
def cli_generate(out, seed, small) -> int
```
Writes `out/takeout-20260101T000000Z-001.zip`, `-002.zip` (export A) and
`out/takeout-20260201T000000Z-001.zip` (export B), plus `out/expected.json`:
```json
{
  "item_keys": {"<zip>::<member>": "<item_key>"},       // every image member -> library item key
  "dup_groups": [{"id": "P1", "members": ["<item_key>", ...], "keeper": "<item_key>"}],
  "must_not_group": [["<item_key>", "<item_key>"], ...],
  "collapsed": [{"id": "C1", "item_key": "<item_key>", "copies": ["<zip>::<member>", ...]}],
  "pairings": {"<zip>::<media member>": "<zip>::<json member>" | null},
  "bursts": [{"id": "N4", "members": ["<item_key>", ...], "best": "<item_key>"}],
  "junk": {"<item_key>": ["screenshot", "messaging", ...]},
  "videos": {"members": ["<zip>::<member>", ...], "by_day": {"YYYY-MM-DD": n}},
  "skipped": {"<zip>::<member>": "<reason>"},
  "n_items": 0
}
```
`item_key` = the fake url id (`AF1QipFAKE...`) when the item has a sidecar with a url, else
`nourl:<filename>`. An item's key equals its bundle `item_uid` minus the `g:` prefix when a url
exists.

## Addenda from wave 1 (implemented; binding for later modules)
**takeout.names / sidecar**
- A `(n)` preceded by a space (`Photo (1).jpg`, `... AM (1).jpeg`) is NOT a Takeout dup index.
- `pair()`: R1/R2/R5 may pair a title-less sidecar with conf `low`; R3/R4/R6 require a title.
  A prefix-only title match needs both timestamps and an exact quarter-hour offset and must not
  name another present media file. **Callers must include video media in `pair()`** (so a video
  can claim its own sidecar). `parse_sidecar` caps text fields and drops timestamps outside ±1e12;
  timestamp 0 -> None; `from_shared_album`/`from_partner` = 1 when the key exists under
  `googlePhotosOrigin`; missing booleans -> 0.

**takeout.rangefile / zipsource**
- `ZipSource.zipfile(*, fresh=False)` returns a shared ZipFile; call once per worker per zip and
  never close it per task. `ZipSource.broken` / `HTTPRangeFile.broken`; 408/429/5xx retried.
- Permanent read failures raise `RangeReadError(OSError)` with `.retryable`. Scan: catch
  `(OSError, zipfile.BadZipFile, zlib.error, DecompressionBombError)` per item; if a
  `RangeReadError` is retryable or the source is broken, **abort the shard** (resumable) instead
  of recording item errors. `plan_span` splits non-consecutive entries itself and never reads
  into an unrequested member. Extra kwargs: `HTTPRangeFile(..., backoff=1.0, sleep=time.sleep)`,
  `ZipSource.open_http(url, size, tail=None, *, timeout=60.0, retries=5, backoff=1.0)`.

**imaging**
- `decode` raises `OSError` (incl. re-raised Pillow/libheif errors) or `DecompressionBombError`.
  `process_image` calls `configure_pillow(cfg.max_pixels)` (idempotent). MPO opens via JPEG.
- `sig_verify` alone does NOT separate same-template chat screenshots: the merge MUST keep the
  `is_graphic` exclusion (flat_frac >= 0.35 or screenshot score >= 0.8 or sig luma std < 8).

**clipmodel**
- Pins: b32 `laion/CLIP-ViT-B-32-DataComp.XL-s13B-b90K@f0e2ffa0…` `open_clip_model.safetensors`;
  b16 `laion/CLIP-ViT-B-16-DataComp.XL-s13B-b90K@d110532e…` `open_clip_pytorch_model.bin`
  (loaded with `weights_only=True`). Cache: `$HF_HOME/gpclean/...`.
- The scan parent must call `fetch_model(name)` once BEFORE starting the pool; workers only load.
- `get_image_embedder(name, *, threads=None)`; `StubEmbedder(dim).embed(str | list[Image])`.

**publiclog**
- Import early (it removes `GPCLEAN_PUBLIC_FD` from `os.environ` at import). Lines look like
  `PROGRESS phase=2 done=10`. Helpers: `set_phase(n)`, `disable_public()` (call it in scan worker
  initializers), `setup_logging()` returns the private log path (`gpclean.log`).
  `github_output` accepts ints, bools, restricted strings and lists of ints (compact JSON).

**rclone / store**
- `Rclone(config=None, binary=..., log_file=None, *, out_root="gp:gpclean-output")` (defaults from
  `GPCLEAN_REMOTE`/`GPCLEAN_OUT_ROOT`/`GPCLEAN_PRIVATE_DIR`); extra: `stat()`, `lsf()`,
  `cat_to_file()`, `about()`, `version()`, `is_under_out_root()`, `lsjson(..., max_depth=)`,
  `access_token(remote, refresh=False)`, `serve_quota_hit()`. Errors: `RcloneError(.returncode)`,
  `QuotaError(RcloneError)`, `RcloneDenied(PermissionError)`. `serve_http` yields a base URL
  without trailing slash. Binary comes from `GPCLEAN_RCLONE` or PATH: **no file under src/ other
  than rclone.py may contain the literal word for the binary in code**.
- `RcloneStore.get` downloads via `cat`; `list(rel_dir)` is recursive, sorted POSIX paths
  relative to `rel_dir`; missing dir -> [].

**review_db / bundle_read / search**
- Proposer must match `^claude(:[A-Za-z0-9._-]{1,64})?$` (MCP passes e.g. `claude:code`).
  batch_id `^[A-Za-z0-9._:-]{1,80}$`; category None or `^[a-z_]{1,32}$`.
- `user_add` upgrades proposed/rejected rows to approved (user wins). `decide()` skips rows
  marked deleted. `set_group_decision(..., action='clear')` removes a decision.
- Helpers: `review_db.strip_unsafe()`, `sanitise_reason()`; `Bundle.fetchall()`, `count()`,
  `scores_many()`, `memberships()`, `dup_group(id)`, `burst(id)`, `n_dup_groups()`, `n_bursts()`;
  `ReviewDB.uids()`, `batches()`, `events()`, `close()`. `search()` raises
  `SearchUnavailable(ValueError)` for a text query without embedder/embeddings.
- `tests/bundle_factory.py` (`make_bundle`, `plan_items`, `StubTextEmbedder`) is the shared test
  helper for site / MCP / sheet tests (`from bundle_factory import ...`).

**fixtures**
- `expected.json` describes an `include_albums=true` run (top-level `"include_albums": true`);
  `gpclean.fixtures.generate.expected_view(expected, include_albums=False)` derives the truth for
  a default run. Collapse happens before grouping. `junk` is exhaustive only for screenshot,
  messaging, tiny, dup_extra, burst_extra.

**check_repo rules**
- No line may start with `type = drive`; no refresh-token JSON; emails only @example.com /
  users.noreply.github.com / anthropic.com; workflows: only pinned `actions/checkout` or local
  reusable workflows, top-level `permissions: {}`, `persist-credentials: false`, no
  `secrets: inherit`, no `: write` permissions, no `${{ }}` inside `run:` except bare
  `${{ matrix.x }}`; `pull_request` only in ci.yml; no denied rclone verbs in code or scripts.
