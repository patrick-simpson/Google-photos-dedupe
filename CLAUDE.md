# Operating notes for Claude sessions on this repo

This is a one-time Google Photos cleanup for one non-technical user on Windows. Read
`docs/PLAN.md` (why) and `docs/INTERFACES.md` (module contracts) before changing code.

## Hard rules
- The repo is **public**. Never commit or print photos, thumbnails, indexes, filenames, dates,
  GPS, URLs of photos, logs with personal data, or credentials. `tools/check_repo.py` must pass.
- Public CI output is numbers only (`gpclean.publiclog`). Detailed logs go to the user's private
  Drive folder `gpclean-output/logs/`; you cannot read them unless the user shares them.
- Never modify or delete anything in the user's Drive except under `gpclean-output/` (the rclone
  wrapper enforces an allow-list). Nothing may touch the Google Photos library.
- Develop on a feature branch and open a PR into `main`. **Never merge your own PR**: the user
  merges. Drive-touching workflow runs only work from `main` (environment `photos`).
- No Anthropic API keys anywhere. Claude help happens only inside Claude Code / Claude Desktop
  through the local MCP server.

## Running the pipeline (after the user finished docs/SETUP_WINDOWS.md)
Trigger `.github/workflows/pipeline.yml` on `main` (GitHub tools: actions_run_trigger), in order:
1. `mode=selftest` - synthetic photos; proves Drive access, checkpoints and automatic resume.
2. `mode=probe`, `folder=Takeout-test` - speeds, compression, sidecar stats (aggregates only).
3. `mode=full`, `folder=Takeout-test`, `include_albums=true` - small real run; ask the user to
   spot-check 10 "Open in Google Photos" links and a contact sheet in Claude Code and Desktop.
4. `mode=full`, `folder=Takeout` - the real library.
If the Drive token is more than ~5 days old, ask the user to run `tools\refresh-secret.ps1` first
(Testing-mode tokens expire after 7 days). A quota stop (exit 3) needs a re-dispatch after the
UTC reset hour shown in the log. When a bundle is ready, tell the user its `cfg` name and to run
`tools\get-bundle.ps1`.

## Checks before every push
`uv run --frozen pytest -q`, `uv run --frozen python tools/check_repo.py`, and actionlint on
`.github/workflows/*.yml` if you changed workflows. CI runs on Ubuntu and Windows.
