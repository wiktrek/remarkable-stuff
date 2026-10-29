"""Two ways of talking to a reMarkable (Paper Pro, rM2, Move...).

* CloudBackend: shells out to `rmapi` (github.com/ddvk/rmapi) and goes through
  the reMarkable cloud. Works from anywhere, e.g. a NAS, as long as the tablet
  has cloud sync turned on.
* UsbBackend: talks to the tablet's built-in USB web interface
  (Settings → Storage → USB web interface) at http://10.11.99.1. No account
  needed, but the tablet has to be plugged into the machine running this app.

Both expose the same small interface. Entries are addressed by an opaque
`ref`: a path like "/Books/Papers" for the cloud backend and a document ID for
the USB backend.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

UPLOADABLE = {".pdf", ".epub"}


class RemarkableError(RuntimeError):
    pass


@dataclass
class Entry:
    ref: str
    name: str
    kind: str  # "folder" | "doc"
    modified: str = ""
    file_type: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def safe_filename(name: str) -> str:
    cleaned = "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in name)
    return cleaned.strip().strip(".") or "untitled"


class CloudBackend:
    name = "cloud"

    def __init__(self, rmapi_bin: str) -> None:
        self.rmapi_bin = rmapi_bin
        # rmapi keeps a local tree cache; running two at once corrupts it.
        self._lock = asyncio.Lock()
        self._status: tuple[float, dict] | None = None

    async def _run(
        self, *args: str, cwd: Path | None = None, flags: tuple[str, ...] = ()
    ) -> str:
        if shutil.which(self.rmapi_bin) is None:
            raise RemarkableError(f"rmapi not found ({self.rmapi_bin}); see README")
        async with self._lock:
            proc = await asyncio.create_subprocess_exec(
                self.rmapi_bin,
                "-ni",  # never block waiting for a one-time code
                *flags,
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = (err or out).decode(errors="replace").strip().splitlines()
            raise RemarkableError(msg[-1] if msg else f"rmapi {args[0]} failed")
        return out.decode(errors="replace")

    async def status(self) -> dict:
        # The UI polls this; don't hit the cloud more than once a minute.
        if self._status and time.monotonic() - self._status[0] < 60:
            return self._status[1]
        result = await self._check()
        self._status = (time.monotonic(), result)
        return result

    async def _check(self) -> dict:
        try:
            await self._run("version")
        except RemarkableError as exc:
            return {"ok": False, "detail": str(exc)}
        try:
            await self._run("ls", "/")
        except RemarkableError as exc:
            return {"ok": False, "detail": f"rmapi not authenticated: {exc}"}
        return {"ok": True, "detail": "reMarkable cloud"}

    async def list(self, ref: str = "/") -> list[Entry]:
        ref = ref or "/"
        # -json is a global rmapi flag, so it goes before the command.
        text = await self._run("ls", _glob_escape(ref), flags=("-json",))
        start = text.find("[")
        nodes = json.loads(text[start:]) if start != -1 else []  # empty folder: "null"
        entries = []
        for node in nodes or []:
            kind = "folder" if node["type"] == "CollectionType" else "doc"
            entries.append(
                Entry(
                    ref=_join(ref, node["name"]),
                    name=node["name"],
                    kind=kind,
                    modified=node.get("modifiedClient", ""),
                )
            )
        return _sorted(entries)

    async def ensure_folder(self, path: str) -> str:
        current = ""
        for part in [p for p in path.split("/") if p]:
            current = f"{current}/{part}"
            await self._run("mkdir", current)  # no-op if it already exists
        return current or "/"

    async def upload(self, file: Path, folder_ref: str, overwrite: bool = False) -> None:
        args = ["put"]
        if overwrite:
            args.append("--force")
        await self._run(*args, str(file), folder_ref or "/")

    async def download(self, ref: str, dest_dir: Path, name: str | None = None) -> Path:
        """Fetch a document as a PDF with annotations (or the original EPUB)."""
        name = safe_filename(name or ref.rstrip("/").rsplit("/", 1)[-1])
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            try:
                # `geta -a` renders pen strokes onto the PDF / notebook pages.
                await self._run("geta", "-a", ref, cwd=tmpdir)
                rendered = next(tmpdir.glob("*-annotations.pdf"), None)
            except RemarkableError:
                rendered = None
            if rendered is not None:
                return _move(rendered, dest_dir / f"{name}.pdf")

            # EPUBs (and anything geta can't render): ship the original file.
            await self._run("get", ref, cwd=tmpdir)
            archive = next(tmpdir.glob("*.rmdoc"), None) or next(tmpdir.glob("*.zip"), None)
            if archive is None:
                raise RemarkableError(f"rmapi did not produce a file for {ref}")
            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():
                    suffix = Path(member).suffix.lower()
                    if suffix in UPLOADABLE and "/" not in member:
                        target = dest_dir / f"{name}{suffix}"
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(member))
                        return target
            raise RemarkableError(
                f"{ref} is a notebook that rmapi could not render; try the USB backend"
            )


class UsbBackend:
    name = "usb"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        # The web UI uploads into whichever folder was listed last, so
        # navigating + uploading must not interleave.
        self._lock = asyncio.Lock()

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Origin": self.base_url, "Referer": self.base_url + "/"},
        )

    async def status(self) -> dict:
        try:
            await self.list("")
        except RemarkableError as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": f"USB web interface at {self.base_url}"}

    async def _list_raw(self, client: httpx.AsyncClient, folder_id: str) -> list[dict]:
        path = f"/documents/{folder_id}" if folder_id else "/documents/"
        try:
            resp = await client.get(path)
            if resp.status_code == 405:  # older firmware wanted POST
                resp = await client.post(path)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RemarkableError(
                f"Can't reach the tablet at {self.base_url}. Is it plugged in, unlocked "
                f"and is the USB web interface enabled? ({exc})"
            ) from exc
        return resp.json()

    async def list(self, ref: str = "") -> list[Entry]:
        async with self._client() as client:
            raw = await self._list_raw(client, ref)
        entries = [
            Entry(
                ref=item["ID"],
                name=item.get("VissibleName") or item.get("VisibleName") or item["ID"],
                kind="folder" if item.get("Type") == "CollectionType" else "doc",
                modified=item.get("ModifiedClient", ""),
                file_type=item.get("fileType", ""),
            )
            for item in raw
        ]
        return _sorted(entries)

    async def ensure_folder(self, path: str) -> str:
        """Resolve "/A/B" to a folder ID. The USB interface can't create folders."""
        folder_id = ""
        for part in [p for p in path.split("/") if p]:
            match = next(
                (e for e in await self.list(folder_id) if e.kind == "folder" and e.name == part),
                None,
            )
            if match is None:
                raise RemarkableError(
                    f"Folder '{part}' does not exist on the tablet; create it there first "
                    "(the USB web interface cannot create folders)"
                )
            folder_id = match.ref
        return folder_id

    async def upload(self, file: Path, folder_ref: str, overwrite: bool = False) -> None:
        # The USB interface has no overwrite; re-uploads create a second copy.
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        async with self._lock, self._client(timeout=300) as client:
            await self._list_raw(client, folder_ref)
            with file.open("rb") as fh:
                resp = await client.post("/upload", files={"file": (file.name, fh, mime)})
            if resp.status_code >= 400:
                raise RemarkableError(f"Upload failed: HTTP {resp.status_code} {resp.text[:200]}")

    async def download(self, ref: str, dest_dir: Path, name: str | None = None) -> Path:
        async with self._client(timeout=600) as client:
            # /pdf on current firmware, /placeholder on older ones.
            for suffix in ("pdf", "placeholder"):
                resp = await client.get(f"/download/{ref}/{suffix}")
                if resp.status_code == 200:
                    break
            else:
                raise RemarkableError(f"Download failed: HTTP {resp.status_code}")
        if name is None:
            disposition = resp.headers.get("content-disposition", "")
            name = disposition.split("filename=")[-1].strip('"; ') if "filename=" in disposition else ref
            name = Path(name).stem
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{safe_filename(name)}.pdf"
        target.write_bytes(resp.content)
        return target


Backend = CloudBackend | UsbBackend


def _glob_escape(path: str) -> str:
    # `rmapi ls` matches the last path segment as a glob (case-insensitively).
    head, sep, last = path.rpartition("/")
    return head + sep + re.sub(r"([*?\[\\])", r"\\\1", last)


def _join(folder: str, name: str) -> str:
    return f"{folder.rstrip('/')}/{name}"


def _sorted(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (e.kind != "folder", e.name.lower()))


def _move(src: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, target)
    return target
