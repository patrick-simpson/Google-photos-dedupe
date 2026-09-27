"""SQLite schemas: the contract between scan -> merge -> bundle -> review site / MCP server.

Files that travel through Google Drive (shard meta, thumb packs, bundle index) are written with
journal_mode=DELETE, VACUUMed and closed before upload. The local review DB uses WAL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------------------------
# Shard meta file: work/<cfg>/<zipkey>/<shard:04d>.meta.sqlite  (one per shard; the checkpoint)
# ---------------------------------------------------------------------------------------------
SHARD_DDL = """
CREATE TABLE shard_info (key TEXT PRIMARY KEY, value TEXT);
-- keys: zipkey, zip_name, export_id, shard, cfg, extract_version, code_version, start, end,
--       n_items, n_err, n_sidecars, n_videos, n_skipped, bytes_read, bytes_discarded,
--       pack_name, pack_sha256, pack_md5, pack_size, started, finished, clip_model

CREATE TABLE items_raw (
  member_idx INTEGER PRIMARY KEY,   -- index into ZipFile.infolist() (central-directory order)
  member TEXT NOT NULL,             -- full path inside the zip
  folder TEXT NOT NULL,             -- directory directly under "Google Photos/"
  folder_kind TEXT NOT NULL,        -- 'year' | 'album'
  filename TEXT NOT NULL,           -- basename
  ext TEXT NOT NULL,                -- lowercase, no dot
  format TEXT,                      -- PIL format name (JPEG, PNG, HEIF, ...)
  file_size INTEGER NOT NULL,       -- uncompressed bytes
  crc32 INTEGER,
  sha256 BLOB,                      -- 32 raw bytes
  width INTEGER, height INTEGER,    -- full-resolution dims AFTER orientation is applied
  stored_w INTEGER, stored_h INTEGER,
  orientation INTEGER,              -- EXIF orientation 1..8 (1 for HEIF, already applied)
  phash64 INTEGER,                  -- signed int64
  sig BLOB,                         -- SIG_VERSION 1: 1024 B 32x32 luma + 64 B Cb + 64 B Cr
  exif_dt TEXT,                     -- DateTimeOriginal raw 'YYYY:MM:DD HH:MM:SS'
  exif_offset TEXT,                 -- OffsetTimeOriginal e.g. '-04:00'
  exif_subsec TEXT,                 -- SubSecTimeOriginal
  exif_dt_any TEXT,                 -- DateTime (0x0132)
  make TEXT, model TEXT,
  has_camera_exif INTEGER,          -- Make or Model or ExposureTime present
  exif_lat REAL, exif_lon REAL,
  lap_var REAL, luma_mean REAL, luma_std REAL, luma_p02 REAL, luma_p98 REAL,
  frac_dark REAL, frac_bright REAL, flat_frac REAL, colorfulness REAL,
  animated INTEGER,
  emb BLOB,                         -- 512 x float16 little-endian, L2-normalised; NULL if no CLIP
  err TEXT                          -- exception class name when processing failed
);

CREATE TABLE sidecars_raw (
  member_idx INTEGER PRIMARY KEY,
  member TEXT NOT NULL, folder TEXT NOT NULL, folder_kind TEXT NOT NULL, json_name TEXT NOT NULL,
  title TEXT, taken_ts INTEGER, creation_ts INTEGER,
  lat REAL, lon REAL, alt REAL, lat_exif REAL, lon_exif REAL,
  url TEXT, description TEXT, people TEXT,           -- people: JSON array of names
  origin_folder TEXT, device_type TEXT,
  from_shared_album INTEGER, from_partner INTEGER,
  favorited INTEGER, archived INTEGER, trashed INTEGER,
  raw TEXT,                                          -- original JSON text, truncated to 64 KiB
  err TEXT
);

CREATE TABLE videos_raw (          -- central-directory info only; video bytes are never read
  member_idx INTEGER PRIMARY KEY,
  member TEXT NOT NULL, folder TEXT NOT NULL, folder_kind TEXT NOT NULL,
  filename TEXT NOT NULL, file_size INTEGER
);

CREATE TABLE skipped_raw (         -- counted, never read
  member_idx INTEGER PRIMARY KEY, member TEXT NOT NULL, reason TEXT NOT NULL
  -- reason: edited | raw | other_image | too_large | trash | non_sidecar_json | album_media
  --         | album_video | outside_photos | directory | other
);
"""

# ---------------------------------------------------------------------------------------------
# Thumb pack: bundle/<cfg>/thumbs/<zipkey>-<shard:04d>.sqlite
# ---------------------------------------------------------------------------------------------
PACK_DDL = """
CREATE TABLE t (
  member_idx INTEGER PRIMARY KEY,
  g BLOB NOT NULL,     -- grid thumbnail, long edge 160 px, WebP, no metadata
  p BLOB NOT NULL      -- preview, long edge 640 px, WebP, no metadata
);
"""

# ---------------------------------------------------------------------------------------------
# Bundle index: bundle/<cfg>/index.sqlite  (opened read-only + immutable on the PC)
# ---------------------------------------------------------------------------------------------
INDEX_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- keys: index_schema, cfg, extract_version, code_version, created_at, threshold, clip_model,
--       tz_fallback, partial, missing_shards

CREATE TABLE zips (
  zip_id INTEGER PRIMARY KEY, zipkey TEXT UNIQUE NOT NULL, name TEXT, export_id TEXT, size INTEGER
);

CREATE TABLE items (
  item_id INTEGER PRIMARY KEY,           -- dense 1..N
  item_uid TEXT UNIQUE NOT NULL,         -- 'g:<AF1Qip...>' from url, else 's:<sha256hex>:<filename>'
  zip_id INTEGER NOT NULL, member TEXT NOT NULL, member_idx INTEGER NOT NULL,
  folder TEXT, folder_kind TEXT, filename TEXT, ext TEXT, format TEXT,
  file_size INTEGER, sha256 BLOB, width INTEGER, height INTEGER, orientation INTEGER,
  phash64 INTEGER, sig BLOB,
  taken_ts INTEGER,                      -- UTC epoch seconds
  taken_src TEXT,                        -- sidecar | exif_offset | exif_local | exif_any | none
  local_date TEXT,                       -- 'YYYY-MM-DD' or NULL when unknown
  local_time TEXT,                       -- 'HH:MM:SS' or NULL
  day_uncertain INTEGER,                 -- 1 when no UTC offset was known
  year INTEGER,
  url TEXT,                              -- validated https://photos.google.com/photo/<id> or NULL
  match_rule TEXT,                       -- R1..R6 | none
  match_conf TEXT,                       -- high | low | none
  lat REAL, lon REAL,
  origin_folder TEXT, device_type TEXT,
  shared INTEGER, partner INTEGER, favorited INTEGER, archived INTEGER,
  description TEXT, people TEXT,
  albums_n INTEGER, albums TEXT,         -- albums: JSON array of album folder names
  make TEXT, model TEXT, has_camera_exif INTEGER, exif_dt TEXT, exif_subsec TEXT,
  lap_var REAL, luma_mean REAL, luma_std REAL, frac_dark REAL, frac_bright REAL,
  flat_frac REAL, colorfulness REAL,
  is_graphic INTEGER,
  pack TEXT, pack_row INTEGER,           -- thumb pack file name + its member_idx
  emb_row INTEGER                        -- row in embeddings.f16.npy, NULL if none
);
CREATE INDEX items_date ON items(local_date, local_time);
CREATE INDEX items_year ON items(year);
CREATE INDEX items_sha ON items(sha256);
CREATE INDEX items_filename ON items(filename);

CREATE TABLE item_aliases (           -- collapsed copies (album copy, double export, ...)
  item_id INTEGER NOT NULL, zip_id INTEGER NOT NULL, member TEXT NOT NULL, reason TEXT NOT NULL
);

CREATE TABLE videos_by_day (local_date TEXT PRIMARY KEY, n INTEGER NOT NULL);

CREATE TABLE dup_groups (
  group_id INTEGER PRIMARY KEY,
  group_key TEXT UNIQUE NOT NULL,        -- sha1 hex of sorted member item_uids
  kind TEXT NOT NULL,                    -- exact | near
  seed_item_id INTEGER NOT NULL,         -- suggested keeper
  size INTEGER NOT NULL,
  deletable INTEGER NOT NULL             -- 1 only if >=2 members have distinct non-null urls
);
CREATE TABLE dup_members (
  group_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
  dist INTEGER, sig_mad REAL, sig_block REAL, sig_chroma REAL,
  is_keeper INTEGER NOT NULL,
  PRIMARY KEY (group_id, item_id)
);
CREATE INDEX dup_members_item ON dup_members(item_id);

CREATE TABLE bursts (
  burst_id INTEGER PRIMARY KEY, source TEXT NOT NULL,   -- filename | time
  best_item_id INTEGER NOT NULL, size INTEGER NOT NULL, start_ts INTEGER, end_ts INTEGER
);
CREATE TABLE burst_members (
  burst_id INTEGER NOT NULL, item_id INTEGER NOT NULL, rank INTEGER NOT NULL, quality REAL,
  PRIMARY KEY (burst_id, item_id)
);
CREATE INDEX burst_members_item ON burst_members(item_id);

CREATE TABLE scores (
  item_id INTEGER NOT NULL, category TEXT NOT NULL, score REAL NOT NULL, reason TEXT,
  PRIMARY KEY (item_id, category)
);
CREATE INDEX scores_cat ON scores(category, score DESC);
-- categories: screenshot messaging blur dark overexposed tiny pocket burst_extra dup_extra

CREATE TABLE stats (key TEXT PRIMARY KEY, value TEXT);   -- JSON-encoded values
"""

JUNK_CATEGORIES = (
    "screenshot", "messaging", "blur", "dark", "overexposed", "tiny", "pocket",
    "burst_extra", "dup_extra",
)

# ---------------------------------------------------------------------------------------------
# Local review state: <home>/state/review.sqlite  (WAL; review_db.py is the only writer)
# ---------------------------------------------------------------------------------------------
REVIEW_DDL = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO state(key, value) VALUES ('rev', '0'), ('review_schema', '1');

CREATE TABLE IF NOT EXISTS queue (
  item_uid TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected')),
  proposed_by TEXT NOT NULL,             -- 'claude:<client>' | 'user'
  batch_id TEXT,
  reason TEXT NOT NULL,
  category TEXT,
  proposed_at TEXT NOT NULL,
  decided_at TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS queue_status ON queue(status);

CREATE TABLE IF NOT EXISTS group_decisions (
  group_key TEXT PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('dismissed', 'keeper')),
  keeper_uid TEXT,
  decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, item_uid TEXT, detail TEXT
);

CREATE TRIGGER IF NOT EXISTS rev_q_ins AFTER INSERT ON queue
  BEGIN UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'rev'; END;
CREATE TRIGGER IF NOT EXISTS rev_q_upd AFTER UPDATE ON queue
  BEGIN UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'rev'; END;
CREATE TRIGGER IF NOT EXISTS rev_q_del AFTER DELETE ON queue
  BEGIN UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'rev'; END;
CREATE TRIGGER IF NOT EXISTS rev_g_ins AFTER INSERT ON group_decisions
  BEGIN UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'rev'; END;
CREATE TRIGGER IF NOT EXISTS rev_g_upd AFTER UPDATE ON group_decisions
  BEGIN UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'rev'; END;
CREATE TRIGGER IF NOT EXISTS rev_g_del AFTER DELETE ON group_decisions
  BEGIN UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'rev'; END;
"""


def create_transport_db(path: str | Path, ddl: str) -> sqlite3.Connection:
    """Create a fresh SQLite file that will be uploaded (DELETE journal, big pages)."""
    path = Path(path)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA page_size = 65536")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = OFF")
    conn.executescript(ddl)
    return conn


def finalize_transport_db(conn: sqlite3.Connection) -> None:
    """Commit, VACUUM and close a transport DB so it is a single self-contained file."""
    conn.commit()
    conn.execute("VACUUM")
    conn.close()


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open a transport DB read-only and immutable (safe for concurrent readers)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def open_review(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the local review DB in WAL mode, autocommit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(REVIEW_DDL)
    except BaseException:
        conn.close()  # do not leak a half-open connection on retry
        raise
    return conn
