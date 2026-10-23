"""Stand-in for the rmapi CLI, backed by a directory tree (FAKE_RM_ROOT)."""

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

root = Path(os.environ["FAKE_RM_ROOT"])
args = [a for a in sys.argv[1:] if a != "-ni"]
as_json = "-json" in args
args = [a for a in args if a != "-json"]
cmd, rest = args[0], args[1:]


def node(path: str) -> Path:
    return root / path.strip("/")


if cmd == "version":
    print("fake")
elif cmd == "ls":
    target = node(rest[0] if rest else "/")
    nodes = [
        {"id": p.name, "name": p.stem if p.is_file() else p.name,
         "type": "CollectionType" if p.is_dir() else "DocumentType",
         "modifiedClient": str(p.stat().st_mtime_ns)}
        for p in sorted(target.iterdir())
    ]
    print(json.dumps(nodes) if as_json else "\n".join(n["name"] for n in nodes))
elif cmd == "mkdir":
    node(rest[0]).mkdir(exist_ok=True)
elif cmd == "put":
    force = "--force" in rest
    src, folder = [a for a in rest if not a.startswith("--")]
    dest = node(folder) / Path(src).name
    if dest.exists() and not force:
        sys.exit("entry already exists")
    shutil.copy(src, dest)
elif cmd in ("geta", "get"):
    path = rest[-1]
    matches = list(node(path).parent.glob(node(path).name + ".*"))
    if not matches:
        sys.exit("file doesn't exist")
    doc = matches[0]
    if cmd == "geta":
        if doc.suffix == ".epub":
            sys.exit("only pdf and notebooks supported")
        Path(f"{doc.stem}.zip").write_bytes(b"zip")
        Path(f"{doc.stem}-annotations.pdf").write_bytes(b"%PDF annotated " + doc.read_bytes())
    else:
        with zipfile.ZipFile(f"{doc.stem}.rmdoc", "w") as zf:
            zf.writestr(f"abc{doc.suffix}", doc.read_bytes())
            zf.writestr("abc.content", "{}")
else:
    sys.exit(f"unknown command {cmd}")
