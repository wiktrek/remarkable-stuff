# Syncing a reMarkable Paper Pro with Obsidian (Self-hosted LiveSync / CouchDB)

## The short version

There is **no direct way** to connect a reMarkable to Obsidian LiveSync. The
tablet only syncs with reMarkable's cloud (or USB), and it can't read Markdown
or edit text the way Obsidian does. LiveSync stores your vault in CouchDB as
chunked and often encrypted documents that only the LiveSync plugin can read.

So the bridge has three hops. Each hop syncs both ways:

```
 Obsidian (any device)
      ⇅  Self-hosted LiveSync plugin
 CouchDB  ──────────────────────────────── (your existing server)
      ⇅  livesync-bridge  (headless LiveSync client, writes plain files)
 ./data/vault/   =  <vault>/reMarkable/
      ⇅  rmbridge vault sync  (this repo)
 reMarkable cloud  /Obsidian folder
      ⇅  the tablet's normal cloud sync
 reMarkable Paper Pro
```

"Both ways" means:

| You do this…                                               | …and this happens                                                                 |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Drop a PDF/EPUB into `reMarkable/Outbox/` in Obsidian      | It appears on the tablet in `/Obsidian/`                                          |
| Write a note in `reMarkable/Outbox/` (`.md`)               | It's converted to EPUB and appears on the tablet (reflowable, annotatable)         |
| Edit that note again in Obsidian                           | The tablet copy is replaced (**ink on the old copy is lost**, see caveats)         |
| Annotate any doc / write a notebook in `/Obsidian` on the tablet | A PDF with your handwriting lands in `reMarkable/Inbox/`, plus a `.md` note embedding it |

Things you write on the tablet come back to Obsidian **as PDFs**, not as
editable Markdown. reMarkable's handwriting-to-text conversion runs only in
its own app and cloud, and no API exposes it. If you need searchable text,
convert on the tablet and *share by email*, or run OCR on the PDFs in the inbox.

## 1. Requirements

- The CouchDB server you already run for LiveSync.
- A reMarkable account with **cloud sync turned on** on the Paper Pro. Without
  Connect, the free tier still syncs, but only documents opened in the last
  50 days, which is fine for an `/Obsidian` working folder.
- Docker on the NAS / server.

## 2. Collect your LiveSync settings

In Obsidian go to **Settings → Self-hosted LiveSync** and note:

- **Remote Database → URI, Username, Password, Database name**
- **E2EE passphrase** if end-to-end encryption is on
- **Path obfuscation**: if it's on, the obfuscation passphrase is normally the
  same as the E2EE passphrase
- On recent plugin versions with E2EE: **Show current recovery code**. This
  gives the vault's ID key (`sls-id-v1:…`), which the bridge needs as
  `idDerivationKey`. If you skip it, the plugin can't read what the bridge writes.

Then create a folder `reMarkable/` in your vault with `Outbox/` and `Inbox/`
inside it.

## 3. Configure livesync-bridge

```bash
cp livesync-bridge/config.sample.json livesync-bridge/config.json
```

Fill in the `couchdb` peer:

```jsonc
{
  "type": "couchdb",
  "name": "obsidian",
  "url": "https://couchdb.your.lan",     // same URI as in the plugin
  "database": "obsidian",                 // same DB name
  "username": "admin",
  "password": "…",
  "passphrase": "…",                      // E2EE passphrase, "" if off
  "obfuscatePassphrase": "…",             // "" if path obfuscation is off
  "idDerivationKey": "sls-id-v1:…",       // only if the plugin shows one
  "baseDir": "reMarkable/",               // ONLY this vault folder is mirrored
  "useRemoteTweaks": true                 // take chunk sizes from the vault
}
```

`baseDir: "reMarkable/"` matters. The bridge then mirrors only
`<vault>/reMarkable/**` into `./data/vault/` (Outbox and Inbox land at
`./data/vault/Outbox` and `./data/vault/Inbox`). The rest of your notes never
leave CouchDB.

The `storage` peer (`./data/vault/`) is already set up in the sample. Both
peers sit in the same (default) group, so the bridge syncs them **in both
directions**. A file written by rmbridge goes up to CouchDB, and a file you add
in Obsidian comes down to the folder.

## 4. Configure rmbridge and start everything

```bash
cp .env.example .env            # set NAS_SHARE, APP_PASSWORD, etc.
mkdir -p data/rmbridge data/vault
sudo chown -R 1993:1993 data    # uid used by both containers (PUID/PGID in .env)
docker compose build

# One-time: log rmapi into your reMarkable account.
# Get a code at https://my.remarkable.com/device/browser/connect
docker compose run --rm -it rmbridge rmapi
#   → paste the code, wait for the "[/]>" prompt, type `exit`

docker compose up -d
```

Open `http://<nas>:8484`. The footer should show `Vault ↔ /Obsidian`. Press
**Sync Obsidian vault now** to test it, or wait `SYNC_INTERVAL` seconds.

## 5. Daily use

- **To read or annotate something on the tablet:** drag it into
  `reMarkable/Outbox/` in Obsidian (subfolders are recreated on the tablet), or
  write a note there.
- **To get handwriting into Obsidian:** write or annotate anything in the
  tablet's `/Obsidian` folder. Within a few minutes it shows up in
  `reMarkable/Inbox/` as `Name.pdf` plus `Name.md`. The `.md` note is created
  once and never overwritten, so link it, tag it and write around it. The PDF
  is refreshed every time you change the document on the tablet.

## Caveats (worth reading once)

- **Overwrites, not merges.** Editing a file in `Outbox/` re-uploads it with
  `rmapi put --force`, which recreates the document on the tablet and drops
  any ink on the old copy. Your annotated version is still in `Inbox/` from the
  previous pull. If you want to keep annotating, treat the tablet copy as the
  live one and stop editing the source.
- **Deletes are not propagated** in either direction. That's on purpose, so a
  mistake can't wipe the other side.
- **LiveSync file size limit:** big PDFs can be skipped by the plugin. Check
  *Settings → Self-hosted LiveSync → Sync settings → Maximum file size*.
- **Annotation rendering** goes through `rmapi geta`. It handles pens and
  highlighters well but is simpler than the tablet's own export. For a
  pixel-perfect PDF, plug the tablet in by USB, switch the UI to *USB cable* and
  pull it from there (that uses the tablet's own renderer).
- **USB backend + vault sync:** the USB web interface can't overwrite, so a
  re-pushed Outbox file becomes a duplicate. Use the cloud backend for the
  vault bridge.

## Alternatives that were considered

- **rmfakecloud** (self-hosted replacement for reMarkable's cloud): this would
  take reMarkable's servers out of the loop. On the Paper Pro, pointing the
  tablet at it requires *developer mode*, which factory-resets the device and
  needs SSH tinkering after updates. Swap it in later if you want: rmapi works
  against it via `RMAPI_HOST`.
- **Obsidian plugins that read the reMarkable cloud** (e.g. ones that render
  `.rm` files): they work, but they run in a single Obsidian instance and
  don't push anything to the tablet.
