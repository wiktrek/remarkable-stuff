"""One-way sync of NAS folders to reMarkable folders (NAS_SYNC in .env).

    NAS_SHARE/Books/**   ==>   reMarkable /Books/**

Same subfolders on both sides. Every PDF/EPUB that shows up in a synced NAS
folder is uploaded once. After that it belongs to the tablet: annotations stay,
changes on the NAS are not re-sent, and a document you delete on the tablet
stays deleted. Files that are already on the tablet under the same name (e.g.
sent by hand earlier) are not uploaded again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from . import nas
from .config import Settings
from .remarkable import UPLOADABLE, Backend, RemarkableError

log = logging.getLogger(__name__)


class NasSync:
    def __init__(self, settings: Settings, backend: Backend) -> None:
        self.settings = settings
        self.backend = backend
        self.state_file = settings.data_dir / "nas-sync-state.json"
        self.last_report: dict = {}
        self._lock = asyncio.Lock()

    def _load_state(self) -> dict:
        # "<nas folder>:<tablet folder>" -> list of NAS paths already sent
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.state_file)

    async def run_once(self) -> dict:
        async with self._lock:
            report = {"started": time.time(), "pushed": [], "errors": []}
            state = self._load_state()
            try:
                for nas_folder, rm_folder in self.settings.nas_sync:
                    sent = set(state.get(f"{nas_folder}:{rm_folder}", []))
                    try:
                        await self._sync(nas_folder, rm_folder, sent, report)
                    finally:
                        state[f"{nas_folder}:{rm_folder}"] = sorted(sent)
            finally:
                self._save_state(state)
            report["finished"] = time.time()
            self.last_report = report
            return report

    async def _sync(self, nas_folder: str, rm_folder: str, sent: set[str], report: dict) -> None:
        try:
            root = nas.resolve(self.settings.nas_root, nas_folder)
        except nas.PathError as exc:
            report["errors"].append(str(exc))
            return
        if not root.is_dir():
            report["errors"].append(f"NAS folder {nas_folder!r} does not exist")
            return

        new: dict[str, list[Path]] = {}
        for src in sorted(root.rglob("*")):
            rel = src.relative_to(root)
            if not src.is_file() or src.suffix.lower() not in UPLOADABLE:
                continue
            if any(part.startswith(".") for part in rel.parts) or rel.as_posix() in sent:
                continue
            folder = "/".join([rm_folder.rstrip("/"), *rel.parent.parts]) or "/"
            new.setdefault(folder, []).append(src)

        for folder, files in new.items():
            try:
                folder_ref = await self.backend.ensure_folder(folder)
                existing = {e.name for e in await self.backend.list(folder_ref) if e.kind == "doc"}
            except RemarkableError as exc:
                report["errors"].append(f"{folder}: {exc}")
                continue
            for src in files:
                key = src.relative_to(root).as_posix()
                if src.stem not in existing:
                    try:
                        await self.backend.upload(src, folder_ref)
                    except RemarkableError as exc:
                        report["errors"].append(f"{nas_folder}/{key}: {exc}")
                        continue
                    report["pushed"].append(f"{nas_folder}/{key}")
                sent.add(key)

    async def loop(self) -> None:
        while True:
            try:
                report = await self.run_once()
                if report["pushed"] or report["errors"]:
                    log.info("NAS sync: %s", report)
            except Exception:  # keep the loop alive no matter what
                log.exception("NAS sync failed")
            await asyncio.sleep(self.settings.sync_interval)
