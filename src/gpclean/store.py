"""Storage for pipeline outputs: a local directory or a folder on Drive via rclone.

Both stores address files by *relative POSIX paths* under their root (``work/<cfg>/x.sqlite``).
Relative paths are validated strictly so no caller can climb out of the root, hit another
drive letter, or smuggle an rclone flag: absolute paths, ``..``/``.`` segments, empty
segments, backslashes, colons, control characters and segments starting with ``-`` are all
rejected.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from gpclean.rclone import QuotaError, Rclone, RcloneError

_log = logging.getLogger(__name__)


def check_rel(rel: str, *, allow_root: bool = False) -> str:
    """Validate a relative POSIX path and return it without a trailing slash.

    ``allow_root=True`` accepts ``""`` (the store root itself), used for listing.
    """
    if not isinstance(rel, str):
        raise TypeError("relative path must be a string")
    rel = rel.rstrip("/")
    if rel == "":
        if allow_root:
            return ""
        raise ValueError("empty relative path")
    if rel.startswith("/") or "\\" in rel or ":" in rel:
        raise ValueError("relative path must be POSIX and relative")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in rel):
        raise ValueError("relative path contains control characters")
    for part in rel.split("/"):
        if part in ("", ".", "..") or part.startswith("-"):
            raise ValueError("relative path has an invalid segment")
    return rel


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy via a temp file in the destination directory, then rename over ``dst``.

    A crash mid-copy therefore never leaves a truncated file under the final name, which
    matters because the presence of a meta file is the shard checkpoint.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class LocalStore:
    """A store rooted at a local directory (``run-local`` and tests)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, rel: str, *, allow_root: bool = False) -> Path:
        rel = check_rel(rel, allow_root=allow_root)
        return self.root / rel if rel else self.root

    def exists(self, rel: str) -> bool:
        """True if a file or directory exists at ``rel``."""
        return self._path(rel).exists()

    def list(self, rel_dir: str = "") -> list[str]:
        """Sorted POSIX paths of all files below ``rel_dir`` (recursive), relative to it.

        A missing directory lists as empty.
        """
        base = self._path(rel_dir, allow_root=True)
        if not base.is_dir():
            return []
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*")
                      if p.is_file() and not p.name.startswith(".tmp-"))

    def get(self, rel: str, local: Path) -> None:
        """Copy the stored file ``rel`` to the local path ``local``."""
        _atomic_copy(self._path(rel), Path(local))

    def put(self, local: Path, rel: str) -> None:
        """Store the local file ``local`` at ``rel`` (parents are created)."""
        _atomic_copy(Path(local), self._path(rel))

    def mkdirs(self, rels: list[str]) -> None:
        """Create directories (and parents)."""
        for rel in rels:
            self._path(rel).mkdir(parents=True, exist_ok=True)


class RcloneStore:
    """A store rooted at a remote folder, e.g. ``gp:gpclean-output``.

    Writes go through :class:`gpclean.rclone.Rclone`, which refuses anything outside its
    output root, so ``remote_root`` must be that root or a folder inside it.
    """

    def __init__(self, rclone: Rclone, remote_root: str):
        remote_root = remote_root.rstrip("/")
        if not rclone.is_under_out_root(remote_root):
            raise ValueError("remote_root must be inside the rclone output root")
        self.rclone = rclone
        self.remote_root = remote_root

    def _remote(self, rel: str, *, allow_root: bool = False) -> str:
        rel = check_rel(rel, allow_root=allow_root)
        return f"{self.remote_root}/{rel}" if rel else self.remote_root

    def exists(self, rel: str) -> bool:
        """True if a file or directory exists at ``rel``."""
        return self.rclone.stat(self._remote(rel)) is not None

    def list(self, rel_dir: str = "") -> list[str]:
        """Sorted POSIX paths of all files below ``rel_dir`` (recursive), relative to it.

        A missing directory lists as empty.
        """
        try:
            entries = self.rclone.lsjson(self._remote(rel_dir, allow_root=True),
                                         recursive=True, files_only=True)
        except QuotaError:
            raise
        except RcloneError as exc:
            if exc.returncode == 3:  # directory not found
                return []
            raise
        return sorted(e["Path"] for e in entries if not e.get("IsDir"))

    def get(self, rel: str, local: Path) -> None:
        """Download ``rel`` to ``local`` (atomically, via a temp file next to it)."""
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=local.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            self.rclone.cat_to_file(self._remote(rel), tmp)
            os.replace(tmp, local)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def put(self, local: Path, rel: str) -> None:
        """Upload the local file ``local`` to ``rel``."""
        self.rclone.copyto(str(Path(local)), self._remote(rel))

    def mkdirs(self, rels: list[str]) -> None:
        """Create folders one at a time, parents first.

        Drive allows duplicate folder names, so parallel creation of the same folder would
        race into two folders; creating them sequentially up front avoids that. (rclone
        creates missing parents itself, one process at a time.)
        """
        wanted = {check_rel(rel) for rel in rels}
        for rel in sorted(wanted, key=lambda r: (r.count("/"), r)):
            self.rclone.mkdir(self._remote(rel))
