# Using Claude to help review your photos

Claude can look through your photos with you and **suggest** which ones to delete. It
connects to gpclean through **MCP** (a standard way for Claude apps to use local tools).

What Claude **can** do: see your library's numbers, search it, look at contact sheets
(up to 48 small previews in one picture) and single photos, and **propose** items for
your "To delete" list, with a reason.

What Claude **can't** do: approve, reject, or delete anything, or change your Google
Photos. Every proposal waits for you in the review site (**To delete** > **Proposed**).

You can use **Claude Code** (a terminal app), **Claude Desktop**, or both. Set up
gpclean first ([SETUP_WINDOWS.md](SETUP_WINDOWS.md), through Part F).

This guide sets up the **photo-review chat**. It is not the **project chat** (the Claude
Code chat on https://claude.ai/code that runs the pipeline and opens pull requests; see
[Two Claudes](../README.md#two-claudes-which-chat-to-use)). Keep photo details in the
review chat only: never paste them into the project chat, which works on a public
repository.

---

## Step 1: Get your personal setup text

1. Open **PowerShell** (Start > type `PowerShell` > **Windows PowerShell**).
2. Paste these two lines:

   ```powershell
   cd C:\gpclean\app
   uv run gpclean mcp-config --home C:\gpclean
   ```

3. It prints two parts: **1) Claude Code** (one long line starting with
   `claude mcp add gpclean`) and **2) Claude Desktop** (a block starting with `{`). Keep
   this window open; you'll copy from it.

---

## Connect Claude Code

If you don't have Claude Code yet: install it following
https://code.claude.com/docs/en/setup (on Windows you paste one line into PowerShell:
`irm https://claude.ai/install.ps1 | iex`), then run `claude` once and sign in with your
Claude account.

1. Open a **new** PowerShell window and go to the review folder:

   ```powershell
   cd C:\gpclean\review
   ```

   (This folder was made by gpclean. Its settings switch off Claude's web, shell, and file
   tools, so only the photo tools work there.)
2. Copy the **1) Claude Code** line from Step 1, paste it here, and press **Enter**.
   It looks like this (your paths may differ):

   ```powershell
   claude mcp add gpclean --scope local -e HF_HUB_OFFLINE=1 -- 'C:\gpclean\app\.venv\Scripts\gpclean.exe' mcp --home 'C:\gpclean'
   ```

   **Check:** it says **Added stdio MCP server gpclean** ... **to local config**.
3. Start Claude Code in the same folder:

   ```powershell
   claude
   ```

   If it asks **Do you trust the files in this folder?**, choose **Yes, proceed**.
4. Type `/mcp` and press **Enter**. **gpclean** should show as **connected**. Press
   **Esc**.
5. Type: `show me gpclean stats`

**Check:** Claude answers with totals (photos, duplicates, junk categories, and so on).

**Every time you review with Claude Code:** open PowerShell, then

```powershell
cd C:\gpclean\review
claude
```

The photo tools only exist in that folder. (That's on purpose: other Claude Code sessions
can't see your photos.)

---

## Connect Claude Desktop

1. Open **Claude Desktop**.
2. Open **Settings**: click your name or initials at the bottom left, then **Settings**
   (or use the menu **File** > **Settings**).
3. In the left list, under **Desktop app**, click **Developer**, then click **Edit Config**.
   - No **Developer** item? Use the menu **Help** > **Troubleshooting** > **Enable
     Developer Mode**, then try again.
4. A File Explorer window opens with the file **claude_desktop_config.json** selected.
   Right-click it > **Open with** > **Notepad**.
5. Paste the block from **2) Claude Desktop** in Step 1:
   - **File is empty or only has `{}`:** select everything (**Ctrl+A**) and paste the
     whole block.
   - **File already has an `"mcpServers"` section:** paste only the `"gpclean": { ... }`
     part inside it, and put a comma between it and the entry before it.
   - **File has other settings (for example `"preferences"`) but no `"mcpServers"`:** put
     your cursor right after the very first `{`, paste only the `"mcpServers": { ... }`
     part, and type a comma after its closing `}`.
   - The block looks like this (note the **double** backslashes, which are needed):

     ```json
     {
       "mcpServers": {
         "gpclean": {
           "command": "C:\\gpclean\\app\\.venv\\Scripts\\gpclean.exe",
           "args": ["mcp", "--home", "C:\\gpclean"],
           "env": {"HF_HUB_OFFLINE": "1"}
         }
       }
     }
     ```

6. **File** > **Save**, and close Notepad.
7. **Fully quit** Claude Desktop: right-click the Claude icon near the clock (bottom right;
   click **^** if you don't see it) > **Quit**. Then start Claude Desktop again.
8. Start a new chat. Click the **+** button at the bottom left of the message box, then
   **Connectors** (older versions: the slider icon **Search and tools** below the message
   box). **gpclean** should be listed and switched on.
9. Type: `show me gpclean stats`. When Claude asks to use a gpclean tool, click **Allow
   always** (the tools can only read and propose).

**Check:** Claude answers with totals. If Claude Desktop shows an error about gpclean,
see [Troubleshooting](#troubleshooting).

---

## Privacy tips

Before your first review chat, take two minutes for these:

- **Turn off other connectors and web search in review chats.** In Claude Desktop: in each
  photo-review chat, click the **+** button at the bottom left of the message box. Switch
  off **Web search** there, then open **Connectors** and switch off every other connector
  (Gmail, Google Drive, Calendar, ...), leaving only **gpclean**. (Older versions: the
  slider icon **Search and tools**.) Text inside a photo could try to trick Claude; with
  nothing else switched on, there's nothing else it can reach. (Claude Code's review folder already blocks web, shell, files, and claude.ai
  connectors.)
- **Check your Claude privacy setting.** On claude.ai or in Claude Desktop: **Settings** >
  **Privacy** > **Help improve Claude**. If it's on, your chats may be used to train
  models and kept longer. Many people turn it off for these chats.
- **What Claude sees:** previews, file names, dates, GPS locations, names of people Google
  recognized, captions, and any text inside screenshots. You can say "don't mention
  locations or names" at the start of a chat.
- **When you're finished,** delete the photo-review chats (see the teardown checklist in
  the [README](../README.md#teardown-checklist-when-you-are-completely-finished)).
- When you're not reviewing, you can switch **gpclean** off in **+** > **Connectors**
  (older versions: **Search and tools**).

---

## Tips for getting the most out of Max 5x

Looking at pictures uses more of your Claude usage than text does. These habits keep it low:

1. **Start with stats.** "show me gpclean stats" tells you where the big piles are.
2. **Narrow down with search first.** Searches return short text rows (cheap). Only then
   ask for contact sheets of the results.
3. **One category per chat.** For example: screenshots in one chat, blurry photos in the
   next, burst extras in another.
4. **Start a fresh chat every 10-15 contact sheets.** Long chats get slower and less
   accurate. Claude Code: type `/clear`. Claude Desktop: click **New chat**.
5. **Let the site do the easy bulk work.** Exact duplicates and obvious categories are
   faster to approve in the review site yourself. Use Claude for the unclear cases.
6. **Pick a lighter model for sorting.** Sonnet uses less of your limit than Opus and is
   good at this.
7. **Watch the "To delete" > "Proposed" tab** in the review site. New proposals appear
   there within seconds, with Claude's reason. Approve or reject them there.

---

## Example requests

Copy any of these into a chat. Change years and words to suit you.

1. "show me gpclean stats"
2. "find screenshots of text conversations older than 2022 and propose the ones that are clearly just chats"
3. "show me blurry photos from 2019 and queue the ones that are clearly accidental"
4. "search 'receipt' before 2023 and show me a contact sheet"
5. "review burst extras from 2022; propose the extra frames when the best shot is clearly sharper"
6. "what's in my queue and why?"
7. "show me the darkest photos with camera data (pocket shots) and propose only the obvious ones"
8. "find memes and images saved from WhatsApp or Facebook before 2020"
9. "show me tiny images (under 480 pixels) and tell me which ones look like junk"
10. "search 'whiteboard' and 'document' photos from 2018 and let me decide on each one"
11. "show me duplicate groups that are not exact copies, one contact sheet at a time"
12. "find photos of parking spots or parking signs and propose them"
13. "show me overexposed (very bright) photos from 2021; skip anything with people in it"
14. "withdraw your proposals for anything marked as a favorite"
15. "list what you proposed today and group it by reason"

Good habits when asking:

- Say **"propose"** or **"queue"** when you want suggestions added; otherwise Claude just
  shows you.
- Ask Claude to **explain** a proposal ("why did you pick cell 12?").
- Claude only proposes. Nothing is deleted until **you** approve it in the site and delete
  it in Google Photos.

---

## Troubleshooting

**`/mcp` in Claude Code doesn't list gpclean**
You started `claude` in a different folder. Close it, run `cd C:\gpclean\review`, and start
`claude` again. Still missing? Paste the `claude mcp add ...` line again in that folder.

**"claude mcp add" says something about unknown options or a missing command**
If you installed Claude Code with npm, PowerShell can swallow the two dashes. Put quotes
around them: replace ` -- ` with ` '--' ` in the line and try again.

**Claude Desktop says the gpclean server failed or disconnected**
- Check the JSON: every backslash must be doubled (`C:\\gpclean`), and all quotes must be
  straight quotes (`"`), not curly ones. Paste the block again from `gpclean mcp-config`.
- Make sure you fully quit Claude Desktop (tray icon > **Quit**) and restarted it.
- The server writes a log to `C:\gpclean\state\logs\`. If it still fails, tell Claude (in
  the project chat) the error message shown in **Settings** > **Developer**.

**Claude says "No review bundle is configured yet"**
Download a bundle first: [SETUP_WINDOWS.md, Part F](SETUP_WINDOWS.md#part-f-let-claude-run-it-then-review-on-your-pc).

**After downloading a new bundle**
Claude switches to the new bundle by itself on its next request. Photo numbers (ids) from
before the switch no longer match, so Claude is told to search again; the simplest is to
start a new chat. If Claude still sees the old one, restart Claude Code (type `/exit`, then
`claude`) or fully quit and reopen Claude Desktop.

**Text search says it isn't available**
- It says the CLIP weights are not downloaded: the search model is missing. Run
  `cd C:\gpclean\app` and then `uv run gpclean fetch-model --model b32`, then ask again.
- It says the CLIP libraries are not installed: run the setup again
  ([Updating the app](SETUP_WINDOWS.md#updating-the-app-when-claude-asks-you-to)), then
  restart Claude (see above).
- It says the bundle was built without CLIP embeddings: that bundle has no text search.
  Filters (category, year, dates, file names) still work.
