"""Settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path(name: str, default: str | None = None) -> Path | None:
    value = os.environ.get(name, default)
    return Path(value).expanduser().resolve() if value else None


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
    # Optional Obsidian vault folder (the livesync-bridge "storage" peer).
    vault_dir: Path | None
    vault_outbox: str
    vault_inbox: str
    rm_vault_folder: str
    sync_interval: int
    password: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            nas_root=_path("NAS_ROOT", "./data/nas"),
            backend=os.environ.get("RM_BACKEND", "cloud").lower(),
            rmapi_bin=os.environ.get("RMAPI_BIN", "rmapi"),
            usb_url=os.environ.get("RM_USB_URL", "http://10.11.99.1").rstrip("/"),
            data_dir=_path("DATA_DIR", "./data/state"),
            vault_dir=_path("VAULT_DIR"),
            vault_outbox=os.environ.get("VAULT_OUTBOX", "reMarkable/Outbox"),
            vault_inbox=os.environ.get("VAULT_INBOX", "reMarkable/Inbox"),
            rm_vault_folder=os.environ.get("RM_VAULT_FOLDER", "/Obsidian"),
            sync_interval=int(os.environ.get("SYNC_INTERVAL", "300")),
            password=os.environ.get("APP_PASSWORD") or None,
        )
