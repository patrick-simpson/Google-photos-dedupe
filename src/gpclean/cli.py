"""Command-line entry point. Every subcommand lazily imports its implementation.

The argument surface here is the contract: implementations live in the modules named in
``_COMMANDS`` and are called with keyword arguments exactly as parsed below.
CI subcommands take their inputs from GPCLEAN_* environment variables (see gpclean.ci).
"""

from __future__ import annotations

import argparse
import importlib
import multiprocessing
import sys
from pathlib import Path

# name -> (module, function)
_COMMANDS: dict[str, tuple[str, str]] = {
    "fixtures": ("gpclean.fixtures.generate", "cli_generate"),
    "run-local": ("gpclean.scan", "cli_run_local"),
    "regroup": ("gpclean.merge.bundle", "cli_regroup"),
    "fetch-model": ("gpclean.clipmodel", "cli_fetch_model"),
    "verify-bundle": ("gpclean.localinit", "cli_verify_bundle"),
    "init": ("gpclean.localinit", "cli_init"),
    "mcp-config": ("gpclean.localinit", "cli_mcp_config"),
    "serve": ("gpclean.site.server", "cli_serve"),
    "mcp": ("gpclean.mcp_server", "cli_mcp"),
    "ci-plan": ("gpclean.ci", "cli_plan"),
    "ci-pending": ("gpclean.ci", "cli_pending"),
    "ci-scan": ("gpclean.ci", "cli_scan"),
    "ci-finalize": ("gpclean.ci", "cli_finalize"),
    "ci-merge": ("gpclean.ci", "cli_merge"),
    "ci-report": ("gpclean.ci", "cli_report"),
    "ci-scope-check": ("gpclean.ci", "cli_scope_check"),
    "selftest-upload": ("gpclean.ci", "cli_selftest_upload"),
    "probe": ("gpclean.probe", "cli_probe"),
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpclean", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("fixtures", help="generate a fake Takeout export with planted cases")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--small", action="store_true", help="fewer distinct filler images (fast tests)")

    s = sub.add_parser("run-local", help="scan + merge a local folder of Takeout zips into a bundle")
    s.add_argument("--zips", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--include-albums", action="store_true")
    s.add_argument("--threshold", type=int, default=3, choices=(2, 3, 4, 5))
    s.add_argument("--clip-model", default="b32", choices=("b32", "b16", "none"))
    s.add_argument("--no-clip", action="store_true", help="same as --clip-model none")
    s.add_argument("--workers", type=int, default=0, help="0 = number of CPUs")
    s.add_argument("--photos-per-shard", type=int, default=1000)

    s = sub.add_parser("regroup", help="redo duplicate grouping/scores in a bundle (no Drive)")
    s.add_argument("--bundle", type=Path, required=True)
    s.add_argument("--threshold", type=int, required=True, choices=(2, 3, 4, 5))

    s = sub.add_parser("fetch-model", help="download + verify pinned CLIP weights")
    s.add_argument("--model", default="b32", choices=("b32", "b16"))

    s = sub.add_parser("verify-bundle", help="check a downloaded bundle against its manifest")
    s.add_argument("bundle", type=Path)

    s = sub.add_parser("init", help="point the local home at a bundle; create review folder")
    s.add_argument("--home", type=Path, required=True)
    s.add_argument("--bundle", type=Path, required=True)

    s = sub.add_parser("mcp-config", help="print ready-to-paste Claude Code / Desktop MCP config")
    s.add_argument("--home", type=Path, required=True)

    s = sub.add_parser("serve", help="run the local review site on 127.0.0.1")
    s.add_argument("--home", type=Path, required=True)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("mcp", help="run the MCP server over stdio (launched by Claude apps)")
    s.add_argument("--home", type=Path, required=True)

    for name in ("ci-plan", "ci-pending", "ci-finalize", "ci-merge", "ci-report",
                 "ci-scope-check", "selftest-upload", "probe"):
        sub.add_parser(name, help="CI step (inputs from GPCLEAN_* env vars)")
    s = sub.add_parser("ci-scan", help="CI scan worker (inputs from GPCLEAN_* env vars)")
    s.add_argument("--worker", type=int, required=True)
    s.add_argument("--of", type=int, required=True, dest="of")
    return p


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = _build_parser().parse_args(argv)
    module_name, func_name = _COMMANDS[args.command]
    func = getattr(importlib.import_module(module_name), func_name)
    kwargs = {k: v for k, v in vars(args).items() if k != "command"}
    rc = func(**kwargs)
    return int(rc or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
