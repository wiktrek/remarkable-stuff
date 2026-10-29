#!/usr/bin/env python3
"""Build the Chromium engine for the tablet (runs on your PC, no emulation).

Downloads Google's arm64 `chrome-headless-shell` (Chrome for Testing) and
collects every shared library it needs from Ubuntu 24.04 arm64 packages.
Ubuntu 24.04 has the same glibc as the Paper Pro (2.39), so the tablet's own
libc is used and only the missing libraries are shipped.

    browser/bundle-engine.py                # -> browser/build/engine
    browser/bundle-engine.py --version 156.0.8078.47 --cjk

Needs: python3, bsdtar, readelf.
"""

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

CFT_JSON = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
CFT_URL = "https://storage.googleapis.com/chrome-for-testing-public/{v}/linux-arm64/chrome-headless-shell-linux-arm64.zip"
UBUNTU = "http://ports.ubuntu.com/ubuntu-ports"
SUITES = ["noble", "noble-updates", "noble-security"]
COMPONENTS = ["main", "universe"]
LIB_DIRS = ("usr/lib/aarch64-linux-gnu/", "lib/aarch64-linux-gnu/")

# Provided by the tablet's glibc / gcc runtime.
SYSTEM = {
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
    "ld-linux-aarch64.so.1", "libresolv.so.2", "libutil.so.1", "libgcc_s.so.1",
}
# Loaded with dlopen(), so they never show up as NEEDED.
DLOPENED = ["libnssckbi.so", "libsoftokn3.so", "libfreeblpriv3.so", "libfreebl3.so"]
FONT_PACKAGES = ["fonts-liberation", "fonts-dejavu-core", "fonts-noto-color-emoji"]
CJK_PACKAGES = ["fonts-noto-cjk"]

FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir prefix="relative">.</dir>
  <dir>/usr/share/fonts</dir>
  <cachedir prefix="xdg">fontconfig</cachedir>
  <alias><family>sans-serif</family><prefer>
    <family>Liberation Sans</family><family>DejaVu Sans</family>
    <family>Noto Sans CJK SC</family><family>Noto Color Emoji</family>
  </prefer></alias>
  <alias><family>serif</family><prefer>
    <family>Liberation Serif</family><family>DejaVu Serif</family>
    <family>Noto Serif CJK SC</family><family>Noto Color Emoji</family>
  </prefer></alias>
  <alias><family>monospace</family><prefer>
    <family>Liberation Mono</family><family>DejaVu Sans Mono</family>
  </prefer></alias>
  <alias><family>emoji</family><prefer><family>Noto Color Emoji</family></prefer></alias>
  <alias><family>system-ui</family><prefer><family>Liberation Sans</family></prefer></alias>
  <alias binding="same"><family>Arial</family><accept><family>Liberation Sans</family></accept></alias>
  <alias binding="same"><family>Helvetica</family><accept><family>Liberation Sans</family></accept></alias>
  <alias binding="same"><family>Times New Roman</family><accept><family>Liberation Serif</family></accept></alias>
  <alias binding="same"><family>Times</family><accept><family>Liberation Serif</family></accept></alias>
  <alias binding="same"><family>Courier New</family><accept><family>Liberation Mono</family></accept></alias>
  <match target="font">
    <edit name="antialias" mode="assign"><bool>true</bool></edit>
    <edit name="hinting" mode="assign"><bool>true</bool></edit>
    <edit name="hintstyle" mode="assign"><const>hintslight</const></edit>
  </match>
</fontconfig>
"""


def log(msg):
    print(msg, flush=True)


def fetch(url, dest):
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.rename(dest)
    return dest


class Ubuntu:
    """Minimal apt: which package has a library, and where to download it."""

    def __init__(self, cache):
        self.cache = cache
        self.packages = {}  # name -> (version, Filename)
        for suite in SUITES:
            for comp in COMPONENTS:
                path = fetch(f"{UBUNTU}/dists/{suite}/{comp}/binary-arm64/Packages.gz",
                             cache / "index" / f"{suite}-{comp}-Packages.gz")
                self._read_packages(path)
        contents = fetch(f"{UBUNTU}/dists/noble/Contents-arm64.gz", cache / "index" / "Contents-arm64.gz")
        self.libs = {}  # basename -> (path in package, package, preference)
        with gzip.open(contents, "rt", errors="replace") as f:
            for line in f:
                path, _, owners = line.rstrip("\n").rpartition(" ")
                path = path.strip()
                if not path.startswith(LIB_DIRS) or ".so" not in path:
                    continue
                pkg = owners.split(",")[0].rsplit("/", 1)[-1]
                name = path.rsplit("/", 1)[-1]
                # Prefer the plain runtime library (lib dir itself, not a
                # subdirectory such as genwqe/libz.so.1, not -dev/-dbg).
                score = (pkg.endswith(("-dev", "-dbg")), path.count("/"))
                if name not in self.libs or score < self.libs[name][2]:
                    self.libs[name] = (path, pkg, score)

    def _read_packages(self, path):
        with gzip.open(path, "rt", errors="replace") as f:
            fields = {}
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    self._add(fields)
                    fields = {}
                elif not line.startswith(" ") and ":" in line:
                    k, _, v = line.partition(":")
                    fields[k] = v.strip()
            self._add(fields)

    def _add(self, fields):
        if "Package" in fields and "Filename" in fields:
            # Later suites (updates, security) win.
            self.packages[fields["Package"]] = (fields.get("Version"), fields["Filename"])

    def extract(self, pkg):
        """Unpack a package's files; returns the directory."""
        if pkg not in self.packages:
            sys.exit(f"package {pkg} not found in Ubuntu noble arm64")
        _, filename = self.packages[pkg]
        root = self.cache / "pkgs" / pkg
        if root.exists():
            return root
        deb = fetch(f"{UBUNTU}/{filename}", self.cache / "debs" / Path(filename).name)
        tmp = root.with_name(pkg + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        members = subprocess.run(["bsdtar", "-tf", str(deb)], check=True, capture_output=True, text=True).stdout.split()
        data = next(m for m in members if m.startswith("data.tar"))
        tar = subprocess.Popen(["bsdtar", "-xOf", str(deb), data], stdout=subprocess.PIPE)
        subprocess.run(["bsdtar", "-xf", "-", "-C", str(tmp)], stdin=tar.stdout, check=True)
        tar.wait()
        tmp.rename(root)
        return root


def resolve(root, rel):
    """Follow symlinks inside an unpacked package."""
    p = root / rel
    for _ in range(20):
        if not p.is_symlink():
            return p
        target = os.readlink(p)
        p = root / target.lstrip("/") if target.startswith("/") else p.parent / target
    return p


def needed(path):
    out = subprocess.run(["readelf", "-d", "--wide", str(path)], capture_output=True, text=True).stdout
    return [line.split("[", 1)[1].split("]", 1)[0] for line in out.splitlines() if "(NEEDED)" in line]


def is_elf(path):
    with open(path, "rb") as f:
        return f.read(4) == b"\x7fELF"


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=here / "build" / "engine")
    ap.add_argument("--cache", type=Path, default=here / "build" / "cache")
    ap.add_argument("--version", help="Chrome for Testing version (default: current stable)")
    ap.add_argument("--cjk", action="store_true", help="also bundle Chinese/Japanese/Korean fonts (~100 MB)")
    args = ap.parse_args()

    for tool in ("bsdtar", "readelf"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is required")

    version = args.version
    if not version:
        with urllib.request.urlopen(CFT_JSON) as r:
            version = json.load(r)["channels"]["Stable"]["version"]
    log(f"Chromium {version}")

    out = args.out
    shutil.rmtree(out, ignore_errors=True)
    (out / "lib").mkdir(parents=True)
    (out / "fonts").mkdir()

    log("Fetching chrome-headless-shell")
    zip_path = fetch(CFT_URL.format(v=version), args.cache / f"chrome-headless-shell-{version}.zip")
    subprocess.run(["bsdtar", "-xf", str(zip_path), "-C", str(out)], check=True)
    (out / "chrome-headless-shell-linux-arm64").rename(out / "chrome")

    log("Reading Ubuntu 24.04 arm64 package lists")
    ubuntu = Ubuntu(args.cache)

    log("Collecting libraries")
    chrome_dir = out / "chrome"
    have = {p.name for p in chrome_dir.iterdir()} | SYSTEM
    queue = list(DLOPENED)
    for p in chrome_dir.iterdir():
        if p.is_file() and (p.suffix == ".so" or ".so." in p.name or p.name == "chrome-headless-shell") and is_elf(p):
            queue += needed(p)
    missing = []
    while queue:
        name = queue.pop()
        if name in have:
            continue
        have.add(name)
        if name not in ubuntu.libs:
            missing.append(name)
            continue
        rel, pkg, _ = ubuntu.libs[name]
        src = resolve(ubuntu.extract(pkg), rel)
        shutil.copy2(src, out / "lib" / name)
        log(f"  {name:<32} {pkg}")
        queue += needed(src)
    if missing:
        log(f"  not found (probably fine if dlopen()ed optionally): {', '.join(sorted(missing))}")

    log("Collecting fonts")
    for pkg in FONT_PACKAGES + (CJK_PACKAGES if args.cjk else []):
        root = ubuntu.extract(pkg)
        for f in (root / "usr/share/fonts").rglob("*"):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc") and not f.is_symlink():
                shutil.copy2(f, out / "fonts" / f.name)
    (out / "fonts" / "fonts.conf").write_text(FONTS_CONF)

    (out / "VERSION").write_text(version + "\n")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    log(f"Engine ready: {out} ({size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
