"""Local (Windows/PC) home folder layout and setup commands.

Home layout (e.g. C:\\gpclean):
  state/config.json    -> {"bundle": "<absolute path of the current bundle directory>"}
  state/review.sqlite  -> the shared "To delete" queue (review_db.ReviewDB)
  state/logs/          -> rotating logs for the site and MCP server
  review/              -> Claude Code working folder (.claude/settings.json denies risky tools)
  bundle/<cfg>-<stamp>/ -> downloaded bundles (never overwritten in place)

``home_paths`` and ``current_bundle`` are the stable API used by the site and MCP server.
"""

from __future__ import annotations

import json
from pathlib import Path


def home_paths(home: Path) -> dict[str, Path]:
    """Return the well-known paths under the local home folder (nothing is created)."""
    home = Path(home).expanduser().resolve()
    state = home / "state"
    return {
        "home": home,
        "state_dir": state,
        "config_json": state / "config.json",
        "review_db": state / "review.sqlite",
        "logs_dir": state / "logs",
        "review_dir": home / "review",
        "bundles_dir": home / "bundle",
    }


def current_bundle(home: Path) -> Path:
    """Return the bundle directory recorded by ``gpclean init``; raise with a helpful message."""
    cfg = home_paths(home)["config_json"]
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        bundle = Path(data["bundle"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FileNotFoundError(
            f"No bundle configured under {cfg}. Run: gpclean init --home <home> --bundle <bundle dir>"
        ) from exc
    if not (bundle / "index.sqlite").is_file():
        raise FileNotFoundError(f"Configured bundle has no index.sqlite: {bundle}")
    return bundle
