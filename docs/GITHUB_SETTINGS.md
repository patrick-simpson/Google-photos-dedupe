# GitHub settings (click by click)

These settings keep the public repository safe: only the `main` branch can use your Drive
key, workflows get read-only access, and nothing unexpected can run. It takes about 10
minutes. Do it once, after [SETUP_WINDOWS.md, Part D](SETUP_WINDOWS.md#part-d-give-github-the-drive-key-the-secret).

All steps start on the repository page:
**https://github.com/patrick-simpson/Google-photos-dedupe** > **Settings** (the gear tab at
the top; you must be signed in as the owner).

If a screen looks a little different: GitHub renames menus now and then. Look for the
same words nearby. Older names are given in brackets.

---

## 1. The `photos` environment (main branch only, with the secret)

You created it with two commands in Part D. Now check it:

1. **Settings** > left menu **Environments** > click **photos**.
2. **Deployment protection rules:** leave **Required reviewers** unticked (Claude starts
   runs without waiting for you).
3. **Deployment branches and tags:** it must say **Selected branches and tags**, with one
   rule: **main**.
   - Not there? Pick **Selected branches and tags** in the dropdown, click **Add
     deployment branch or tag rule**, choose **Ref type: Branch**, type `main`, and click
     **Add rule**.
4. **Environment secrets:** it must list **RCLONE_CONFIG_B64**.
   - Not there? Run the D3 command in SETUP_WINDOWS.md again.
5. There must be **no repository secrets**: left menu **Secrets and variables** >
   **Actions** > the **Repository secrets** list is empty. (The secret lives only in the
   environment.)

## 2. Actions permissions

1. **Settings** > left menu **Actions** > **General**.
2. **Actions permissions** (at the top):
   - Choose **Allow patrick-simpson, and select non-patrick-simpson, actions and reusable
     workflows**.
   - **Untick** **Allow actions created by GitHub**.
   - **Untick** **Allow actions by Marketplace verified creators**.
   - In the box **Allow specified actions and reusable workflows**, type exactly:

     ```text
     actions/checkout@*
     ```

   - **Tick** **Require actions to be pinned to a full-length commit SHA**.
   - Click **Save** (right below this section).
3. **Approval for running fork pull request workflows from contributors:**
   - Choose **Require approval for all external contributors** (older name: "all outside
     collaborators").
   - Click **Save**.
4. **Artifact and log retention:** set it to `7` days. Click **Save**.
5. **Workflow permissions** (near the bottom):
   - Choose **Read repository contents and packages permissions**.
   - **Untick** **Allow GitHub Actions to create and approve pull requests**.
   - Click **Save**.

## 3. Security features (should already be on; just check)

1. **Settings** > left menu **Advanced Security** (older name: "Code security and
   analysis", or "Code security").
2. Check that these say **Enabled** (or their button says **Disable**):
   - **Dependabot alerts**
   - **Secret Protection** / **Secret scanning**
   - **Push protection**
3. If one is off, click **Enable** next to it.

## 4. The `main` ruleset (already done)

The ruleset for `main` is already set up: no force-pushes, no deleting `main`, and changes
only through a pull request. To look: **Settings** > **Rules** > **Rulesets**; it should
show one **Active** ruleset for `main`. Nothing to change.

## 5. Your GitHub account

1. **Two-factor authentication:** your profile picture (top right) > **Settings** >
   **Password and authentication**. **Two-factor authentication** must say **Enabled**.
2. **Keep your email private:** **Settings** > **Emails**. Tick **Keep my email addresses
   private** and **Block command line pushes that expose my email**.
   The page shows your private address, like `12345678+your-username@users.noreply.github.com`.
   Tell git to use it (paste in PowerShell, with *your* address from that page):

   ```powershell
   git config --global user.email "12345678+your-username@users.noreply.github.com"
   ```

---

## Merging Claude's changes

Claude works on a separate branch and opens **pull requests** into `main`. **Claude never
merges its own pull requests. You do.** This is a promise, not a technical lock, so please
keep to it:

1. Open the pull request link Claude gives you.
2. Wait until the checks show green ticks.
3. Click **Merge pull request** > **Confirm merge**.

Anything that reaches `main` can use your Drive key, so only merge what Claude asked you to
merge in the chat.

Merging changes the code on GitHub only. When a change affects the app on your PC (the
review site, Claude's photo tools, or the helper scripts), Claude asks you to update it:
see [Updating the app](SETUP_WINDOWS.md#updating-the-app-when-claude-asks-you-to).

## Never do this

- Never click **Re-run jobs** > **Enable debug logging** (or "Re-run with debug
  logging"). Debug logs could print private details into the public log.
- Never add other people as collaborators while the `photos` environment exists.
- Never create the `gpclean-output` folder in Drive by hand. The app makes it itself;
  otherwise the app isn't allowed to write into it.
