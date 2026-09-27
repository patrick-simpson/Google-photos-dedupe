# Setting up gpclean on Windows (step by step)

This guide walks you through everything once, click by click. Take it slowly; each part
ends with a check so you know it worked. You don't need to understand the technical
details: copy, paste, and compare what you see with what the guide says.

**Time:** about 1 hour, plus waiting for Google Takeout.

**You need:**
- A Windows 10 or 11 PC with about 10 GB free on drive C:
- The Google account that holds your photos
- Your GitHub account (the one that owns `patrick-simpson/Google-photos-dedupe`)
- Notepad, for keeping two codes for a few minutes

**Words used in this guide:**
- **PowerShell**: the Windows window where you paste commands. To open it: click
  **Start**, type `PowerShell`, and click **Windows PowerShell**.
- **Paste a command**: copy the gray box from this page, click in the PowerShell window,
  **right-click** (this pastes), then press **Enter**.
- **Drive**: your Google Drive (drive.google.com).

---

## Part A: Google Cloud (a private "app" that can read your Drive)

Google requires a small "app" before any program can read your Drive. You create your own,
private one. Nobody else can use it.

### A1. Create the project

1. Open https://console.cloud.google.com and sign in with the Google account that has your
   photos.
   - First time here? Pick your country, tick the terms box, and click **Agree and continue**.
2. At the top left, click the project picker (it says **Select a project** or shows a
   project name).
3. In the window that opens, click **New project** (top right).
4. **Project name:** type `gpclean`. Leave **Location** as it is. Click **Create**.
5. Wait about 20 seconds. A notification (bell icon) says it's done. Click **Select
   project** in it, or pick `gpclean` in the project picker.

**Check:** the project picker at the top now shows **gpclean**.

### A2. Turn on the Google Drive API

1. Open https://console.cloud.google.com/apis/library/drive.googleapis.com
2. Make sure the top bar still shows **gpclean**.
3. Click the blue **Enable** button.

**Check:** the page now shows **API Enabled** (or a **Manage** button). Don't enable any
other APIs.

### A3. Set up the sign-in screen (Google Auth Platform)

1. Open https://console.cloud.google.com/auth/overview
2. Click **Get started**.
3. **App information:** App name `gpclean`. User support email: pick your own address.
   Click **Next**.
4. **Audience:** choose **External**. Click **Next**.
5. **Contact information:** type your own email address (for example
   your-name@example.com, but your real one). Click **Next**.
6. **Finish:** tick **I agree to the Google API Services: User Data Policy**. Click
   **Continue**, then **Create**.

### A4. Add yourself as the only test user

1. In the left menu, click **Audience**.
2. **Publishing status** says **Testing**. Leave it that way. **Don't** click "Publish
   app".
3. Under **Test users**, click **+ Add users**.
4. Type the Gmail address that holds your photos. Click **Save**.

**Check:** your address is listed under **Test users**.

### A5. Choose what the app may do (Data access)

1. In the left menu, click **Data access**.
2. Click **Add or remove scopes**. A panel opens on the right.
3. Scroll to the bottom of the panel, to **Manually add scopes**, and paste this line:

   ```text
   https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/drive.file
   ```

4. Click **Add to table**, make sure both new rows are ticked, then click **Update**.
5. Back on the page, scroll down and click **Save**.

**Check:** the page lists `.../auth/drive.readonly` and `.../auth/drive.file` (possibly
under "restricted" or "sensitive" scopes; that's normal).

What these mean: **drive.readonly** = may *look at* files in your Drive, never change
them. **drive.file** = may create and change *only files it made itself* (the
`gpclean-output` folder).

### A6. Create the Desktop client (the app's ID and password)

1. In the left menu, click **Clients**.
2. Click **+ Create client**.
3. **Application type:** choose **Desktop app**. **Name:** `gpclean-desktop`. Click
   **Create**.
4. A window shows your **Client ID** and **Client secret**. Open Notepad and copy both
   into it. **Google shows the secret only this once.**
   - Lost it? Open the client again and click **Add secret** to get a new one.
5. Keep the Notepad window open for Part C. Don't save it to OneDrive, email it, or share
   it.

**Always keep using this one client. Never create a second one.** (Files gpclean makes in
your Drive only work with the client that made them.)

### What to expect later: "Google hasn't verified this app"

When you sign in during Part C, Google shows a warning page: **Google hasn't verified this
app**. That's expected: it's your own private app, and you're its only user.

- Click **Continue**. (If you only see **Back to safety**, click **Advanced** first, then
  **Go to gpclean (unsafe)**.)
- On the next page, tick **both** boxes (see your Drive files; files used with this app),
  then click **Continue**.

---

## Part B: Install the tools on your PC

One script installs everything: Git, GitHub CLI, uv (runs Python for the app), and rclone
1.75.1 (talks to Google Drive). It also downloads the app into `C:\gpclean\app` and the
photo-search model.

1. Open **PowerShell** (Start > type `PowerShell` > **Windows PowerShell**).
2. Paste these two lines (you can paste both at once) and press **Enter**:

   ```powershell
   Invoke-WebRequest https://raw.githubusercontent.com/patrick-simpson/Google-photos-dedupe/main/tools/setup-windows.ps1 -OutFile "$env:TEMP\setup-windows.ps1" -UseBasicParsing
   powershell -ExecutionPolicy Bypass -File "$env:TEMP\setup-windows.ps1"
   ```

3. Windows may ask **Do you want to allow this app to make changes to your device?**
   once or twice (for Git). Click **Yes**.
4. Wait 10-20 minutes. Green **OK** lines appear as each step finishes.

**Check:** the last lines say **ALL DONE** in green.

- Red **SETUP STOPPED** instead? Read the message above it, fix that one thing, and paste
  the second line again. Already finished steps are skipped. See
  [Troubleshooting](#troubleshooting).

5. **Close** the PowerShell window and open a **new** one, so it knows about the new tools.

---

## Part C: Connect your Google Drive (rclone)

This creates the file `C:\gpclean\ci-rclone.conf`: your Drive "key". Keep it private.
Never email, upload, or share it.

### C1. Create the connection

1. In Notepad, below your two codes, paste this line:

   ```powershell
   rclone config create gp drive "client_id=PASTE-CLIENT-ID" "client_secret=PASTE-CLIENT-SECRET" "scope=drive.readonly,drive.file" --config C:\gpclean\ci-rclone.conf
   ```

2. Replace `PASTE-CLIENT-ID` with your Client ID and `PASTE-CLIENT-SECRET` with your Client
   secret. **Keep the quote marks.** There must be no spaces inside the quotes.
3. Copy the finished line from Notepad, paste it into PowerShell, and press **Enter**.
4. A browser window opens. Sign in with your photos account. You'll see **Google hasn't
   verified this app**: click **Continue**, tick both boxes, click **Continue** (see the
   end of Part A).
5. The browser says **Success!** You can close that tab.
6. PowerShell prints a block of settings that includes your token. **Don't copy, share, or
   screenshot it.** Type `cls` and press **Enter** to clear the window.
7. Close Notepad **without saving**.

**Check:** paste this; it lists your top-level Drive folders:

```powershell
rclone lsf gp: --max-depth 1 --config C:\gpclean\ci-rclone.conf
```

### C2. The safety test ("canary"): prove it can't change your files

This proves that gpclean can look at your Drive but **can't change** files it didn't make.

1. Open https://drive.google.com, click **+ New** > **Google Docs** > **Blank document**.
2. Click **Untitled document** (top left), type `gpclean-canary`, press **Enter**, and
   close the tab.
3. In PowerShell, paste:

   ```powershell
   rclone lsf gp: --include "gpclean-canary*" --config C:\gpclean\ci-rclone.conf
   ```

   It should print `gpclean-canary.docx`. (If it prints nothing, wait a minute and try
   again.)
4. Now try to delete it with the app's sign-in. Paste:

   ```powershell
   rclone deletefile gp:gpclean-canary.docx --config C:\gpclean\ci-rclone.conf
   ```

5. **The GOOD result is an error.** You should see a red/ERROR line mentioning **403**,
   **insufficientFilePermissions**, or **permission denied**. That means it works as it
   should.
   - **No error, and the document disappeared from Drive?** Stop here and tell Claude
     "the canary delete succeeded". The sign-in has more access than it should. Don't
     continue until it's fixed.
6. In the browser, delete the test document yourself: in Drive, right-click
   **gpclean-canary** > **Move to trash**.

---

## Part D: Give GitHub the Drive key (the secret)

The pipeline on GitHub needs the Drive key. It's stored as an encrypted **secret** in a
protected **environment** called `photos`. Only the `main` branch can use it.

### D1. Sign in to GitHub from PowerShell

1. Paste:

   ```powershell
   gh auth login
   ```

2. Answer the questions with the arrow keys and **Enter**:
   - **Where do you use GitHub?** > `GitHub.com`
   - **What is your preferred protocol?** > `HTTPS`
   - **Authenticate Git with your GitHub credentials?** > `Yes`
   - **How would you like to authenticate?** > `Login with a web browser`
3. It shows a code like `ABCD-1234`. Press **Enter**; a browser opens. Type the code, click
   **Continue**, then **Authorize github**.

**Check:** PowerShell says **Logged in as** your GitHub name.

### D2. Create the `photos` environment (main branch only)

Paste these two lines, one at a time:

```powershell
gh api -X PUT repos/patrick-simpson/Google-photos-dedupe/environments/photos -F "deployment_branch_policy[protected_branches]=false" -F "deployment_branch_policy[custom_branch_policies]=true"
```

```powershell
gh api -X POST repos/patrick-simpson/Google-photos-dedupe/environments/photos/deployment-branch-policies -f name=main -f type=branch
```

Each prints a block of text starting with `{`. That's fine.

### D3. Upload the secret

Paste this one line (it reads the key file and sends it straight to GitHub without showing
it):

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\gpclean\ci-rclone.conf")) | gh secret set RCLONE_CONFIG_B64 --env photos --repo patrick-simpson/Google-photos-dedupe
```

**Check:** it says **Set Actions secret RCLONE_CONFIG_B64** for the repository.

### D4. GitHub settings

Now follow [GITHUB_SETTINGS.md](GITHUB_SETTINGS.md) (about 10 minutes). It also shows
where to double-check the environment you just created.

---

## Part E: Google Takeout (the copy of your photos)

Takeout makes a copy of your photos as `.zip` files in your Drive. gpclean reads that copy;
your library itself is never touched.

**First a small test export.** It checks the whole process on real data in about an hour,
before the big export (which can take a day or more).

### E1. The test export

1. Open https://takeout.google.com
2. Under **Select data to include**, click **Deselect all**.
3. Scroll down to **Google Photos** and tick its box.
4. Under Google Photos, click the button **All photo albums included**. A list opens.
5. Click **Deselect all**. Then tick:
   - one or two **Photos from 20XX** entries (pick an older, quieter year, for example
     `Photos from 2015`), and
   - **one** album.
   Aim for a small test (under about 2 GB). Click **OK**.
6. Scroll to the very bottom and click **Next step**.
7. Choose:
   - **Destination / Transfer to:** `Add to Drive`
   - **Frequency:** `Export once`
   - **File type:** `.zip` (never .tgz)
   - **File size:** `50 GB`
8. Click **Create export**.
9. Google emails you when it's ready (often within an hour for a small test).
10. When it's ready, open https://drive.google.com. There is a new folder named
    **Takeout**. **Rename it now:** right-click **Takeout** > **Rename** > type
    `Takeout-test` > **OK**.
    (Takeout always uses the name "Takeout", so renaming keeps the test and the full export
    apart.)
11. Tell Claude: "The test Takeout is in Drive as Takeout-test."

### E2. The full export (later, when Claude says the test looked good)

Repeat E1, but in step 4-5 **don't** open **All photo albums included**: leave everything
selected. Still **Google Photos only**, **.zip**, **50 GB**, **Add to Drive**.

- A big library takes hours to a few days, and arrives as several 50 GB parts in a new
  **Takeout** folder. Leave that folder and its files exactly as they are: don't rename,
  move, or open them.
- Tell Claude when the email says it's done.

---

## Part F: Let Claude run it, then review on your PC

### What Claude does next

Claude starts the pipeline on GitHub: first a short **probe** (a speed and settings test),
then the full run on `Takeout-test`, and later on `Takeout`. You'll get a summary in the
chat. You don't need to watch it. Claude may open small code changes (pull requests)
for you to merge; see [GITHUB_SETTINGS.md](GITHUB_SETTINGS.md#merging-claudes-changes).

### Download the results and open the review site

When Claude says the bundle is ready:

1. Open **PowerShell** and paste:

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\gpclean\app\tools\get-bundle.ps1
   ```

   It downloads the newest bundle into a new folder under `C:\gpclean\bundle\`, checks
   every file, and switches gpclean to it. **Check:** it ends with **ALL DONE** in green.
2. Start the review site. Paste these two lines:

   ```powershell
   cd C:\gpclean\app
   uv run gpclean serve --home C:\gpclean
   ```

3. Your browser opens **http://127.0.0.1:8765** (the site runs only on your PC). Keep the
   PowerShell window open while you review. To stop the site, click the window and press
   **Ctrl+C**.
4. First job: open the **Duplicates** tab and look at about 30 groups by eye. If any group
   holds photos that are *not* the same, tell Claude.
5. Want Claude's help sorting? See [MCP_GUIDE.md](MCP_GUIDE.md).

Next time, you only need step 2. After a new pipeline run, do step 1 again (your "To
delete" list is kept).

---

## The 7-day sign-in: refresh it before it runs out

Because the Google app stays in **Testing** mode, Google ends the Drive sign-in after
**7 days**. Refresh it when it's about **5 days old**, before asking Claude for a run and
before downloading a bundle:

```powershell
powershell -ExecutionPolicy Bypass -File C:\gpclean\app\tools\refresh-secret.ps1
```

A browser opens: sign in with the **same** Google account, click **Continue** on the
warning page, tick both boxes, and click **Continue**. The script sends the renewed key
to GitHub. Then tell Claude: "I refreshed the secret, please run the probe." Claude runs
a quick check; a green **scope check** step confirms the new sign-in works.

Always keep using the same `gpclean-desktop` client. Never create a second one.

---

## Troubleshooting

**"winget is not recognized" / "winget is missing"**
Open the **Microsoft Store**, search for **App Installer**, click **Get** or **Update**.
Then run Part B again.

**"running scripts is disabled on this system"**
Use the exact commands in this guide; they start with
`powershell -ExecutionPolicy Bypass -File`, which allows just that one script.

**"uv" / "rclone" / "gh" / "git" is not recognized**
Close PowerShell and open a new window. Still wrong? Run Part B again.

**Setup stopped at "uv sync" or "fetch-model"**
Usually a network hiccup. Run the second line of Part B again; it continues where it
stopped.

**Google says "Access blocked: gpclean has not completed the Google verification process"**
You aren't listed as a test user, or you signed in with a different account. Redo A4 with
the exact address you sign in with.

**Google says "Error 400: redirect_uri_mismatch" or "invalid_client"**
The client isn't a **Desktop app**, or the ID/secret was mistyped. Open **Google Auth
Platform > Clients**, click **gpclean-desktop**, and copy the **Client ID** again. For the
secret, click **Add secret** on that **same** client. Then redo C1. Don't create a second
client: files gpclean already made in Drive only work with the client that made them. If
you really need a new client, tell Claude first.

**rclone says "invalid_grant", "token expired", or "couldn't fetch token"**
The 7-day sign-in ran out. Run `refresh-secret.ps1` (see above).

**"The canary delete succeeded" (the test document was deleted)**
Stop and tell Claude. Don't upload the secret or run anything until it's fixed.

**get-bundle says "No finished bundle found on Drive yet"**
The pipeline hasn't finished. Wait until Claude says the run is done.

**get-bundle says the bundle is incomplete or damaged**
Run get-bundle again; it starts a fresh folder. Old folders under `C:\gpclean\bundle\` can
be deleted once the new one works.

**The site says "No bundle configured"**
Run get-bundle (Part F, step 1) first.

**The site doesn't open, or says the port is in use**
Maybe it's already running in another PowerShell window. Or start it on another port:
`uv run gpclean serve --home C:\gpclean --port 8766`, then open http://127.0.0.1:8766

**Windows Defender or antivirus warns about a file**
The app and model files are downloaded from GitHub, PyPI, and Hugging Face and checked
against fixed checksums. If a warning names a file under `C:\gpclean`, tell Claude the exact
message.

**Where things are on your PC**
- `C:\gpclean\app`: the app
- `C:\gpclean\bundle\...`: downloaded bundles
- `C:\gpclean\state`: your "To delete" list and logs
- `C:\gpclean\review`: the folder for Claude Code reviews
- `C:\gpclean\ci-rclone.conf`: your Drive key (private!)
- `%USERPROFILE%\.cache\huggingface\gpclean`: the photo-search model (about 600 MB)
- `%LOCALAPPDATA%\uv\cache`: uv's download cache (several GB; `uv cache clean` empties it)

Keep all of it outside OneDrive. `C:\gpclean` is outside OneDrive by default.
