"""Browsing the NAS share, confined to NAS_ROOT."""

from __future__ import annotations

from pathlib import Path

from .remarkable import UPLOADABLE


class PathError(ValueError):
    pass


def resolve(root: Path, rel: str) -> Path:
    """Map a UI path ("Books/Papers") to a real path, refusing to escape root."""
    target = (root / rel.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise PathError(f"{rel!r} is outside the NAS root")
    return target


def list_dir(root: Path, rel: str) -> list[dict]:
    folder = resolve(root, rel)
    if not folder.is_dir():
        raise PathError(f"{rel!r} is not a folder")
    entries = []
    for child in folder.iterdir():
        if child.name.startswith("."):
            continue
        is_dir = child.is_dir()
        if not is_dir and child.suffix.lower() not in UPLOADABLE:
            continue
        stat = child.stat()
        entries.append(
            {
                "path": str(child.relative_to(root)),
                "name": child.name,
                "kind": "folder" if is_dir else "file",
                "size": 0 if is_dir else stat.st_size,
                "modified": stat.st_mtime,
            }
        )
    return sorted(entries, key=lambda e: (e["kind"] != "folder", e["name"].lower()))
