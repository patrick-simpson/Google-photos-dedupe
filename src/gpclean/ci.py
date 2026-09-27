"""The GitHub Actions steps of the Drive pipeline (``gpclean ci-*`` and ``selftest-upload``).

Every command reads its inputs from ``GPCLEAN_*`` environment variables (workflow inputs are
passed through ``env:`` only, never pasted into a script) and validates them before use:
they are public, attacker-influenced text. See docs/PLAN.md §2 and §9.

Console discipline (the repo and its Actions logs are public):

* public lines only through :mod:`gpclean.publiclog` (numbers only);
* job outputs only through ``publiclog.github_output`` (integers, booleans, int lists);
* everything else (zip names, tracebacks, rclone output) goes to the private log, which
  ``ci-upload-logs`` copies to ``gpclean-output/logs/<run>-<attempt>/`` on Drive.

Exit codes: 0 ok, 3 Drive quota / download limit hit (resume after the reset), 1 anything else.

Drive layout under ``<remote>:<out_root>`` (default ``gp:gpclean-output``)::

    state/<cfg>/shards.json                        the shard table (private: has zip names)
    state/<cfg>/quota-<run>-<attempt>-p<pass>-w<worker>.flag
    work/<cfg>/<zipkey>/<shard:04d>.meta.sqlite     per-shard checkpoint (written last;
                                                   0 bytes = rejected by a merge, rescan it)
    bundle/<cfg>/thumbs/<zipkey>-<shard:04d>.sqlite thumb packs (written before the meta)
    bundle/<cfg>/{index.sqlite, embeddings.f16.npy, manifest.json}
    logs/<run>-<attempt>/<job>/                    private logs
    probe/<run>/                                   probe details
    selftest/                                      fixture zips for mode=selftest

A shard counts as done when its meta file exists and is not empty. The merge cannot
delete anything on Drive (the rclone wrapper denies it), so when it finds a checkpoint it
cannot use (not SQLite, another cfg or pack, its pack missing or not matching the listed
hash) it overwrites the meta with a 0-byte *tombstone*. The next pass (or run) then sees the
shard as pending again and its rescan overwrites both the pack and the tombstone.

Shard ids are positions in ``shards.json``. A later plan only appends (new zips get new
ids at the end), so ids never change. A zip that is no longer in the folder stays in the
table with ``present: false`` and is ignored from then on; a changed zip gets a new zipkey.

Extra environment knobs besides the ones in docs/INTERFACES.md:
``GPCLEAN_PASS`` (1..3, scan/finalize), ``GPCLEAN_PENDING_IDS`` (the pending job's list, so
all scan workers split the same list), ``GPCLEAN_JOB`` / ``GPCLEAN_WORKER`` (log folder
name), ``GPCLEAN_JOB_BUDGET_MIN`` (default 330), ``GPCLEAN_SCAN_PROCS`` (default: CPUs),
``GPCLEAN_PHOTOS_PER_SHARD`` (selftest only; tests), ``GPCLEAN_RCLONE`` (binary path),
``GPCLEAN_PENDING_QUOTA`` (finalize: the pending job's ``quota`` output), and for
``ci-report`` ``GPCLEAN_PLAN_RESULT``, ``GPCLEAN_MERGE_RESULT``, ``GPCLEAN_PARTIAL``,
``GPCLEAN_MISSING``, ``GPCLEAN_QUOTA_PLAN``, ``GPCLEAN_QUOTA1..3``, ``GPCLEAN_BAD_ZIPS``.
The selftest's shard size and its forced pass-1 failure are code defaults
(``SELFTEST_PHOTOS_PER_SHARD``, ``SELFTEST_FAIL_SHARD``), not workflow values: the shard size
is hashed into ``cfg``, and two workflow files carrying it could drift apart.

Thumb-pack integrity: the merge compares each pack's hash in the Drive listing
(``lsjson --hash``) with the one its meta recorded, preferring ``pack_md5`` (Drive's
md5Checksum, always present; ``scan.scan_shard`` records it next to ``pack_sha256``) and
falling back to ``pack_sha256`` when the backend lists one. A checkpoint written before
``pack_md5`` existed, on a backend that lists no sha256, is only checked for size here;
``verify-bundle`` on the PC re-hashes every pack anyway.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from gpclean import publiclog
from gpclean.config import CLIP_MODELS, THRESHOLD_CHOICES, ScanConfig, parse_bool, validate_folder
from gpclean.publiclog import Ev
from gpclean.rclone import QuotaError, Rclone, RcloneError
from gpclean.store import RcloneStore, check_rel

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAIL, EXIT_QUOTA = 0, 1, 3

# Public phase numbers (the only thing a FAILED line says about where it failed).
PHASE_SCOPE, PHASE_PLAN, PHASE_PENDING, PHASE_SCAN, PHASE_FINALIZE = 1, 2, 3, 4, 5
PHASE_MERGE, PHASE_REPORT, PHASE_LOGS, PHASE_SELFTEST, PHASE_PROBE = 6, 7, 8, 9, 10

MODES = ("full", "probe", "merge_only", "selftest")
REQUIRED_SCOPES = frozenset({
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
})
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

SHARD_TIMEOUT_S = 45 * 60          # watchdog for one shard
DEFAULT_JOB_BUDGET_MIN = 330       # stop *starting* shards after 5 h 30 m
# A shard started just before the budget ends may run this much longer, so it still ends
# before the job's timeout-minutes (350) kills it: 330 + 12 + ~3 min setup < 350.
HARD_STOP_EXTRA_MIN = 12
MAX_CONSECUTIVE_ERRORS = 3         # a dying backend fails every shard; stop early
SELFTEST_DIR = "selftest"
# ScanConfig's minimum: the small fixture zips (44, 33 and 5 photos) then make 8 shards, so
# the selftest covers shards that start mid-zip and several packs per zip.
SELFTEST_PHOTOS_PER_SHARD = 10
SELFTEST_FAIL_SHARD = 1            # pass 1 fails this shard on purpose; pass 2 must redo it
SHARDS_FORMAT = 1
# Google resets daily API quotas at midnight Pacific time.
QUOTA_RESET_TZ = "America/Los_Angeles"

_REMOTE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", re.ASCII)
_JOB_RE = re.compile(r"[a-z][a-z0-9-]{0,30}", re.ASCII)
_ZIPKEY_RE = re.compile(r"[0-9a-f]{12}", re.ASCII)
_META_REL_RE = re.compile(r"([0-9a-f]{12})/(\d{4,6})\.meta\.sqlite", re.ASCII)
_JOB_RESULTS = frozenset({"success", "failure", "cancelled", "skipped", ""})


# The scan's clock (time.monotonic; scan_shard deadlines use the same clock). A seam for tests.
_clock = time.monotonic


class SelftestFailure(RuntimeError):
    """The shard the selftest deliberately fails in pass 1 (proves pass 2 resumes it)."""


# ----------------------------------------------------------------------------- inputs


def _env_int(environ: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = (environ.get(name) or "").strip()
    if raw == "":
        return default
    if not re.fullmatch(r"\d{1,13}", raw, re.ASCII):
        raise ValueError(f"{name} must be a non-negative integer")
    value = int(raw)
    if not lo <= value <= hi:
        raise ValueError(f"{name} out of range")
    return value


def _env_choice(environ: dict, name: str, default: str, choices) -> str:
    value = (environ.get(name) or "").strip() or default
    if value not in choices:
        raise ValueError(f"{name} is not one of the allowed values")
    return value


@dataclass(frozen=True)
class CiEnv:
    """Validated CI inputs. Build it with :func:`load_env`."""

    folder: str
    threshold: int
    include_albums: bool
    mode: str
    clip_model: str
    workers: int
    run_id: int
    attempt: int
    out_root: str
    remote: str
    rclone_config: Path | None
    private_dir: Path | None
    pass_no: int
    fail_shard: int | None
    photos_per_shard: int
    job_budget_min: int
    scan_procs: int
    job: str
    worker: int | None

    @property
    def scan_cfg(self) -> ScanConfig:
        """The scan configuration (hashed into ``cfg``)."""
        return ScanConfig(include_albums=self.include_albums, clip_model=self.clip_model,
                          photos_per_shard=self.photos_per_shard)

    @property
    def cfg(self) -> str:
        return self.scan_cfg.cfg_hash()

    @property
    def run_tag(self) -> str:
        return f"{self.run_id}-{self.attempt}"

    @property
    def source(self) -> str:
        """rclone path of the Takeout folder (read-only use)."""
        return f"{self.remote}:{self.folder}"

    @property
    def out_remote(self) -> str:
        """rclone path of the output root (the only place anything is written)."""
        return f"{self.remote}:{self.out_root}"


def load_env(environ: dict | None = None) -> CiEnv:
    """Read and validate every ``GPCLEAN_*`` input. Raises ValueError on a bad value."""
    e = os.environ if environ is None else environ
    mode = _env_choice(e, "GPCLEAN_MODE", "full", MODES)
    out_root = validate_folder((e.get("GPCLEAN_OUT_ROOT") or "").strip() or "gpclean-output")
    remote = (e.get("GPCLEAN_REMOTE") or "").strip() or "gp"
    if not _REMOTE_RE.fullmatch(remote):
        raise ValueError("GPCLEAN_REMOTE is not a valid remote name")
    folder = validate_folder(e.get("GPCLEAN_FOLDER") if e.get("GPCLEAN_FOLDER") is not None
                             else "Takeout")
    if mode == "selftest":
        # The selftest reads the fixture zips that selftest-upload put into the output root,
        # whatever folder was typed in.
        folder = f"{out_root}/{SELFTEST_DIR}"
    threshold = _env_int(e, "GPCLEAN_THRESHOLD", 3, min(THRESHOLD_CHOICES), max(THRESHOLD_CHOICES))
    pass_no = _env_int(e, "GPCLEAN_PASS", 0, 0, 3)
    fail_shard = None
    if mode == "selftest" and pass_no == 1:  # only selftest pass 1 fails a shard on purpose
        fail_shard = _env_int(e, "GPCLEAN_SELFTEST_FAIL_SHARD", SELFTEST_FAIL_SHARD,
                              0, 100_000)
    photos_per_shard = ScanConfig().photos_per_shard
    if mode == "selftest":
        photos_per_shard = _env_int(e, "GPCLEAN_PHOTOS_PER_SHARD", SELFTEST_PHOTOS_PER_SHARD,
                                    10, 100_000)
    worker_raw = (e.get("GPCLEAN_WORKER") or "").strip()
    config = (e.get("RCLONE_CONFIG") or "").strip()
    private = (e.get("GPCLEAN_PRIVATE_DIR") or "").strip()
    return CiEnv(
        folder=folder,
        threshold=threshold,
        include_albums=parse_bool(e.get("GPCLEAN_INCLUDE_ALBUMS")),
        mode=mode,
        clip_model=_env_choice(e, "GPCLEAN_CLIP_MODEL", "b32", CLIP_MODELS),
        workers=_env_int(e, "GPCLEAN_WORKERS", 6, 1, 20),
        run_id=_env_int(e, "GPCLEAN_RUN_ID", 0, 0, 10**13 - 1),
        attempt=_env_int(e, "GPCLEAN_ATTEMPT", 1, 1, 999),
        out_root=out_root,
        remote=remote,
        rclone_config=Path(config) if config else None,
        private_dir=Path(private) if private else None,
        pass_no=pass_no,
        fail_shard=fail_shard,
        photos_per_shard=photos_per_shard,
        job_budget_min=_env_int(e, "GPCLEAN_JOB_BUDGET_MIN", DEFAULT_JOB_BUDGET_MIN, 1, 345),
        scan_procs=_env_int(e, "GPCLEAN_SCAN_PROCS", os.cpu_count() or 1, 1, 64),
        job=_job_name(e.get("GPCLEAN_JOB")),
        worker=_env_int(e, "GPCLEAN_WORKER", 0, 0, 63) if worker_raw else None,
    )


def _job_name(raw: str | None) -> str:
    value = (raw or "").strip() or "job"
    if not _JOB_RE.fullmatch(value):
        raise ValueError("GPCLEAN_JOB is not a valid job name")
    return value


# ------------------------------------------------------------------------------ seams


def make_rclone(env: CiEnv) -> Rclone:
    """The rclone wrapper for this job (tests replace this function with a fake)."""
    binary = (os.environ.get("GPCLEAN_RCLONE") or "").strip()
    kwargs = {"binary": binary} if binary else {}
    return Rclone(config=env.rclone_config, out_root=env.out_remote, **kwargs)


def make_store(env: CiEnv, rc: Rclone):
    """The output store on Drive (tests replace this function with a LocalStore)."""
    return RcloneStore(rc, env.out_remote)


# ------------------------------------------------------------------------- Drive paths


def shards_rel(cfg: str) -> str:
    return f"state/{cfg}/shards.json"


def meta_rel(cfg: str, zipkey: str, shard: int) -> str:
    return f"work/{cfg}/{zipkey}/{shard:04d}.meta.sqlite"


def pack_name(zipkey: str, shard: int) -> str:
    return f"{zipkey}-{shard:04d}.sqlite"


def pack_rel(cfg: str, zipkey: str, shard: int) -> str:
    return f"bundle/{cfg}/thumbs/{pack_name(zipkey, shard)}"


def _quota_prefix(env: CiEnv) -> str:
    return f"quota-{env.run_tag}-p{env.pass_no}-"


def _log_dir_rel(env: CiEnv, worker: int | None = None) -> str:
    """``logs/<run>-<attempt>/<job>[-p<pass>][-w<worker>]``: one folder per job, so parallel
    jobs never write the same Drive path (Drive allows duplicate names; races make twins)."""
    name = env.job
    if env.pass_no:
        name += f"-p{env.pass_no}"
    worker = env.worker if worker is None else worker
    if worker is not None:
        name += f"-w{worker}"
    return f"logs/{env.run_tag}/{name}"


# ------------------------------------------------------------------------- the runner


def run_step(phase: int, body: Callable[[CiEnv], int]) -> int:
    """Common wrapper: private logging, redacting excepthook, env validation, exit codes."""
    publiclog.setup_logging()
    publiclog.install_excepthook(phase)
    try:
        env = load_env()
        return body(env)
    except QuotaError:
        log.warning("phase %d stopped by a Drive quota / download limit", phase)
        # A job output too, so the next gate (pass2/pass3, the report) sees the quota even
        # when this step ran outside the scan loop (plan, pending, finalize).
        try:
            publiclog.github_output(quota=True)
        except Exception:
            log.warning("could not write the quota job output", exc_info=True)
        publiclog.event(Ev.FAILED, phase=phase, quota=True)
        return EXIT_QUOTA
    except Exception:
        # The installed hook logs the traceback privately and prints only
        # "FAILED phase=<n> exc=<Type>" publicly.
        sys.excepthook(*sys.exc_info())
        return EXIT_FAIL


def raise_if_quota(rc: Rclone, exc: BaseException) -> None:
    """Turn a failed HTTP read into QuotaError when the serve log shows a quota hit."""
    if isinstance(exc, QuotaError):
        raise exc
    if rc.serve_quota_hit():
        raise QuotaError("serve", 7) from exc


def quota_reset_hour_utc(now: datetime | None = None) -> int:
    """UTC hour of the next midnight Pacific time (when Google's daily quotas reset)."""
    tz = ZoneInfo(QUOTA_RESET_TZ)
    now = now or datetime.now(UTC)
    tomorrow = now.astimezone(tz).date() + timedelta(days=1)
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz)
    return midnight.astimezone(UTC).hour


# ------------------------------------------------------------------------ scope check


def _config_secrets(path: Path) -> list[str]:
    """Access tokens, refresh tokens and client secrets found in an rclone config file."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, configparser.Error):
        return []
    found: list[str] = []
    for section in parser.sections():
        secret = parser.get(section, "client_secret", fallback="").strip()
        if secret:
            found.append(secret)
        raw = parser.get(section, "token", fallback="").strip()
        if raw:
            try:
                token = json.loads(raw)
            except ValueError:
                continue
            if isinstance(token, dict):
                for key in ("access_token", "refresh_token"):
                    value = token.get(key)
                    if isinstance(value, str) and value:
                        found.append(value)
    return list(dict.fromkeys(found))


def mask_config_secrets(path: Path | None) -> int:
    """``::add-mask::`` every secret in the rclone config; returns how many were masked."""
    if path is None:
        return 0
    n = 0
    for secret in _config_secrets(path):
        try:
            publiclog.add_mask(secret)
            n += 1
        except ValueError:
            log.warning("a config secret could not be masked (control characters)")
    return n


def tokeninfo_scopes(access_token: str) -> set[str]:
    """The scopes Google reports for ``access_token``.

    The token goes in a POST form body, never in a URL (URLs end up in proxy and server
    logs). Errors say nothing about the token.
    """
    body = urllib.parse.urlencode({"access_token": access_token}).encode("ascii")
    req = urllib.request.Request(
        TOKENINFO_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("tokeninfo answered HTTP %d", exc.code)
        raise RuntimeError("tokeninfo request failed") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("tokeninfo request failed: %s", type(exc).__name__)
        raise RuntimeError("tokeninfo request failed") from None
    scope = data.get("scope") if isinstance(data, dict) else None
    return set(scope.split()) if isinstance(scope, str) else set()


def scope_check(env: CiEnv, rc: Rclone) -> bool:
    """Mask the secrets, refresh the token, and require exactly the two Drive scopes."""
    if env.rclone_config is None or not env.rclone_config.is_file():
        raise FileNotFoundError("RCLONE_CONFIG does not name a file")
    mask_config_secrets(env.rclone_config)  # before anything can print
    token = rc.access_token(env.remote, refresh=True)
    publiclog.add_mask(token)
    mask_config_secrets(env.rclone_config)  # the refresh wrote new tokens into the file
    scopes = tokeninfo_scopes(token)
    ok = scopes == set(REQUIRED_SCOPES)
    if not ok:
        log.error("token scopes are not exactly drive.readonly + drive.file (%d scopes)",
                  len(scopes))
    return ok


def cli_scope_check() -> int:
    """``gpclean ci-scope-check``: fail closed unless the token has exactly the two scopes."""
    def body(env: CiEnv) -> int:
        ok = scope_check(env, make_rclone(env))
        if ok:
            publiclog.event(Ev.OK, phase=PHASE_SCOPE, scope_ok=1)
            return EXIT_OK
        publiclog.event(Ev.FAILED, phase=PHASE_SCOPE, scope_ok=0)
        return EXIT_FAIL
    return run_step(PHASE_SCOPE, body)


# ------------------------------------------------------------------------- shard table


def list_zips(rc: Rclone, source: str) -> list[dict]:
    """The readable-by-name ``*.zip`` files in ``source`` (see :func:`split_duplicates`)."""
    return split_duplicates(list_zip_files(rc, source))[0]


def split_duplicates(zips: list[dict]) -> tuple[list[dict], int]:
    """Drop every zip whose name is shared by another file; return ``(kept, n_dropped)``.

    Drive allows two files with the same name, but ``rclone serve http`` exposes only one
    of them per URL, so the second zipkey would read the other file's bytes (with the wrong
    size). Neither is safe to scan until the user renames or removes one.
    """
    counts: dict[str, int] = {}
    for z in zips:
        counts[z["name"]] = counts.get(z["name"], 0) + 1
    kept = [z for z in zips if counts[z["name"]] == 1]
    if len(kept) != len(zips):
        log.warning("%d zip files share a name with another file; skipped: %s",
                    len(zips) - len(kept), sorted({n for n, c in counts.items() if c > 1}))
    return kept, len(zips) - len(kept)


def list_zip_files(rc: Rclone, source: str) -> list[dict]:
    """The ``*.zip`` files directly inside ``source``, sorted by name, with their zipkey."""
    from gpclean.scan import zipkey_for

    out = []
    for e in rc.lsjson(source, files_only=True):
        if e.get("IsDir"):
            continue
        name = e.get("Name") or e.get("Path") or ""
        path = e.get("Path") or name
        if not isinstance(name, str) or "/" in path or not name.lower().endswith(".zip"):
            continue
        if any(ord(ch) < 32 for ch in name):
            continue  # not a Takeout name; never pass such text to anything
        size = int(e.get("Size") or 0)
        modtime = str(e.get("ModTime") or "")
        # Drive entries carry an ID; other backends (tests, local) do not.
        drive_id = str(e.get("ID") or f"path:{name}")
        out.append({"name": name, "size": size, "modtime": modtime, "drive_id": drive_id,
                    "zipkey": zipkey_for(drive_id, size, modtime)})
    out.sort(key=lambda z: z["name"])
    return out


def zip_url(base: str, name: str) -> str:
    """URL of a zip under ``rclone serve http`` (the base URL has no trailing slash)."""
    return base + "/" + urllib.parse.quote(name)


def read_zip_entries(url: str, size: int):
    """Fetch a zip's tail over HTTP; return ``(tail, entries)``."""
    from gpclean.takeout.members import list_entries
    from gpclean.takeout.zipsource import ZipSource

    with ZipSource.open_http(url, size) as src:
        return src.tail(), list_entries(src.zipfile())


def _empty_table(cfg: str) -> dict:
    return {"format": SHARDS_FORMAT, "cfg": cfg, "zips": [], "shards": []}


def validate_table(table: object, cfg: str) -> dict:
    """Check the structure of a shards.json document (it is re-read by every job)."""
    if not isinstance(table, dict) or table.get("format") != SHARDS_FORMAT:
        raise ValueError("shards.json has an unknown format")
    if table.get("cfg") != cfg:
        raise ValueError("shards.json belongs to another cfg")
    zips, shards = table.get("zips"), table.get("shards")
    if not isinstance(zips, list) or not isinstance(shards, list):
        raise ValueError("shards.json is malformed")
    keys = set()
    for z in zips:
        if not (isinstance(z, dict) and isinstance(z.get("zipkey"), str)
                and _ZIPKEY_RE.fullmatch(z["zipkey"]) and isinstance(z.get("name"), str)
                and isinstance(z.get("export_id"), str)
                and type(z.get("size")) is int and type(z.get("n_entries")) is int
                and isinstance(z.get("present"), bool)):
            raise ValueError("shards.json has a malformed zip entry")
        keys.add(z["zipkey"])
    for i, s in enumerate(shards):
        if not (isinstance(s, dict) and s.get("id") == i and s.get("zipkey") in keys
                and isinstance(s.get("zip_name"), str) and isinstance(s.get("export_id"), str)
                and all(type(s.get(k)) is int for k in ("shard", "start", "end", "n_images"))):
            raise ValueError("shards.json has a malformed shard entry")
    return table


def load_table(store, cfg: str, *, required: bool = True) -> dict | None:
    """Download and validate ``state/<cfg>/shards.json`` (None if absent and not required)."""
    rel = shards_rel(cfg)
    if not store.exists(rel):
        if required:
            raise FileNotFoundError("shards.json not found (did the plan job run?)")
        return None
    with tempfile.TemporaryDirectory(prefix="gpclean-") as tmp:
        local = Path(tmp) / "shards.json"
        store.get(rel, local)
        return validate_table(json.loads(local.read_text(encoding="utf-8")), cfg)


def save_table(store, table: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="gpclean-") as tmp:
        local = Path(tmp) / "shards.json"
        local.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        store.put(local, shards_rel(table["cfg"]))


def extend_table(table: dict, listed: list[dict], new_entries: dict[str, list],
                 cfg: ScanConfig) -> dict:
    """Mark which zips are present and append the shards of new ones.

    ``new_entries`` maps the zipkey of a listed zip that is not in the table yet to its
    entry list. A new zip missing from it (its tail could not be read) is left out of the
    table, so the next plan tries it again. Existing shards keep their ids; new shards get
    the next free ids.
    """
    from gpclean.scan import plan_shards
    from gpclean.takeout.names import export_id

    listed_keys = {z["zipkey"] for z in listed}
    for z in table["zips"]:
        z["present"] = z["zipkey"] in listed_keys
    known = {z["zipkey"] for z in table["zips"]}
    for z in listed:
        key = z["zipkey"]
        if key in known or key not in new_entries:
            continue
        entries = new_entries[key]
        table["zips"].append({
            "zipkey": key, "name": z["name"], "size": z["size"], "modtime": z["modtime"],
            "drive_id": z["drive_id"], "export_id": export_id(z["name"]),
            "n_entries": len(entries), "present": True,
        })
        known.add(key)
        for spec in plan_shards(entries, cfg, zipkey=key, zip_name=z["name"]):
            table["shards"].append({
                "id": len(table["shards"]), "zipkey": key, "zip_name": spec.zip_name,
                "export_id": spec.export_id, "shard": spec.shard, "start": spec.start,
                "end": spec.end, "n_images": spec.n_images,
            })
    return table


def present_shards(table: dict) -> list[dict]:
    """Shards of the zips that are in the folder now."""
    keys = {z["zipkey"] for z in table["zips"] if z["present"]}
    return [s for s in table["shards"] if s["zipkey"] in keys]


def list_sizes(store, rel_dir: str) -> dict[str, int]:
    """``{path relative to rel_dir: size in bytes}`` of every file below ``rel_dir``.

    Like ``store.list`` (recursive; a missing folder lists as empty) but with sizes, which
    ``done_set`` needs to tell a tombstone from a checkpoint. The stores have no such call
    yet, so this uses one if a store grows it and otherwise asks the backend directly.
    """
    lister = getattr(store, "list_sizes", None)
    if callable(lister):
        return dict(lister(rel_dir))
    rel = check_rel(rel_dir)
    if isinstance(store, RcloneStore):
        try:
            entries = store.rclone.lsjson(f"{store.remote_root}/{rel}", recursive=True,
                                          files_only=True)
        except QuotaError:
            raise
        except RcloneError as exc:
            if exc.returncode == 3:  # directory not found
                return {}
            raise
        return {e["Path"]: int(e.get("Size", -1)) for e in entries
                if isinstance(e.get("Path"), str) and not e.get("IsDir")}
    base = Path(store.root) / rel  # a LocalStore (run-local and tests)
    return {name: (base / name).stat().st_size for name in store.list(rel)}


def done_set(store, cfg: str) -> set[tuple[str, int]]:
    """(zipkey, shard) of every meta checkpoint on Drive, tombstones (0 bytes) excluded."""
    done = set()
    for rel, size in list_sizes(store, f"work/{cfg}").items():
        m = _META_REL_RE.fullmatch(rel)
        if m and size != 0:
            done.add((m.group(1), int(m.group(2))))
    return done


def tombstone(store, cfg: str, zipkey: str, shard: int) -> None:
    """Overwrite a shard's meta with 0 bytes so the next pass rescans it (see done_set)."""
    with tempfile.TemporaryDirectory(prefix="gpclean-") as tmp:
        empty = Path(tmp) / "tombstone"
        empty.write_bytes(b"")
        store.put(empty, meta_rel(cfg, zipkey, shard))


def pending_ids(table: dict, done: set[tuple[str, int]]) -> list[int]:
    """Ids of present shards without a checkpoint, in table order."""
    return [s["id"] for s in present_shards(table) if (s["zipkey"], s["shard"]) not in done]


def _output_tree(env: CiEnv, zipkeys: list[str]) -> list[str]:
    """Every folder parallel jobs will write into, created once by the plan job.

    Drive allows two folders with the same name, so two workers creating
    ``work/<cfg>/<zipkey>`` at the same moment could each make one.
    """
    cfg = env.cfg
    rels = [f"state/{cfg}", f"work/{cfg}", f"bundle/{cfg}/thumbs", f"logs/{env.run_tag}",
            "probe"]
    rels += [f"work/{cfg}/{key}" for key in zipkeys]
    return rels


def _plan(env: CiEnv) -> int:
    rc = make_rclone(env)
    store = make_store(env, rc)
    cfg = env.cfg
    listed, dup_names = split_duplicates(list_zip_files(rc, env.source))
    table = load_table(store, cfg, required=False) or _empty_table(cfg)
    store.mkdirs(_output_tree(env, [z["zipkey"] for z in listed]))

    known = {z["zipkey"] for z in table["zips"]}
    new = [z for z in listed if z["zipkey"] not in known]
    new_entries: dict[str, list] = {}
    unreadable = 0
    if new:
        with rc.serve_http(env.source) as base:
            for z in new:
                try:
                    _tail, entries = read_zip_entries(zip_url(base, z["name"]), z["size"])
                except Exception as exc:
                    raise_if_quota(rc, exc)
                    # One broken or stray zip must not stop the rest from being scanned.
                    # It stays out of the table (retried by the next plan) and the report
                    # turns the run red with the count.
                    log.warning("zip %r is not readable (%s); skipped", z["name"],
                                type(exc).__name__)
                    unreadable += 1
                    continue
                new_entries[z["zipkey"]] = entries
    extend_table(table, listed, new_entries, env.scan_cfg)
    save_table(store, table)

    present = present_shards(table)
    n_zips = sum(1 for z in table["zips"] if z["present"])
    n_exports = len({z["export_id"] for z in table["zips"] if z["present"]})
    bad_zips = unreadable + dup_names
    pending = pending_ids(table, done_set(store, cfg))
    log.info("plan: %d zips (%d new), %d shards, %d pending, %d unreadable, %d duplicate names",
             n_zips, len(new) - unreadable, len(present), len(pending), unreadable, dup_names)
    publiclog.event(Ev.COUNT, phase=PHASE_PLAN, n_zips=n_zips, new_zips=len(new) - unreadable,
                    n_shards=len(present), pending=len(pending))
    if bad_zips:
        publiclog.event(Ev.COUNT, phase=PHASE_PLAN, bad_zips=bad_zips, unreadable=unreadable,
                        dup_names=dup_names)
    if n_exports > 1:
        # Several Takeout exports in one folder is supported, but worth a look.
        publiclog.event(Ev.COUNT, phase=PHASE_PLAN, n_exports=n_exports, exports_warning=True)
    publiclog.github_output(n_zips=n_zips, n_shards=len(present), n_exports=n_exports,
                            pending=len(pending), bad_zips=bad_zips)
    return EXIT_OK


def cli_plan() -> int:
    """``gpclean ci-plan``: create the Drive tree, list the zips, extend shards.json."""
    return run_step(PHASE_PLAN, _plan)


# ----------------------------------------------------------------------------- pending


def _pending(env: CiEnv) -> int:
    rc = make_rclone(env)
    store = make_store(env, rc)
    table = load_table(store, env.cfg)
    ids = pending_ids(table, done_set(store, env.cfg))
    # Normally the plan job made it already; a "re-run failed jobs" attempt has a new
    # attempt number, and the scan workers must not race to create its log folder.
    store.mkdirs([f"logs/{env.run_tag}"])
    n_workers = min(env.workers, len(ids))
    ids_out = json.dumps(ids, separators=(",", ":"))
    publiclog.event(Ev.COUNT, phase=PHASE_PENDING, pending=len(ids), workers=n_workers)
    publiclog.github_output(
        count=len(ids), of=n_workers, workers=list(range(n_workers)),
        # Too long for an output: the scan workers then list Drive themselves.
        ids=ids if len(ids_out) <= 4000 else "")
    return EXIT_OK


def cli_pending() -> int:
    """``gpclean ci-pending``: count unfinished shards and size the scan matrix."""
    return run_step(PHASE_PENDING, _pending)


# -------------------------------------------------------------------------------- scan


def _parse_pending_ids(raw: str | None) -> list[int] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    value = json.loads(raw)
    if not isinstance(value, list) or not all(type(v) is int and v >= 0 for v in value):
        raise ValueError("GPCLEAN_PENDING_IDS must be a JSON list of ints")
    return value


def _write_quota_flag(env: CiEnv, store, worker: int) -> None:
    """Record a quota stop on Drive so finalize (a different job) can see it."""
    rel = f"state/{env.cfg}/{_quota_prefix(env)}w{worker}.flag"
    with tempfile.TemporaryDirectory(prefix="gpclean-") as tmp:
        local = Path(tmp) / "quota.flag"
        local.write_text(json.dumps({"worker": worker,
                                     "reset_hour_utc": quota_reset_hour_utc()}) + "\n",
                         encoding="utf-8")
        store.put(local, rel)


class _ZipCache:
    """Per job: fetch each zip's tail once and build its (picklable) HTTP opener."""

    def __init__(self, base: str, zips: dict[str, dict]):
        self.base = base
        self.zips = zips
        self._cache: dict[str, tuple[object, list]] = {}

    def get(self, zipkey: str):
        if zipkey not in self._cache:
            from gpclean.scan import HttpOpener

            z = self.zips[zipkey]
            url = zip_url(self.base, z["name"])
            tail, entries = read_zip_entries(url, z["size"])
            if len(entries) != z["n_entries"]:
                raise ValueError("zip changed since it was planned")
            self._cache[zipkey] = (HttpOpener(url, z["size"], tail), entries)
        return self._cache[zipkey]


@dataclass
class _Tally:
    """Scan counters, kept outside :func:`_scan_shards` so a quota stop still reports them."""

    total: int = 0
    done: int = 0
    errors: int = 0


def _scan(env: CiEnv, worker: int, of: int) -> int:
    if not (1 <= of <= 64 and 0 <= worker < of):
        raise ValueError("--worker/--of out of range")
    if not 1 <= env.pass_no <= 3:
        raise ValueError("GPCLEAN_PASS must be 1..3 for ci-scan")
    budget_end = _clock() + env.job_budget_min * 60
    hard_stop = budget_end + HARD_STOP_EXTRA_MIN * 60

    rc = make_rclone(env)
    store = make_store(env, rc)
    tally = _Tally()
    quota = False
    try:
        # Everything that touches Drive, including reading shards.json and listing the
        # checkpoints: a quota hit anywhere here must reach the flag file and finalize.
        _scan_shards(env, rc, store, worker, of, tally, budget_end=budget_end,
                     hard_stop=hard_stop)
    except QuotaError:
        quota = True
    publiclog.event(Ev.PHASE_DONE, phase=PHASE_SCAN, done=tally.done, total=tally.total,
                    errors=tally.errors, quota=quota)
    if quota:
        try:
            _write_quota_flag(env, store, worker)
        except Exception:
            log.warning("could not write the quota flag", exc_info=True)
        _upload_logs(env, store, worker=worker)
        try:
            publiclog.github_output(quota=True)
        except Exception:
            log.warning("could not write the quota job output", exc_info=True)
        publiclog.event(Ev.FAILED, phase=PHASE_SCAN, quota=True)
        return EXIT_QUOTA
    return EXIT_OK if tally.errors == 0 else EXIT_FAIL


def _scan_shards(env: CiEnv, rc: Rclone, store, worker: int, of: int, tally: _Tally, *,
                 budget_end: float, hard_stop: float) -> None:
    """Scan this worker's share of the pending shards. Raises QuotaError on a quota stop."""
    cfg = env.cfg
    table = load_table(store, cfg)
    done = done_set(store, cfg)
    ids = _parse_pending_ids(os.environ.get("GPCLEAN_PENDING_IDS"))
    if ids is None:
        ids = pending_ids(table, done)
    shards = table["shards"]
    present = present_shards(table)
    mine = [sid for i, sid in enumerate(ids) if i % of == worker]
    # Skip ids that are not (or no longer) valid pending shards, e.g. finished by a re-run.
    present_ids = {s["id"] for s in present}
    mine = [sid for sid in mine if sid in present_ids
            and (shards[sid]["zipkey"], shards[sid]["shard"]) not in done]
    fail_id = None
    if env.fail_shard is not None and env.fail_shard < len(present):
        fail_id = present[env.fail_shard]["id"]

    tally.total = len(mine)
    publiclog.event(Ev.PHASE_START, phase=PHASE_SCAN, total=tally.total)
    if not mine:
        return
    from gpclean.scan import ScanPool

    zips = {z["zipkey"]: z for z in table["zips"]}
    consecutive = 0
    # One pool for the whole job: each worker process starts and loads CLIP once, not once
    # per shard. A shard that times out or loses a worker leaves the pool usable.
    with ScanPool(env.scan_procs, env.scan_cfg, env.clip_model) as pool:
        # Fetch and verify the CLIP weights once, before any worker process starts (they
        # only load the verified file), so a bad model fails the job before the first shard.
        pool.prepare()
        with rc.serve_http(env.source) as base, \
                tempfile.TemporaryDirectory(prefix="gpclean-scan-") as tmp:
            cache = _ZipCache(base, zips)
            for sid in mine:
                if _clock() >= budget_end:
                    log.info("job budget used up; leaving the rest to the next pass")
                    break
                try:
                    _scan_one(env, cache, store, shards[sid], Path(tmp), pool=pool,
                              fail=(sid == fail_id),
                              deadline=min(_clock() + SHARD_TIMEOUT_S, hard_stop))
                    tally.done += 1
                    consecutive = 0
                except SelftestFailure:
                    log.info("selftest: shard %d failed on purpose", sid)
                    tally.errors += 1
                except Exception as exc:
                    raise_if_quota(rc, exc)
                    log.warning("shard %d failed: %s", sid, type(exc).__name__, exc_info=True)
                    tally.errors += 1
                    consecutive += 1
                publiclog.event(Ev.PROGRESS, phase=PHASE_SCAN, done=tally.done,
                                total=tally.total, errors=tally.errors)
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    log.warning("too many failures in a row; stopping this worker")
                    break


def _scan_one(env: CiEnv, cache: _ZipCache, store, s: dict, tmp: Path, *, pool,
              fail: bool, deadline: float) -> None:
    """Scan one shard to temp files, then upload the pack and, last, the meta checkpoint.

    ``pool`` is the job's ``scan.ScanPool`` (made for ``env.scan_cfg`` and ``env.clip_model``).
    """
    from gpclean.scan import ShardSpec, scan_shard

    if fail:
        raise SelftestFailure("forced selftest failure")
    opener, entries = cache.get(s["zipkey"])
    if s["end"] > len(entries):
        raise ValueError("shard range does not fit the zip")
    spec = ShardSpec(zipkey=s["zipkey"], zip_name=s["zip_name"], export_id=s["export_id"],
                     shard=s["shard"], start=s["start"], end=s["end"], n_images=s["n_images"])
    meta_local = tmp / f"{s['id']}.meta.sqlite"
    pack_local = tmp / pack_name(s["zipkey"], s["shard"])  # its name is recorded in the meta
    try:
        scan_shard(opener, spec, entries, env.scan_cfg, meta_path=meta_local,
                   pack_path=pack_local, workers=env.scan_procs, embedder_name=env.clip_model,
                   deadline=deadline, pool=pool)
        # Pack first, meta last: the meta file on Drive is the "shard done" checkpoint.
        store.put(pack_local, pack_rel(env.cfg, s["zipkey"], s["shard"]))
        store.put(meta_local, meta_rel(env.cfg, s["zipkey"], s["shard"]))
    finally:
        meta_local.unlink(missing_ok=True)
        pack_local.unlink(missing_ok=True)


def cli_scan(worker, of) -> int:
    """``gpclean ci-scan --worker W --of N``: scan this worker's share of the pending shards."""
    return run_step(PHASE_SCAN, lambda env: _scan(env, int(worker), int(of)))


# ---------------------------------------------------------------------------- finalize


def _finalize(env: CiEnv) -> int:
    if not 1 <= env.pass_no <= 3:
        raise ValueError("GPCLEAN_PASS must be 1..3 for ci-finalize")
    rc = make_rclone(env)
    store = make_store(env, rc)
    table = load_table(store, env.cfg)
    pending = pending_ids(table, done_set(store, env.cfg))
    prefix = _quota_prefix(env)
    # A worker's flag file, or the pending job itself stopping on a quota (then no worker ran).
    quota = parse_bool(os.environ.get("GPCLEAN_PENDING_QUOTA")) or any(
        Path(rel).name.startswith(prefix) and rel.endswith(".flag")
        for rel in store.list(f"state/{env.cfg}"))
    done = not pending
    publiclog.event(Ev.COUNT, phase=PHASE_FINALIZE, pending=len(pending),
                    total=len(present_shards(table)), done=done, quota=quota)
    publiclog.github_output(done=done, quota=quota)
    return EXIT_OK


def cli_finalize() -> int:
    """``gpclean ci-finalize``: re-list Drive; output ``done`` and ``quota`` for this pass."""
    return run_step(PHASE_FINALIZE, _finalize)


# ------------------------------------------------------------------------------- merge


def _listed_packs(rc: Rclone, env: CiEnv) -> dict[str, dict]:
    """``{pack name: lsjson entry}`` of the thumbs folder, with the hashes the backend has."""
    out = {}
    for e in rc.lsjson(f"{env.out_remote}/bundle/{env.cfg}/thumbs", files_only=True, hash=True):
        name = e.get("Name") or e.get("Path")
        if isinstance(name, str) and not e.get("IsDir"):
            out[name] = e
    return out


def pack_matches_listing(info: dict, entry: dict) -> bool:
    """Does the Drive listing of a pack agree with what its meta recorded?

    Compares the size, then ``md5`` (Drive's md5Checksum) when the meta has ``pack_md5``,
    else ``sha256`` when the backend lists one. With neither, only the size is checked
    (see the module docstring); ``verify-bundle`` re-hashes every pack on the PC.
    """
    size = str(info.get("pack_size") or "")
    if size and str(entry.get("Size")) != size:
        return False
    hashes = entry.get("Hashes") if isinstance(entry.get("Hashes"), dict) else {}
    for meta_key, listed_key in (("pack_md5", "md5"), ("pack_sha256", "sha256")):
        want, got = info.get(meta_key), hashes.get(listed_key)
        if want and isinstance(got, str) and got:
            return got.lower() == str(want).lower()
    return True


def _merge(env: CiEnv) -> int:
    from gpclean.config import MergeConfig
    from gpclean.merge.bundle import EMB_NAME, INDEX_NAME, MANIFEST_NAME, build_bundle
    from gpclean.scan import read_shard_info

    rc = make_rclone(env)
    store = make_store(env, rc)
    cfg = env.cfg
    table = load_table(store, cfg)
    present = present_shards(table)
    done = done_set(store, cfg)  # re-listed here: job outputs are not trusted
    packs = _listed_packs(rc, env)

    with tempfile.TemporaryDirectory(prefix="gpclean-merge-") as tmp_name:
        tmp = Path(tmp_name)
        metas: list[Path] = []
        pack_hashes: dict[str, dict] = {}
        rejected = 0
        for s in present:
            key, shard = s["zipkey"], s["shard"]
            name = pack_name(key, shard)
            if (key, shard) not in done:
                continue
            # A bad checkpoint (its pack missing, not SQLite, no shard_info, another cfg or
            # pack, a pack that does not match the listed hash) makes the shard count as
            # missing, so the bundle is marked partial. It must not abort the whole merge,
            # and it is tombstoned so the next pass (or run) rescans the shard.
            problem = None
            if name not in packs:
                problem = "its thumb pack is not on Drive"
            else:
                local = tmp / "meta" / key / f"{shard:04d}.meta.sqlite"
                store.get(meta_rel(cfg, key, shard), local)
                info = read_shard_info(local)
                if not info or info.get("cfg") != cfg or info.get("pack_name", name) != name \
                        or not info.get("pack_sha256"):
                    problem = "checkpoint unreadable or does not match its table entry"
                elif not pack_matches_listing(info, packs[name]):
                    problem = "thumb pack on Drive does not match its checkpoint"
            if problem:
                log.warning("shard %d: %s; marked for a rescan", s["id"], problem)
                tombstone(store, cfg, key, shard)
                rejected += 1
                continue
            pack_hashes[name] = {"sha256": info["pack_sha256"],
                                 "size": int(info.get("pack_size") or 0) or None}
            metas.append(local)

        missing = len(present) - len(metas)
        if rejected:
            publiclog.event(Ev.COUNT, phase=PHASE_MERGE, rejected=rejected)
        if not metas:
            log.warning("no finished shards: nothing to merge")
            publiclog.event(Ev.COUNT, phase=PHASE_MERGE, shards=0, missing=missing)
            publiclog.github_output(partial=True, missing=missing)
            return EXIT_OK

        out = tmp / "bundle"
        manifest = build_bundle(metas, None, out, MergeConfig(threshold=env.threshold),
                                cfg_hash=cfg, clip_model=env.clip_model,
                                expected_shards=len(present), pack_hashes=pack_hashes)
        # Record which run produced this bundle so tools/get-bundle.ps1 can skip selftest
        # bundles explicitly instead of guessing. The folder is the user's own workflow input.
        manifest["mode"] = env.mode
        if env.mode != "selftest":
            manifest["folder"] = env.folder
        (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                         encoding="utf-8")
        for name in (INDEX_NAME, EMB_NAME, MANIFEST_NAME):  # manifest LAST
            store.put(out / name, f"bundle/{cfg}/{name}")

    partial = bool(manifest.get("partial"))
    missing = int(manifest.get("missing_shards") or 0)
    counts = {k: v for k, v in (manifest.get("counts") or {}).items()
              if type(v) is int and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k)}
    publiclog.event(Ev.COUNT, phase=PHASE_MERGE, shards=len(metas), missing=missing,
                    partial=partial)
    if counts:
        publiclog.event(Ev.COUNT, phase=PHASE_MERGE, **counts)
    publiclog.github_output(partial=partial, missing=missing)
    return EXIT_OK


def cli_merge() -> int:
    """``gpclean ci-merge``: build the bundle from the checkpoints on Drive and upload it."""
    return run_step(PHASE_MERGE, _merge)


# ------------------------------------------------------------------------------ report


def _report(env: CiEnv) -> int:
    e = os.environ
    results = {}
    for key in ("GPCLEAN_PLAN_RESULT", "GPCLEAN_MERGE_RESULT"):
        value = (e.get(key) or "").strip()
        if value not in _JOB_RESULTS:
            raise ValueError(f"{key} is not a job result")
        results[key] = value
    partial = parse_bool(e.get("GPCLEAN_PARTIAL"))
    missing = _env_int(e, "GPCLEAN_MISSING", 0, 0, 10**7)
    quota = any(parse_bool(e.get(f"GPCLEAN_QUOTA{i}")) for i in ("_PLAN", 1, 2, 3))
    # Zips the plan could not read (or that share a name): the bundle lacks them.
    bad_zips = _env_int(e, "GPCLEAN_BAD_ZIPS", 0, 0, 10**7)
    plan_ok = results["GPCLEAN_PLAN_RESULT"] == "success"
    merge_ok = results["GPCLEAN_MERGE_RESULT"] == "success"
    publiclog.event(Ev.COUNT, phase=PHASE_REPORT, plan_ok=plan_ok, merge_ok=merge_ok,
                    partial=partial, missing=missing, quota=quota, bad_zips=bad_zips)
    if quota:
        publiclog.event(Ev.COUNT, phase=PHASE_REPORT, reset_hour_utc=quota_reset_hour_utc())
    if plan_ok and merge_ok and not partial and not quota and not bad_zips:
        publiclog.event(Ev.OK, phase=PHASE_REPORT)
        return EXIT_OK
    publiclog.event(Ev.FAILED, phase=PHASE_REPORT, partial=partial, quota=quota)
    return EXIT_FAIL


def cli_report() -> int:
    """``gpclean ci-report``: turn the run red when the bundle is partial, a zip could not be
    read, or a quota stopped it."""
    return run_step(PHASE_REPORT, _report)


# -------------------------------------------------------------------------------- logs


def _upload_logs(env: CiEnv, store, *, worker: int | None = None) -> int:
    """Copy the private log folder to Drive; returns the number of files uploaded.

    Files are snapshotted first: the logs are still being written (by this very process and
    by rclone), and rclone refuses to upload a file that changes during the copy. The
    rclone config is never uploaded, even if someone put it into the private folder.
    """
    private = env.private_dir
    if private is None or not private.is_dir():
        return 0
    config = env.rclone_config.resolve() if env.rclone_config else None
    dest = _log_dir_rel(env, worker)
    n = 0
    with tempfile.TemporaryDirectory(prefix="gpclean-logs-") as tmp:
        for path in sorted(p for p in private.rglob("*") if p.is_file()):
            rel = path.relative_to(private).as_posix()
            if path.suffix == ".conf" or (config is not None and path.resolve() == config):
                continue
            if any(part.startswith((".", "-")) for part in rel.split("/")):
                continue
            snap = Path(tmp) / f"{n}.snap"
            try:
                shutil.copyfile(path, snap)
                store.put(snap, f"{dest}/{rel}")
                n += 1
            except Exception:
                log.warning("log upload failed for one file", exc_info=True)
            finally:
                snap.unlink(missing_ok=True)
    return n


def cli_upload_logs() -> int:
    """``gpclean ci-upload-logs``: private logs to ``logs/<run>-<attempt>/<job>/``.

    Never fails the job: a missing log upload is not worth a red run.
    """
    def body(env: CiEnv) -> int:
        try:
            _upload_logs(env, make_store(env, make_rclone(env)))
        except Exception:
            log.warning("log upload failed", exc_info=True)
        return EXIT_OK
    return run_step(PHASE_LOGS, body)


# ---------------------------------------------------------------------------- selftest


def _selftest_upload(env: CiEnv) -> int:
    from gpclean.fixtures.generate import generate

    if env.mode != "selftest":
        raise ValueError("selftest-upload runs only with mode=selftest")
    rc = make_rclone(env)
    store = make_store(env, rc)
    with tempfile.TemporaryDirectory(prefix="gpclean-selftest-") as tmp_name:
        tmp = Path(tmp_name)
        generate(tmp, small=True)
        zips = sorted(p for p in tmp.glob("*.zip") if p.is_file())
        store.mkdirs([SELFTEST_DIR])
        for path in zips:
            store.put(path, f"{SELFTEST_DIR}/{path.name}")
    publiclog.event(Ev.COUNT, phase=PHASE_SELFTEST, zips=len(zips))
    return EXIT_OK


def cli_selftest_upload() -> int:
    """``gpclean selftest-upload``: put the synthetic fixture zips into ``selftest/``."""
    return run_step(PHASE_SELFTEST, _selftest_upload)
