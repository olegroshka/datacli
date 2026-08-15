# Google Drive sync — one-time setup

The `gdrive` backend uses Google's installed-app OAuth: you sign in once in the
browser, a refresh token is cached at `~/.datacli/tokens/gdrive.json`, and no
password is ever stored. Google requires every app (even a personal CLI) to
bring its own **OAuth client**, so there is a one-time ~10 minute setup in
Google Cloud Console:

1. **Create a project** — <https://console.cloud.google.com/projectcreate>
   (any name, e.g. `datacli-sync`).
2. **Enable the Drive API** — APIs & Services → Library → *Google Drive API* →
   Enable.
3. **Configure the OAuth consent screen** — APIs & Services → OAuth consent
   screen → External → fill in only the app name + your email.
   Then **Publish app** (move it out of "Testing"). This matters: apps left in
   Testing status get refresh tokens that expire every 7 days, which would force
   a re-login each week. Publishing needs no review for the scope we use.
4. **Create the OAuth client** — APIs & Services → Credentials → Create
   credentials → OAuth client ID → Application type **Desktop app**. Download
   the JSON.
5. **Point datacli at it** (path only — the file stays wherever you put it):

   ```
   data> config set sync-gdrive-secrets C:/Users/you/secrets/client_secret_xxx.json
   # (or outside the shell: uv run python eodhd/cli.py config set sync-gdrive-secrets <path>)
   ```

6. **Install the deps and sign in:**

   ```
   uv sync --extra sync
   data> sync login          # opens the browser once
   data> sync                # offline plan: what a push would do
   data> sync push --run     # upload
   ```

## Scope & privacy

The app requests only `drive.file` — it can see and touch **only the files it
created itself** (the `datacli/eodhd` folder tree it uploads). It cannot read
the rest of your Drive, which is also why Google does not require verification
review for it.

## Where things live

| what | where |
|---|---|
| OAuth client JSON | wherever you saved it; path in git-ignored `datacli.toml` |
| Cached token | `~/.datacli/tokens/gdrive.json` (delete it to force re-login) |
| Push manifest | `<data_root>/.sync/gdrive.json` (what was uploaded, md5s, Drive ids) |
| Remote layout | `My Drive/<remote_root>/<lane>/<file>` mirroring the data root |
