# gpclean: a one-time Google Photos cleanup

gpclean helps you clean up a big Google Photos library (tens of thousands of photos) once:

- It finds **duplicates**: photos that look the same, even if one copy is smaller or was
  re-saved.
- It finds likely **junk**: screenshots, blurry or dark shots, pocket shots, extra burst
  frames, and images saved from chat apps.
- It gives you a **review website** on your own PC, plus an optional **Claude helper**, so
  you can decide what goes.
- **You** do the actual deleting in Google Photos. gpclean can't delete or change anything
  in your Google account. It only reads a copy of your photos.

The heavy work (looking at every photo) runs on GitHub's free servers, straight from a
Google Takeout copy saved in your Google Drive. Your PC never has to download the whole
export.

> This repository is **public**. It never contains your photos, thumbnails, file lists,
> logs with personal data, or passwords/tokens. Everything personal stays in your Google
> Drive and on your PC.

## The big picture

1. **One-time setup** (about 1 hour, click-by-click): a small Google Cloud project, the
   tools on your Windows PC, and a connection to your Google Drive.
   See [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md).
2. **GitHub settings** (10 minutes): see [docs/GITHUB_SETTINGS.md](docs/GITHUB_SETTINGS.md).
3. **Ask Google for a Takeout export** of your photos, saved to Drive: first a small test
   export, later the full one ([SETUP_WINDOWS.md, Part E](docs/SETUP_WINDOWS.md#part-e-google-takeout-the-copy-of-your-photos)).
4. **Claude runs the pipeline** on GitHub. It reads the export and writes a "review
   bundle" (small previews plus a list of findings) into a new Drive folder,
   `gpclean-output`.
5. **You download the bundle** to your PC with one command and open the review website.
6. **You review**: approve or reject each suggestion. Claude can help sort through the
   unclear cases, but it can only *suggest*. See [docs/MCP_GUIDE.md](docs/MCP_GUIDE.md).
7. **You delete** the approved photos in Google Photos, guided by the review site.
8. **You follow the cleanup order below**, then the **teardown checklist**.

## Documents

| Document | What it covers |
|---|---|
| [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md) | Everything you set up once: Google Cloud, PC tools, Drive connection, GitHub secret, Takeout, and downloading and opening the review site |
| [docs/GITHUB_SETTINGS.md](docs/GITHUB_SETTINGS.md) | The GitHub settings that keep the repository safe |
| [docs/MCP_GUIDE.md](docs/MCP_GUIDE.md) | Connecting Claude (Claude Code and Claude Desktop), privacy tips, and example requests |
| [docs/PLAN.md](docs/PLAN.md) | The full technical plan (for the curious) |

## Before you start

- **Drive space:** free Google Drive space of at least the export size plus about 6 GB.
  (The export is stored in your Drive; the review bundle is 1-5 GB.)
- **PC space:** about 10 GB free on drive C: (app, search model, and review bundle).
- **Export format:** always choose **.zip**, never .tgz.

## Cleanup order (important: follow it in this order)

Some steps can't be undone. Do them in exactly this order:

**(a) Your Takeout export is your only full-quality copy.** Until you finish step (e),
don't delete the `Takeout` folder in Drive.

**(b) Review, then delete in Google Photos.** Use the review site's "To delete" list.
Deleted items go to the Google Photos trash, which keeps them for **30 days**. After that
they are gone for good. (Google shortened the trash period in September 2026, so older
guides may show a longer time.)

**(e) Decide whether you want an offline copy of your originals, and make it now.**
If you want one, download every part of the Takeout export from Drive to an external drive,
then check each part. For each `.zip` file, run this in PowerShell (replace the path):

```powershell
cd C:\gpclean\app
uv run python -m zipfile -t "E:\Takeout\takeout-20260901T120000Z-001.zip"
```

It must print exactly one line: `Done testing`. If you see the word "corrupted" or any
red error text above it, download that part again.
Only continue when every part passes.

**(c) Optional: convert existing photos to Storage saver.** In photos.google.com, click
the gear (Settings) > **Manage storage** > **Recover storage** > **Convert**. This shrinks
your existing photos to save space. **It can't be undone.** gpclean doesn't do this for
you; it's listed here so you do it only after (e).

**(d) Optional: switch backup quality to Storage saver** on every phone and tablet
(Google Photos app > your profile picture > Photos settings > Backup > Backup quality).

## Teardown checklist (when you are completely finished)

Tick these off one by one:

- [ ] In Google Drive, delete the folders `Takeout`, `Takeout-test` and `gpclean-output`,
      then open **Trash** (left side) and click **Empty trash**.
- [ ] Remove the app's access: go to https://myaccount.google.com/connections, click
      **gpclean**, then **Delete all connections** (or "Remove access").
- [ ] Delete the Google Cloud project: https://console.cloud.google.com > pick the gpclean
      project > **IAM & Admin** > **Settings** > **Shut down**.
- [ ] On GitHub: repository **Settings** > **Environments** > **photos** > **Delete
      environment** (this also deletes its secret).
- [ ] On GitHub: **Actions** tab > open each workflow run > **...** > **Delete workflow run**.
- [ ] In PowerShell: `gh auth logout`
- [ ] In PowerShell, inside `C:\gpclean\review`: `claude mcp remove gpclean`
- [ ] In Claude Desktop: Settings > Developer > Edit Config, remove the `"gpclean"` block,
      save, and restart Claude Desktop.
- [ ] Delete your photo-review chats in Claude Desktop and on claude.ai, and Claude Code's
      history for the review folder: the folder `%USERPROFILE%\.claude\projects\C--gpclean-review`.
- [ ] Delete the folder `C:\gpclean` (this removes the app, the bundle, the review list, and
      the Drive sign-in file).
- [ ] Optional: delete the folder `%USERPROFILE%\.cache\huggingface\gpclean` (the
      photo-search model) and run `uv cache clean` to free disk space.
- [ ] Optional: delete the GitHub repository (Settings > General > Danger Zone).

## Privacy: what leaves your computer

Please read this once. It explains exactly where your data goes.

**GitHub (the pipeline).** GitHub's servers read your Takeout zips from Drive, look at
each photo, and write the results into `gpclean-output` in your Drive. Photos exist
there only in memory and temporary disk space that is wiped after each job. The public
job logs show **numbers only** (counts, durations), never file names or places. Because
the repository is public, anyone can see *that* a run happened and how long it took.

**Google.** The Drive sign-in (valid 7 days at a time) lets the pipeline **read all of your
Google Drive** (Google has no "read only the Takeout folder" option) and **write only
files the app itself created** (the `gpclean-output` folder). A test during setup proves
it can't change anything else.

**Anthropic (only if you use Claude with gpclean).** When Claude looks at your photos, the
following go into your Claude chats: small previews and contact sheets, file names, dates,
GPS locations, names of people Google recognized, photo descriptions, and any text visible
in screenshots (receipts, chats, documents). They are kept according to your Claude
privacy settings (see [MCP_GUIDE.md](docs/MCP_GUIDE.md#privacy-tips)). You can't take them
back except by deleting those chats.

**Stays on your PC.** The review bundle, the review website (it only answers your own PC,
at `127.0.0.1`), and the "To delete" list.

**Never.** No Anthropic API key is used anywhere. Nothing is uploaded to the repository.
gpclean never deletes, moves, or edits anything in your Google Photos or your existing
Drive files.

**Risks you accept by using it:**

1. A software package that was harmful when it was pinned could read your whole Drive for
   up to 7 days (the sign-in lifetime).
2. Anyone who can change the `main` branch controls the Drive sign-in. Only you merge
   changes; Claude never merges its own changes.
3. The "numbers only" logs rely on the code being correct. Never use GitHub's
   **"Re-run with debug logging"** option: it could print private details into the public
   log.
4. Text inside photos or captions could try to trick Claude ("prompt injection"). Claude can
   only suggest; you approve every single item, and review chats are kept separate.
5. Some steps can't be undone: deleting shared or partner photos also affects the other
   people; the trash only lasts 30 days; the Storage saver conversion is permanent; and
   deleting the Takeout without an offline copy loses your originals.

---

## For developers

Everything below is for people working on the code.

**Requirements:** [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you).

```bash
uv sync --locked --group dev --group mcp        # add --group clip for real CLIP search
uv run pytest -q                                # all tests (synthetic data only)
uv run python tools/check_repo.py               # repo hygiene + workflow lint
git config core.hooksPath .githooks             # run the hygiene check before each commit
```

Try the whole thing locally on fake photos:

```bash
uv run gpclean fixtures --out /tmp/fx --small
uv run gpclean run-local --zips /tmp/fx --out /tmp/fx-bundle --no-clip
uv run gpclean verify-bundle /tmp/fx-bundle
uv run gpclean init --home /tmp/gphome --bundle /tmp/fx-bundle
uv run gpclean serve --home /tmp/gphome
```

**Layout**

| Path | What |
|---|---|
| `src/gpclean/cli.py` | Command-line entry point (`gpclean <command>`) |
| `src/gpclean/takeout/` | Reading Takeout zips (range reads), member names, sidecar JSON pairing |
| `src/gpclean/imaging/` | Decoding, fingerprints (SHA-256, pHash, pixel signature), features, thumbnails |
| `src/gpclean/scan.py`, `scanworker.py` | Scanning shards of a zip into meta + thumbnail packs |
| `src/gpclean/merge/` | Pairing, collapsing copies, duplicate groups, bursts, junk scores, the bundle |
| `src/gpclean/bundle_read.py`, `search.py` | Reading a bundle; filtered and CLIP text search |
| `src/gpclean/review_db.py` | The local "To delete" queue (the only writer) |
| `src/gpclean/site/` | The local review website (127.0.0.1 only) |
| `src/gpclean/mcp_server.py`, `sheet.py` | The MCP server for Claude, contact sheets |
| `src/gpclean/localinit.py` | `init`, `mcp-config`, `verify-bundle` |
| `src/gpclean/ci.py`, `probe.py`, `.github/workflows/` | The GitHub Actions pipeline |
| `src/gpclean/rclone.py` | The only code that runs rclone (verb allowlist) |
| `tools/` | Windows helper scripts, tool installers, `check_repo.py` |
| `docs/` | User guides, the plan (`PLAN.md`) and module contracts (`INTERFACES.md`) |

Rules for contributors: synthetic test data only; library code logs through `logging`
(only `gpclean.publiclog` writes to the CI console, and only numbers); no pickle, no eval;
never extract zip members to disk by name; must work on Windows and Linux.
