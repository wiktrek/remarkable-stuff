# rmbridge

A small self-hosted web app for moving documents between a **NAS**, a
**reMarkable** (Paper Pro / rM2 / Move) and, optionally, an **Obsidian vault**
synced with Self-hosted LiveSync.

- Two panes, NAS on the left and reMarkable on the right. Tick files, press
  **Send to reMarkable →** or **← Save to NAS**. Whole folders work too.
- Drag PDFs/EPUBs from your desktop into the reMarkable pane to upload them.
- Pulled documents come back as **PDFs with your handwriting**. EPUBs come back
  as the original EPUB.
- Two ways to reach the tablet, switchable in the header:
  - **Cloud** (default): uses [`rmapi`](https://github.com/ddvk/rmapi). Works
    from the NAS, with the tablet anywhere.
  - **USB cable**: uses the tablet's built-in web interface (`10.11.99.1`). No
    account needed, but the tablet has to be plugged into the machine running
    rmbridge, with *Settings → Storage → USB web interface* on.
- **Obsidian sync** (Self-hosted LiveSync): put your CouchDB details in
  `.env` and the vault's `reMarkable/` folder syncs both ways with `/Obsidian`
  on the tablet. rmbridge runs livesync-bridge for you, so there's nothing
  else to install. See [docs/obsidian-livesync.md](docs/obsidian-livesync.md).

## Run with Docker (on the NAS)

```bash
cp .env.example .env          # then edit: NAS_SHARE, APP_PASSWORD, LIVESYNC_*
mkdir -p data
docker compose build

# one-time cloud login: code from https://my.remarkable.com/device/browser/connect
docker compose run --rm -it rmbridge rmapi     # paste code, then `exit`

docker compose up -d
```

Then open `http://<nas>:8484`. If you edit `.env`, apply it with
`docker compose up -d`. If you don't want the Obsidian sync, leave
`LIVESYNC_URL` empty.

## Run locally (no Docker)

```bash
git clone https://github.com/ddvk/rmapi /tmp/rmapi && (cd /tmp/rmapi && go build -o ~/.local/bin/rmapi .)
rmapi                     # one-time login, then `exit`

# only for the Obsidian sync (needs deno):
git clone https://github.com/vrtmrz/livesync-bridge ~/.local/share/livesync-bridge
(cd ~/.local/share/livesync-bridge && deno install --frozen)
echo "LIVESYNC_BRIDGE_DIR=$HOME/.local/share/livesync-bridge" >> .env

uv run rmbridge           # reads ./.env → http://localhost:8484
```

To use it over USB on a laptop, set `RM_BACKEND=usb` in `.env`.

## Configuration

All settings are environment variables. See [`.env.example`](.env.example).

| Variable          | Default              | Meaning                                                  |
| ----------------- | -------------------- | -------------------------------------------------------- |
| `NAS_SHARE`       | `./data/nas`         | Host folder shown in the NAS pane                        |
| `RM_BACKEND`      | `cloud`              | `cloud` or `usb`                                         |
| `RM_USB_URL`      | `http://10.11.99.1`  | USB web interface address                                |
| `RM_VAULT_FOLDER` | `/Obsidian`          | Folder on the tablet that syncs with the vault           |
| `LIVESYNC_URL`    | –                    | CouchDB URI from the LiveSync plugin; enables Obsidian sync |
| `LIVESYNC_DATABASE` / `_USERNAME` / `_PASSWORD` | – | Same as in the plugin                       |
| `LIVESYNC_PASSPHRASE` | –                | E2EE passphrase (empty if E2EE is off)                   |
| `LIVESYNC_OBFUSCATE_PASSPHRASE` | –      | Path obfuscation passphrase (empty if off)               |
| `LIVESYNC_ID_KEY` | –                    | `sls-id-v1:…` recovery code (newer E2EE vaults)          |
| `LIVESYNC_VAULT_FOLDER` | `reMarkable`   | The only vault folder that is mirrored                   |
| `VAULT_OUTBOX` / `VAULT_INBOX` | `Outbox` / `Inbox` | Subfolders of it: to the tablet / from the tablet |
| `SYNC_INTERVAL`   | `120`                | Seconds between tablet syncs (`0` = button only)         |
| `APP_PASSWORD`    | –                    | Enables HTTP basic auth (any username)                   |
| `PORT`            | `8484`               | Web UI port                                              |
| `DATA_DIR`        | `./data/state`       | State, rmapi login, the bridge's vault mirror            |
| `VAULT_DIR`       | –                    | Without LiveSync: sync a local folder (e.g. `<vault>/reMarkable`) instead |

## Development

```bash
uv sync
uv run pytest     # fake rmapi, mocked USB interface and a fake livesync-bridge
```
