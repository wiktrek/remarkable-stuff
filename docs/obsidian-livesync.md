# Syncing a reMarkable Paper Pro with Obsidian (Self-hosted LiveSync / CouchDB)

## How it works

The tablet can't talk to CouchDB directly, so rmbridge sits in the middle and
does both halves itself. You only fill in `.env`.

```
 Obsidian (any device)
      ⇅  Self-hosted LiveSync plugin
 CouchDB  (your server)
      ⇅  livesync-bridge, run inside rmbridge         ← live, both ways
 rmbridge's copy of <vault>/reMarkable/
      ⇅  rmbridge vault sync                          ← every SYNC_INTERVAL s, both ways
 reMarkable cloud  →  /Obsidian on the Paper Pro
```

rmbridge starts [livesync-bridge](https://github.com/vrtmrz/livesync-bridge)
(a LiveSync client without the Obsidian UI, by the LiveSync author) as a child
process. It writes the bridge's config from your `.env`, restarts it if it
crashes, and shows its status in the footer of the web UI. Only the
`reMarkable/` folder of your vault is mirrored. Everything else stays in
CouchDB.

| You do this…                                                     | …and this happens                                                     |
| ---------------------------------------------------------------- | --------------------------------------------------------------------- |
| Drop a PDF/EPUB into `reMarkable/Outbox/` in Obsidian            | It appears on the tablet in `/Obsidian/`                              |
| Write a Markdown note in `reMarkable/Outbox/`                    | It's converted to EPUB and appears on the tablet                      |
| Annotate any doc / write a notebook in `/Obsidian` on the tablet | `Name.pdf` (with your ink) + `Name.md` appear in `reMarkable/Inbox/`  |

## Setup

### 1. Get your LiveSync settings from Obsidian

In Obsidian, open **Settings → Self-hosted LiveSync** and copy these:

| Plugin setting                                       | `.env` variable                 |
| ---------------------------------------------------- | ------------------------------- |
| Remote Database → URI                                | `LIVESYNC_URL`                  |
| Remote Database → Database name                      | `LIVESYNC_DATABASE`             |
| Remote Database → Username / Password                | `LIVESYNC_USERNAME` / `LIVESYNC_PASSWORD` |
| E2EE passphrase (if *End-to-end encryption* is on)   | `LIVESYNC_PASSPHRASE`           |
| Same passphrase, if *Path obfuscation* is on         | `LIVESYNC_OBFUSCATE_PASSPHRASE` |
| **Show current recovery code** (`sls-id-v1:…`), newer E2EE vaults only | `LIVESYNC_ID_KEY`  |

Use a URI the **NAS can reach**. If CouchDB runs on the same Docker host,
`http://localhost:5984` won't work from inside the container; use the host's
LAN IP or put both on one Docker network.

In the vault, create the folder `reMarkable/` (with `Outbox/` and `Inbox/`
inside). rmbridge creates them as well if they're missing.

### 2. Fill in `.env`

```bash
cp .env.example .env     # if you don't have one yet
$EDITOR .env
```

Minimum for the Obsidian sync: the four `LIVESYNC_URL/DATABASE/USERNAME/PASSWORD`
lines, plus the passphrase lines if you use E2EE. Set `NAS_SHARE` and
`APP_PASSWORD` while you're there.

### 3. Start it

```bash
mkdir -p data
docker compose build
# one-time reMarkable cloud login, code from https://my.remarkable.com/device/browser/connect
docker compose run --rm -it rmbridge rmapi      # paste code, then type `exit`
docker compose up -d
```

Open `http://<nas>:8484`. The footer should show a green dot and
`LiveSync ⇄ <database>/reMarkable` within about 30 seconds. Use **logs** if it
stays red. **Sync Obsidian vault now** runs the tablet side immediately instead
of waiting for `SYNC_INTERVAL`.

After changing `.env`, run `docker compose up -d` again (it recreates the
container with the new values).

## Troubleshooting (the "logs" link in the footer)

| Log says                                                    | Fix                                                                         |
| ----------------------------------------------------------- | --------------------------------------------------------------------------- |
| `CouchDB not ready: HTTP 401`                               | Wrong `LIVESYNC_USERNAME` / `LIVESYNC_PASSWORD`                             |
| `fetch failed`                                              | `LIVESYNC_URL` unreachable from the container (see the localhost note above) |
| `Remote database is encrypted but no passphrase provided`   | Set `LIVESYNC_PASSPHRASE`                                                   |
| `Remote database is obfuscated but no obfuscate passphrase` | Set `LIVESYNC_OBFUSCATE_PASSPHRASE`                                         |
| Plugin says *not all files could be synchronised*           | Set `LIVESYNC_ID_KEY` from *Show current recovery code*                     |
| `Remote database looks like empty`                          | The database was never initialised by the plugin. Sync once from Obsidian, then press *restart* |

## Caveats

- **Handwriting comes back as PDFs, not editable text.** reMarkable's
  handwriting-to-text conversion isn't available outside its own apps.
- **Editing a file in `Outbox/` replaces the tablet copy**, and ink on the old
  copy is lost. The annotated version is still in `Inbox/` from the previous
  pull.
- **Deletes aren't synced** between the vault and the tablet, in either
  direction.
- **Big PDFs:** check *Self-hosted LiveSync → Sync settings → Maximum file size*
  in the plugin.
- Use the **cloud** backend for the vault sync. The USB web interface can't
  overwrite, so re-sent files become duplicates.
