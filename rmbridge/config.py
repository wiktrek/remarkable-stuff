"""Settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path(name: str, default: str | None = None) -> Path | None:
    value = os.environ.get(name) or default
    return Path(value).expanduser().resolve() if value else None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class LiveSyncSettings:
    """Connection to the Self-hosted LiveSync CouchDB (same values as the plugin)."""

    url: str
    database: str
    username: str
    password: str
    # E2EE passphrase; empty if end-to-end encryption is off.
    passphrase: str
    # Path obfuscation passphrase; empty if path obfuscation is off.
    obfuscate_passphrase: str
    # "sls-id-v1:..." recovery code / ID key, needed for newer E2EE vaults.
    id_key: str
    # Vault folder mirrored to the tablet, e.g. "reMarkable".
    vault_folder: str
    bridge_dir: str
    deno_bin: str

    @classmethod
    def from_env(cls) -> "LiveSyncSettings | None":
        url = _env("LIVESYNC_URL")
        if not url or _env("LIVESYNC_ENABLED", "true").lower() in ("0", "false", "no"):
            return None
        missing = [n for n in ("LIVESYNC_DATABASE", "LIVESYNC_USERNAME", "LIVESYNC_PASSWORD") if not _env(n)]
        if missing:
            raise SystemExit(f"LIVESYNC_URL is set but {', '.join(missing)} is empty (see .env)")
        return cls(
            url=url.rstrip("/"),
            database=_env("LIVESYNC_DATABASE"),
            username=_env("LIVESYNC_USERNAME"),
            password=_env("LIVESYNC_PASSWORD"),
            passphrase=_env("LIVESYNC_PASSPHRASE"),
            obfuscate_passphrase=_env("LIVESYNC_OBFUSCATE_PASSPHRASE"),
            id_key=_env("LIVESYNC_ID_KEY"),
            vault_folder=_env("LIVESYNC_VAULT_FOLDER", "reMarkable").strip("/") or "reMarkable",
            bridge_dir=_env("LIVESYNC_BRIDGE_DIR", "/opt/livesync-bridge"),
            deno_bin=_env("DENO_BIN", "deno"),
        )


@dataclass(frozen=True)
class Settings:
    # Root of the NAS share as mounted on this machine / container.
    nas_root: Path
    # "cloud" (rmapi, works anywhere) or "usb" (tablet's USB web interface).
    backend: str
    rmapi_bin: str
    usb_url: str
    # Where the app keeps its own state (sync bookkeeping). Must NOT be inside the vault.
    data_dir: Path
    # Folder mirrored with the vault. With LiveSync this is the bridge's copy of
    # <vault>/<LIVESYNC_VAULT_FOLDER>; without it, any folder inside a vault.
    vault_dir: Path | None
    rm_vault_folder: str
    sync_interval: int
    password: str | None
    livesync: LiveSyncSettings | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = _path("DATA_DIR", "./data/state")
        livesync = LiveSyncSettings.from_env()
        vault_dir = _path("VAULT_DIR")
        if livesync:
            vault_dir = data_dir / "vault"
        return cls(
            # NAS_ROOT is set inside Docker; outside it, use the host path directly.
            nas_root=_path("NAS_ROOT") or _path("NAS_SHARE", "./data/nas"),
            backend=_env("RM_BACKEND", "cloud").lower(),
            rmapi_bin=_env("RMAPI_BIN", "rmapi"),
            usb_url=_env("RM_USB_URL", "http://10.11.99.1").rstrip("/"),
            data_dir=data_dir,
            vault_dir=vault_dir,
            rm_vault_folder=_env("RM_VAULT_FOLDER", "/Obsidian"),
            sync_interval=int(_env("SYNC_INTERVAL", "120")),
            password=_env("APP_PASSWORD") or None,
            livesync=livesync,
        )
