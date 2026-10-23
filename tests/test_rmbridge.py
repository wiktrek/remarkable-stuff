import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from rmbridge.app import create_app
from rmbridge.config import Settings
from rmbridge.remarkable import UsbBackend
from rmbridge.vault import VaultSync

HERE = Path(__file__).parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    rm_root = tmp_path / "tablet"
    rm_root.mkdir()
    shim = tmp_path / "rmapi"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_rmapi.py'} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_RM_ROOT", str(rm_root))
    nas_root = tmp_path / "nas"
    (nas_root / "Books").mkdir(parents=True)
    (nas_root / "Books" / "paper.pdf").write_bytes(b"%PDF paper")
    (nas_root / "notes.txt").write_text("ignored")
    settings = Settings(
        nas_root=nas_root, backend="cloud", rmapi_bin=str(shim), usb_url="http://usb",
        data_dir=tmp_path / "state", vault_dir=tmp_path / "vault",
        vault_outbox="reMarkable/Outbox", vault_inbox="reMarkable/Inbox",
        rm_vault_folder="/Obsidian", sync_interval=0, password=None,
    )
    return settings, rm_root


def test_nas_listing_and_traversal(env):
    settings, _ = env
    client = TestClient(create_app(settings))
    names = [e["name"] for e in client.get("/api/nas").json()]
    assert names == ["Books"]  # non-PDF files are hidden
    assert client.get("/api/nas", params={"path": "../.."}).status_code == 400


def test_send_and_pull_via_cloud(env):
    settings, rm_root = env
    client = TestClient(create_app(settings))
    r = client.post("/api/send", json={"paths": ["Books"], "folder": "/"}).json()
    assert r == {"done": ["Books/paper.pdf"], "errors": []}
    assert (rm_root / "paper.pdf").exists()

    listing = client.get("/api/rm", params={"ref": "/"}).json()
    assert listing[0]["name"] == "paper" and listing[0]["ref"] == "/paper"

    r = client.post("/api/pull", json={"items": [{"ref": "/paper", "name": "paper"}], "nas_path": ""}).json()
    assert r["errors"] == []
    assert (settings.nas_root / "paper.pdf").read_bytes().startswith(b"%PDF annotated")


def test_pull_epub_falls_back_to_original(env):
    settings, rm_root = env
    (rm_root / "book.epub").write_bytes(b"EPUB")
    client = TestClient(create_app(settings))
    r = client.post("/api/pull", json={"items": [{"ref": "/book", "name": "book"}]}).json()
    assert r["done"] == ["book.epub"]


def test_browser_upload_rejects_other_types(env):
    settings, rm_root = env
    client = TestClient(create_app(settings))
    r = client.post("/api/upload", data={"folder": "/"}, files=[
        ("files", ("a.pdf", b"%PDF a", "application/pdf")),
        ("files", ("b.docx", b"x", "application/octet-stream")),
    ]).json()
    assert r["done"] == ["a.pdf"] and len(r["errors"]) == 1
    assert (rm_root / "a.pdf").exists()


def test_password(env):
    settings, _ = env
    client = TestClient(create_app(replace(settings, password="hunter2")))
    assert client.get("/api/nas").status_code == 401
    assert client.get("/api/nas", auth=("x", "hunter2")).status_code == 200


async def test_vault_roundtrip(env):
    settings, rm_root = env
    sync = VaultSync(settings, create_app.__globals__["make_backend"](settings, "cloud"))
    outbox = settings.vault_dir / "reMarkable/Outbox"
    (outbox / "Papers").mkdir(parents=True)
    (outbox / "Papers" / "attention.pdf").write_bytes(b"%PDF attention")

    r = await sync.run_once()
    assert r["pushed"] == ["Papers/attention.pdf"] and r["errors"] == []
    assert (rm_root / "Obsidian/Papers/attention.pdf").exists()
    assert r["pulled"] == []  # our own upload doesn't bounce back

    # Unchanged on both sides -> nothing to do.
    r = await sync.run_once()
    assert r["pushed"] == r["pulled"] == []

    # Written on the tablet: an existing doc gets annotated, a new notebook appears.
    doc = rm_root / "Obsidian/Papers/attention.pdf"
    doc.write_bytes(b"%PDF attention + ink")
    os.utime(doc, ns=(doc.stat().st_mtime_ns + 10**9,) * 2)
    (rm_root / "Obsidian/Meeting.pdf").write_bytes(b"%PDF notebook")
    r = await sync.run_once()
    assert sorted(r["pulled"]) == ["Meeting.pdf", "Papers/attention.pdf"]
    inbox = settings.vault_dir / "reMarkable/Inbox"
    note = (inbox / "Meeting.md").read_text()
    assert "![[Meeting.pdf]]" in note and 'remarkable_path: "/Obsidian/Meeting"' in note
    assert json.loads((settings.data_dir / "vault-state.json").read_text())["pulled"]


async def test_usb_backend(tmp_path):
    uploads = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/documents/":
            return httpx.Response(200, json=[
                {"ID": "f1", "VissibleName": "Books", "Type": "CollectionType"},
                {"ID": "d1", "VissibleName": "Notes", "Type": "DocumentType", "fileType": "notebook"},
            ])
        if req.url.path == "/documents/f1":
            return httpx.Response(200, json=[])
        if req.url.path == "/upload":
            uploads.append(req.content)
            return httpx.Response(201)
        if req.url.path == "/download/d1/pdf":
            return httpx.Response(200, content=b"%PDF rendered")
        return httpx.Response(404)

    usb = UsbBackend("http://usb")
    usb._client = lambda timeout=30: httpx.AsyncClient(base_url="http://usb", transport=httpx.MockTransport(handler))
    entries = await usb.list("")
    assert [(e.name, e.kind) for e in entries] == [("Books", "folder"), ("Notes", "doc")]
    assert await usb.ensure_folder("/Books") == "f1"
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF x")
    await usb.upload(pdf, "f1")
    assert b'filename="x.pdf"' in uploads[0]
    out = await usb.download("d1", tmp_path / "out", name="Notes")
    assert out.read_bytes() == b"%PDF rendered"
