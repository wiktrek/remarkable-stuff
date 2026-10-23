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
- **Obsidian bridge**: a vault folder that syncs both ways with a folder on
  the tablet. See [docs/obsidian-livesync.md](docs/obsidian-livesync.md).

## Run with Docker (on the NAS)

```bash
cp .env.example .env          # set NAS_SHARE to your PDF folder, APP_PASSWORD
mkdir -p data/rmbridge data/vault && sudo chown -R 1993:1993 data
docker compose build

# one-time cloud login: code from https://my.remarkable.com/device/browser/connect
docker compose run --rm -it rmbridge rmapi     # paste code, then `exit`

docker compose up -d rmbridge                  # add livesync-bridge later, see docs
```

Then open `http://<nas>:8484`.

If you don't want the Obsidian part, leave `VAULT_DIR` empty in `.env` and only
start the `rmbridge` service.

## Run locally (no Docker)

```bash
git clone https://github.com/ddvk/rmapi /tmp/rmapi && (cd /tmp/rmapi && go build -o ~/.local/bin/rmapi .)
rmapi                                                  # one-time login, then `exit`
NAS_ROOT=/mnt/nas/books uv run rmbridge                # → http://localhost:8484
```

To use it over USB on a laptop: `RM_BACKEND=usb uv run rmbridge`.

## Configuration

All settings are environment variables. See [`.env.example`](.env.example).

| Variable          | Default              | Meaning                                                  |
| ----------------- | -------------------- | -------------------------------------------------------- |
| `NAS_ROOT`        | `./data/nas`         | Folder shown in the NAS pane (`/nas` in Docker)          |
| `RM_BACKEND`      | `cloud`              | `cloud` or `usb`                                         |
| `RM_USB_URL`      | `http://10.11.99.1`  | USB web interface address                                |
| `RMAPI_BIN`       | `rmapi`              | Path to rmapi                                            |
| `DATA_DIR`        | `./data/state`       | Sync bookkeeping (keep it out of the vault)              |
| `APP_PASSWORD`    | –                    | Enables HTTP basic auth (any username)                   |
| `VAULT_DIR`       | –                    | Enables the Obsidian bridge                              |
| `VAULT_OUTBOX`    | `reMarkable/Outbox`  | Vault → tablet folder (relative to `VAULT_DIR`)          |
| `VAULT_INBOX`     | `reMarkable/Inbox`   | Tablet → vault folder                                    |
| `RM_VAULT_FOLDER` | `/Obsidian`          | Folder on the tablet that the vault syncs with           |
| `SYNC_INTERVAL`   | `300`                | Seconds between vault syncs (`0` = manual only)          |
| `HOST` / `PORT`   | `0.0.0.0` / `8484`   | Listen address                                           |

## Development

```bash
uv sync
uv run pytest     # uses a fake rmapi and a mocked USB interface, no tablet needed
```
