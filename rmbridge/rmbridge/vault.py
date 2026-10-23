"""Two-way mirror between an Obsidian vault folder and a reMarkable folder.

    vault/<LIVESYNC_VAULT_FOLDER>/**   <==>   reMarkable <RM_VAULT_FOLDER>/**

Same subfolders on both sides, no inbox/outbox.

* Vault -> tablet: PDFs/EPUBs are uploaded as-is, Markdown notes are rendered
  to a PDF sized for the screen (see render.py). A file is re-sent when it
  changes in the vault.
* Tablet -> vault: a document that changed on the tablet is exported as a PDF
  with your handwriting, next to where it came from:
    - from a vault PDF: replaces that PDF (the tablet keeps the clean original
      under the ink, so nothing is lost);
    - from a note `X.md`: becomes `X.pdf` next to the note (the note is untouched);
    - made on the tablet (notebooks, files uploaded there): `Name.pdf`.
  Exported files are never sent back, so they can't overwrite the tablet copy.

Pulls run before pushes, so if a note changed on both sides the ink is saved
in the vault before the re-rendered note replaces the tablet copy. Deletes are
not mirrored in either direction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath

from .config import Settings
from .remarkable import UPLOADABLE, Backend, RemarkableError, safe_filename
from .render import markdown_to_pdf

log = logging.getLogger(__name__)

PUSHABLE = UPLOADABLE | {".md"}
STATE_VERSION = 2


def _sig(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


class VaultSync:
    def __init__(self, settings: Settings, backend: Backend) -> None:
        assert settings.vault_dir is not None
        self.settings = settings
        self.backend = backend
        self.root = settings.vault_dir
        self.rm_root = "/" + settings.rm_vault_folder.strip("/")
        self.state_file = settings.data_dir / "vault-state.json"
        self.last_report: dict = {}
        self._lock = asyncio.Lock()

    def _load_state(self) -> dict:
        # files: vault path -> {sig, doc, pulled}; docs: tablet path -> {modified, source, output}
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text())
            if state.get("version") == STATE_VERSION:
                return state
        return {"version": STATE_VERSION, "files": {}, "docs": {}}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.state_file)

    async def run_once(self) -> dict:
        async with self._lock:
            report = {"started": time.time(), "pushed": [], "pulled": [], "errors": []}
            state = self._load_state()
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                await self._pull(state, report)
                await self._push(state, report)
            except RemarkableError as exc:
                report["errors"].append(str(exc))
            finally:
                self._save_state(state)
            report["finished"] = time.time()
            self.last_report = report
            return report

    # --- tablet -> vault ---------------------------------------------------

    async def _pull(self, state: dict, report: dict) -> None:
        root_ref = await self.backend.ensure_folder(self.rm_root)
        await self._pull_folder(root_ref, self.rm_root, PurePosixPath(), state, report)

    async def _pull_folder(
        self, folder_ref: str, rm_path: str, rel_dir: PurePosixPath, state: dict, report: dict
    ) -> None:
        for entry in await self.backend.list(folder_ref):
            doc_path = f"{rm_path}/{entry.name}"
            if entry.kind == "folder":
                await self._pull_folder(entry.ref, doc_path, rel_dir / safe_filename(entry.name), state, report)
                continue
            doc = state["docs"].get(doc_path, {})
            if doc and doc.get("modified") == entry.modified:
                continue
            source = doc.get("source")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    got = await self.backend.download(entry.ref, Path(tmp), name="doc")
                    rel = self._pull_target(state, doc_path, rel_dir, entry.name, source, got.suffix)
                    dest = self.root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(got, dest)
            except (RemarkableError, OSError) as exc:
                report["errors"].append(f"pull {doc_path}: {exc}")
                continue
            # A vault PDF overwritten with its inked version is still that file;
            # anything else we wrote is an export and must not be sent back.
            state["files"][rel] = {"sig": _sig(dest), "doc": doc_path, "pulled": rel != source}
            state["docs"][doc_path] = {"modified": entry.modified, "source": source, "output": rel}
            report["pulled"].append(rel)

    def _pull_target(
        self, state: dict, doc_path: str, rel_dir: PurePosixPath, name: str, source: str | None, suffix: str
    ) -> str:
        if source and PurePosixPath(source).suffix.lower() == suffix:
            return source
        if source:
            folder, stem = PurePosixPath(source).parent, PurePosixPath(source).stem
        else:
            folder, stem = rel_dir, safe_filename(name)
        n = 1
        while True:
            rel = str(folder / (f"{stem}{suffix}" if n == 1 else f"{stem} ({n}){suffix}"))
            owner = state["files"].get(rel)
            if owner is not None and owner.get("doc") == doc_path:
                return rel  # our earlier export of this same document
            if owner is None and not (self.root / rel).exists():
                return rel
            n += 1

    # --- vault -> tablet ---------------------------------------------------

    async def _push(self, state: dict, report: dict) -> None:
        pushed: dict[tuple[str, str], list[str]] = {}
        for src in sorted(self.root.rglob("*")):
            if not src.is_file() or src.suffix.lower() not in PUSHABLE:
                continue
            rel = src.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            key = rel.as_posix()
            sig = _sig(src)
            record = state["files"].get(key)
            if record and record["sig"] == sig:
                continue
            if record and record.get("pulled"):
                # Ink exported from the tablet; sending it back would replace the original.
                record["sig"] = sig
                continue

            rm_folder = "/".join([self.rm_root, *rel.parent.parts])
            doc_path = record["doc"] if record else self._free_doc_path(state, rm_folder, src.stem, key)
            doc_name = doc_path.rsplit("/", 1)[1]
            try:
                folder_ref = await self.backend.ensure_folder(rm_folder)
                with tempfile.TemporaryDirectory() as tmp:
                    # The upload's file name becomes the document name on the tablet.
                    if src.suffix.lower() == ".md":
                        upload = Path(tmp) / f"{doc_name}.pdf"
                        await asyncio.to_thread(markdown_to_pdf, src, upload, self.root)
                    else:
                        upload = Path(tmp) / f"{doc_name}{src.suffix.lower()}"
                        shutil.copyfile(src, upload)
                    await self.backend.upload(upload, folder_ref, overwrite=True)
            except Exception as exc:  # render errors come from deep inside WeasyPrint
                report["errors"].append(f"push {key}: {exc}")
                continue
            state["files"][key] = {"sig": sig, "doc": doc_path, "pulled": False}
            state["docs"][doc_path] = {**state["docs"].get(doc_path, {}), "source": key, "modified": None}
            pushed.setdefault((rm_folder, folder_ref), []).append(doc_path)
            report["pushed"].append(key)

        # Remember what our uploads look like on the tablet, so they don't come back as edits.
        for (rm_folder, folder_ref), paths in pushed.items():
            for entry in await self.backend.list(folder_ref):
                doc_path = f"{rm_folder}/{entry.name}"
                if doc_path in paths:
                    state["docs"][doc_path]["modified"] = entry.modified

    def _free_doc_path(self, state: dict, rm_folder: str, stem: str, key: str) -> str:
        """Tablet path for a new vault file that doesn't clash with another document."""
        n = 1
        while True:
            doc_path = f"{rm_folder}/{stem}" if n == 1 else f"{rm_folder}/{stem} ({n})"
            doc = state["docs"].get(doc_path)
            if doc is None or doc.get("source") == key:
                return doc_path
            n += 1

    async def loop(self) -> None:
        while True:
            try:
                report = await self.run_once()
                if report["pushed"] or report["pulled"] or report["errors"]:
                    log.info("vault sync: %s", report)
            except Exception:  # keep the loop alive no matter what
                log.exception("vault sync failed")
            await asyncio.sleep(self.settings.sync_interval)
