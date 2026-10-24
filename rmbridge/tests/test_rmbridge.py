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
from rmbridge.config import Settings, _nas_sync
from rmbridge.remarkable import UsbBackend
from rmbridge.nas_sync import NasSync
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


def _touch(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    os.utime(path, ns=(path.stat().st_mtime_ns + 10**9,) * 2)


async def test_vault_mirror(env):
    settings, rm_root = env
    sync = VaultSync(settings, create_app.__globals__["make_backend"](settings, "cloud"))
    vault = settings.vault_dir
    (vault / "Papers").mkdir(parents=True)
    (vault / "Papers" / "attention.pdf").write_bytes(b"%PDF attention")
    (vault / "note.md").write_text("two words\n")

    r = await sync.run_once()
    assert sorted(r["pushed"]) == ["Papers/attention.pdf", "note.md"] and r["errors"] == []
    assert (rm_root / "Obsidian/Papers/attention.pdf").exists()
    assert (rm_root / "Obsidian/note.pdf").read_bytes().startswith(b"%PDF")  # rendered note
    assert r["pulled"] == []

    # Unchanged on both sides -> nothing to do; our uploads don't bounce back.
    r = await sync.run_once()
    assert r["pushed"] == r["pulled"] == []

    # Written on the tablet: both pushed docs get ink, a new notebook appears.
    _touch(rm_root / "Obsidian/Papers/attention.pdf", b"%PDF attention + ink")
    _touch(rm_root / "Obsidian/note.pdf", b"%PDF note + ink")
    (rm_root / "Obsidian/Meeting.pdf").write_bytes(b"%PDF notebook")
    r = await sync.run_once()
    assert sorted(r["pulled"]) == ["Meeting.pdf", "Papers/attention.pdf", "note.pdf"]
    assert r["pushed"] == [] and r["errors"] == []
    assert (vault / "Papers/attention.pdf").read_bytes() == b"%PDF annotated %PDF attention + ink"
    assert (vault / "note.pdf").read_bytes() == b"%PDF annotated %PDF note + ink"
    assert (vault / "note.md").read_text() == "two words\n"
    assert (vault / "Meeting.pdf").exists()

    # Exports never go back to the tablet.
    r = await sync.run_once()
    assert r["pushed"] == r["pulled"] == []

    # Editing the note re-sends it to the same document; more ink updates the same export.
    _touch(vault / "note.md", b"two words, edited\n")
    r = await sync.run_once()
    assert r["pushed"] == ["note.md"] and r["pulled"] == []
    _touch(rm_root / "Obsidian/note.pdf", b"%PDF more ink")
    r = await sync.run_once()
    assert r["pulled"] == ["note.pdf"] and r["pushed"] == []
    assert sorted(p.name for p in rm_root.joinpath("Obsidian").iterdir()) == ["Meeting.pdf", "Papers", "note.pdf"]

    # A vault note named like a tablet notebook doesn't overwrite it.
    (vault / "Meeting.md").write_text("agenda\n")
    r = await sync.run_once()
    assert r["pushed"] == ["Meeting.md"]
    assert (rm_root / "Obsidian/Meeting (2).pdf").exists()
    assert (rm_root / "Obsidian/Meeting.pdf").read_bytes() == b"%PDF notebook"


def test_nas_sync_setting():
    assert _nas_sync("Books, Papers/ML:/Papers/ ,") == (("Books", "/Books"), ("Papers/ML", "/Papers"))
    assert _nas_sync("") == ()


async def test_nas_sync(env):
    settings, rm_root = env
    settings = replace(settings, nas_sync=(("Books", "/Library"),))
    sync = NasSync(settings, create_app.__globals__["make_backend"](settings, "cloud"))
    books = settings.nas_root / "Books"
    (books / "SciFi").mkdir()
    (books / "SciFi" / "dune.epub").write_bytes(b"EPUB dune")
    (books / "cover.jpg").write_bytes(b"jpg")
    (rm_root / "Library").mkdir()
    (rm_root / "Library" / "paper.pdf").write_bytes(b"%PDF already there")

    r = await sync.run_once()
    assert r["pushed"] == ["Books/SciFi/dune.epub"] and r["errors"] == []
    assert (rm_root / "Library/SciFi/dune.epub").exists()
    assert (rm_root / "Library/paper.pdf").read_bytes() == b"%PDF already there"

    # New books go over; deleted-on-tablet and changed-on-NAS books are left alone.
    (rm_root / "Library/SciFi/dune.epub").unlink()
    _touch(books / "paper.pdf", b"%PDF paper v2")
    (books / "new.pdf").write_bytes(b"%PDF new")
    r = await sync.run_once()
    assert r["pushed"] == ["Books/new.pdf"] and r["errors"] == []
    assert not (rm_root / "Library/SciFi/dune.epub").exists()

    r = await sync.run_once()
    assert r["pushed"] == [] and r["errors"] == []


def test_short_note_is_one_page(tmp_path):
    import weasyprint

    from rmbridge.render import note_html

    note = tmp_path / "test.md"
    note.write_text("---\ntags: [x]\n---\ntest [[Other|remarkable]] ==now==\n\n- [ ] task\n")
    page = note_html(note, tmp_path)
    assert "<h1>test</h1>" in page and "test remarkable <mark>now</mark>" in page
    assert "☐ task" in page and "tags" not in page
    assert len(weasyprint.HTML(string=page).render().pages) == 1


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
