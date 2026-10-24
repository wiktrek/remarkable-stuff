"""HTTP API + single-page UI."""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

from . import nas
from .config import Settings
from .livesync import LiveSyncBridge
from .nas_sync import NasSync
from .remarkable import UPLOADABLE, Backend, CloudBackend, RemarkableError, UsbBackend, safe_filename
from .vault import VaultSync

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class BackendIn(BaseModel):
    name: str


class SendIn(BaseModel):
    paths: list[str]
    folder: str = ""


class PullItem(BaseModel):
    ref: str
    name: str


class PullIn(BaseModel):
    items: list[PullItem]
    nas_path: str = ""


def make_backend(settings: Settings, name: str) -> Backend:
    if name == "usb":
        return UsbBackend(settings.usb_url)
    if name == "cloud":
        return CloudBackend(settings.rmapi_bin)
    raise HTTPException(400, f"unknown backend {name!r}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.nas_root.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    state: dict = {"backend": make_backend(settings, settings.backend), "vault": None, "nas_sync": None}
    if settings.vault_dir:
        state["vault"] = VaultSync(settings, state["backend"])
    if settings.nas_sync:
        state["nas_sync"] = NasSync(settings, state["backend"])
    bridge = LiveSyncBridge(settings) if settings.livesync else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = []
        if bridge:
            tasks.append(asyncio.create_task(bridge.run()))
        if state["vault"] and settings.sync_interval > 0:
            tasks.append(asyncio.create_task(state["vault"].loop()))
        if state["nas_sync"] and settings.sync_interval > 0:
            tasks.append(asyncio.create_task(state["nas_sync"].loop()))
        yield
        if bridge:
            await bridge.stop()
        for task in tasks:
            task.cancel()

    app = FastAPI(title="rmbridge", lifespan=lifespan)

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if settings.password:
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(pw, settings.password)
                except ValueError:
                    pass
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="rmbridge"'})
        return await call_next(request)

    def backend() -> Backend:
        return state["backend"]

    async def guarded(coro):
        try:
            return await coro
        except RemarkableError as exc:
            raise HTTPException(502, str(exc)) from exc
        except nas.PathError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status():
        vault: VaultSync | None = state["vault"]
        nas_sync: NasSync | None = state["nas_sync"]
        return {
            "backend": backend().name,
            "remarkable": await backend().status(),
            "nas_root": str(settings.nas_root),
            "vault": None
            if vault is None
            else {
                "dir": str(settings.vault_dir),
                "rm_folder": settings.rm_vault_folder,
                "interval": settings.sync_interval,
                "last": vault.last_report,
            },
            "nas_sync": None
            if nas_sync is None
            else {
                "folders": [{"nas": n, "rm": r} for n, r in settings.nas_sync],
                "last": nas_sync.last_report,
            },
            "livesync": bridge.status() if bridge else None,
        }

    @app.get("/api/livesync/logs", response_class=PlainTextResponse)
    async def livesync_logs():
        if bridge is None:
            raise HTTPException(400, "LiveSync is not configured (LIVESYNC_URL in .env)")
        return "\n".join(bridge.logs)

    @app.post("/api/livesync/restart")
    async def livesync_restart():
        if bridge is None:
            raise HTTPException(400, "LiveSync is not configured (LIVESYNC_URL in .env)")
        await bridge.restart()
        return {"ok": True}

    @app.post("/api/backend")
    async def set_backend(body: BackendIn):
        state["backend"] = make_backend(settings, body.name)
        for sync in (state["vault"], state["nas_sync"]):
            if sync:
                sync.backend = state["backend"]
        return {"backend": body.name}

    @app.get("/api/nas")
    async def list_nas(path: str = ""):
        return await guarded(asyncio.to_thread(nas.list_dir, settings.nas_root, path))

    @app.get("/api/rm")
    async def list_rm(ref: str = ""):
        entries = await guarded(backend().list(ref))
        return [e.to_dict() for e in entries]

    @app.post("/api/send")
    async def send(body: SendIn):
        """Upload files from the NAS to the current reMarkable folder."""
        done, errors = [], []
        for rel in body.paths:
            try:
                src = nas.resolve(settings.nas_root, rel)
                if src.is_dir():
                    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in UPLOADABLE)
                else:
                    files = [src]
                for f in files:
                    await backend().upload(f, body.folder)
                    done.append(str(f.relative_to(settings.nas_root)))
            except (RemarkableError, nas.PathError) as exc:
                errors.append(f"{rel}: {exc}")
        return {"done": done, "errors": errors}

    @app.post("/api/upload")
    async def upload(folder: str = Form(""), files: list[UploadFile] = File(...)):
        """Upload files straight from the browser (drag & drop)."""
        done, errors = [], []
        with tempfile.TemporaryDirectory() as tmp:
            for up in files:
                name = safe_filename(Path(up.filename or "upload").name)
                if Path(name).suffix.lower() not in UPLOADABLE:
                    errors.append(f"{name}: only PDF and EPUB are supported")
                    continue
                path = Path(tmp) / name
                path.write_bytes(await up.read())
                try:
                    await backend().upload(path, folder)
                    done.append(name)
                except RemarkableError as exc:
                    errors.append(f"{name}: {exc}")
        return {"done": done, "errors": errors}

    @app.post("/api/pull")
    async def pull(body: PullIn):
        """Download documents from the reMarkable into a NAS folder."""
        dest = await guarded(asyncio.to_thread(nas.resolve, settings.nas_root, body.nas_path))
        done, errors = [], []
        for item in body.items:
            try:
                out = await backend().download(item.ref, dest, name=item.name)
                done.append(str(out.relative_to(settings.nas_root)))
            except RemarkableError as exc:
                errors.append(f"{item.name}: {exc}")
        return {"done": done, "errors": errors}

    @app.post("/api/vault/sync")
    async def vault_sync():
        if state["vault"] is None:
            raise HTTPException(400, "VAULT_DIR is not configured")
        return await state["vault"].run_once()

    @app.post("/api/nas-sync")
    async def nas_sync_now():
        if state["nas_sync"] is None:
            raise HTTPException(400, "NAS_SYNC is not configured")
        return await state["nas_sync"].run_once()

    return app
