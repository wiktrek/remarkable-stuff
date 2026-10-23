"""Two-way bridge between an Obsidian vault folder and a reMarkable folder.

    vault/<VAULT_OUTBOX>/**   --push-->  reMarkable <RM_VAULT_FOLDER>/**
    vault/<VAULT_INBOX>/**    <--pull--  reMarkable <RM_VAULT_FOLDER>/**

* Push: PDFs/EPUBs are uploaded as-is, Markdown notes are converted to EPUB
  with pandoc (reflowable on the tablet). A file is re-uploaded when its
  mtime/size changes.
* Pull: every document in the reMarkable folder whose modification time changed
  is exported (as a PDF with your handwriting) into the inbox, next to a small
  Markdown note that embeds it. The Markdown note is only created once, so
  anything you add to it in Obsidian is kept.

The vault folder is expected to be the "storage" peer of livesync-bridge, which
mirrors it to/from the Obsidian LiveSync CouchDB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path

from .config import Settings
from .remarkable import UPLOADABLE, Backend, RemarkableError, safe_filename

log = logging.getLogger(__name__)

PUSHABLE = UPLOADABLE | {".md"}


class VaultSync:
    def __init__(self, settings: Settings, backend: Backend) -> None:
        assert settings.vault_dir is not None
        self.settings = settings
        self.backend = backend
        self.outbox = settings.vault_dir / settings.vault_outbox
        self.inbox = settings.vault_dir / settings.vault_inbox
        self.state_file = settings.data_dir / "vault-state.json"
        self.last_report: dict = {}
        self._lock = asyncio.Lock()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {"pushed": {}, "pulled": {}}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.state_file)

    async def run_once(self) -> dict:
        async with self._lock:
            report = {"started": time.time(), "pushed": [], "pulled": [], "errors": []}
            state = self._load_state()
            self.outbox.mkdir(parents=True, exist_ok=True)
            self.inbox.mkdir(parents=True, exist_ok=True)
            just_pushed: set[str] = set()
            try:
                await self._push(state, report, just_pushed)
                await self._pull(state, report, just_pushed)
            except RemarkableError as exc:
                report["errors"].append(str(exc))
            finally:
                self._save_state(state)
            report["finished"] = time.time()
            self.last_report = report
            return report

    async def _push(self, state: dict, report: dict, just_pushed: set[str]) -> None:
        for src in sorted(self.outbox.rglob("*")):
            if not src.is_file() or src.suffix.lower() not in PUSHABLE:
                continue
            rel = src.relative_to(self.outbox)
            if any(part.startswith(".") for part in rel.parts):
                continue
            stat = src.stat()
            sig = f"{stat.st_mtime_ns}:{stat.st_size}"
            if state["pushed"].get(str(rel), {}).get("sig") == sig:
                continue

            rm_folder = "/".join([self.settings.rm_vault_folder.rstrip("/"), *rel.parent.parts])
            try:
                folder_ref = await self.backend.ensure_folder(rm_folder)
                with tempfile.TemporaryDirectory() as tmp:
                    upload = src
                    if src.suffix.lower() == ".md":
                        upload = await _markdown_to_epub(src, Path(tmp), self.settings.vault_dir)
                    await self.backend.upload(upload, folder_ref, overwrite=True)
            except RemarkableError as exc:
                report["errors"].append(f"push {rel}: {exc}")
                continue
            state["pushed"][str(rel)] = {"sig": sig}
            just_pushed.add(f"{rm_folder}/{src.stem}")
            report["pushed"].append(str(rel))

    async def _pull(self, state: dict, report: dict, just_pushed: set[str]) -> None:
        root_ref = await self.backend.ensure_folder(self.settings.rm_vault_folder)
        await self._pull_folder(
            root_ref, self.settings.rm_vault_folder.rstrip("/"), Path(), state, report, just_pushed
        )

    async def _pull_folder(
        self,
        folder_ref: str,
        rm_path: str,
        rel: Path,
        state: dict,
        report: dict,
        just_pushed: set[str],
    ) -> None:
        for entry in await self.backend.list(folder_ref):
            entry_path = f"{rm_path}/{entry.name}"
            if entry.kind == "folder":
                await self._pull_folder(
                    entry.ref, entry_path, rel / safe_filename(entry.name), state, report, just_pushed
                )
                continue
            if state["pulled"].get(entry_path) == entry.modified:
                continue
            if entry_path in just_pushed:
                # We just uploaded it; nothing new was written on the tablet yet.
                state["pulled"][entry_path] = entry.modified
                continue
            dest_dir = self.inbox / rel
            try:
                out = await self.backend.download(entry.ref, dest_dir, name=entry.name)
            except RemarkableError as exc:
                report["errors"].append(f"pull {entry_path}: {exc}")
                continue
            _write_companion_note(out, entry_path)
            state["pulled"][entry_path] = entry.modified
            report["pulled"].append(str(out.relative_to(self.inbox)))

    async def loop(self) -> None:
        while True:
            try:
                report = await self.run_once()
                if report["pushed"] or report["pulled"] or report["errors"]:
                    log.info("vault sync: %s", report)
            except Exception:  # keep the loop alive no matter what
                log.exception("vault sync failed")
            await asyncio.sleep(self.settings.sync_interval)


async def _markdown_to_epub(src: Path, tmpdir: Path, vault_dir: Path) -> Path:
    if shutil.which("pandoc") is None:
        raise RemarkableError("pandoc is not installed, can't convert Markdown notes")
    out = tmpdir / f"{src.stem}.epub"
    proc = await asyncio.create_subprocess_exec(
        "pandoc",
        str(src),
        # Obsidian [[wikilinks]] render as plain text instead of breaking.
        "--from=markdown+wikilinks_title_after_pipe",
        "--metadata", f"title={src.stem}",
        "--resource-path", f"{src.parent}:{vault_dir}",
        "-o", str(out),
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RemarkableError(f"pandoc failed: {err.decode(errors='replace').strip()}")
    return out


def _write_companion_note(doc: Path, rm_path: str) -> None:
    note = doc.with_suffix(".md")
    if note.exists():
        return
    embed = f"![[{doc.name}]]" if doc.suffix == ".pdf" else f"[[{doc.name}]]"
    note.write_text(
        "---\n"
        "source: reMarkable\n"
        f'remarkable_path: "{rm_path}"\n'
        "tags: [remarkable]\n"
        "---\n\n"
        f"{embed}\n"
    )
