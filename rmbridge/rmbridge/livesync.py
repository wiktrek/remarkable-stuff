"""Runs livesync-bridge (github.com/vrtmrz/livesync-bridge) as a child process.

livesync-bridge is a headless Self-hosted LiveSync client: it mirrors one
folder of the vault stored in CouchDB to a plain directory and back. rmbridge
writes its config from LIVESYNC_* env vars, keeps it running, and points the
vault sync at the mirrored directory.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

# Bridge output that means the CouchDB connection (or the bridge itself) is broken.
ERROR_LINE = re.compile(
    r"error|fail|exception|denied|refused|unauthori[sz]ed|forbidden|not ready|timed? ?out"
    r"|\b40[13]\b|ENOTFOUND|ECONNRESET|no passphrase|no obfuscate passphrase|looks like empty",
    re.IGNORECASE,
)


def bridge_config(settings: Settings) -> dict:
    ls = settings.livesync
    assert ls is not None and settings.vault_dir is not None
    couch = {
        "type": "couchdb",
        "name": "obsidian",
        "url": ls.url,
        "database": ls.database,
        "username": ls.username,
        "password": ls.password,
        "passphrase": ls.passphrase,
        "obfuscatePassphrase": ls.obfuscate_passphrase,
        # Only this vault folder is mirrored; the rest of the vault stays put.
        "baseDir": ls.vault_folder.strip("/") + "/",
        "useRemoteTweaks": True,
    }
    if ls.id_key:
        couch["idDerivationKey"] = ls.id_key
    storage = {
        "type": "storage",
        "name": "rmbridge-vault",
        "baseDir": str(settings.vault_dir) + "/",
        "scanOfflineChanges": True,
        "useChokidar": True,
    }
    return {"peers": [couch, storage]}


class LiveSyncBridge:
    def __init__(self, settings: Settings) -> None:
        assert settings.livesync is not None
        self.settings = settings
        self.workdir = settings.data_dir / "livesync"
        self.config_file = self.workdir / "config.json"
        self.health_file = self.workdir / "health.json"
        self.logs: collections.deque[str] = collections.deque(maxlen=300)
        self.proc: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.restarts = 0
        self._stopping = False
        self._restart_requested = False
        # Seconds between connection checks, and how long a fresh start may take to connect.
        self.health_interval = 15.0
        self.health_grace = 60.0

    def write_config(self) -> None:
        # Target of the $DENO_DIR/location_data symlink in the Docker image.
        (self.workdir / "location_data").mkdir(parents=True, exist_ok=True)
        self.settings.vault_dir.mkdir(parents=True, exist_ok=True)
        # Contains the CouchDB password and E2EE passphrase.
        fd = os.open(self.config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(bridge_config(self.settings), fh, indent=2)

    def _command(self) -> list[str]:
        ls = self.settings.livesync
        deno = shutil.which(ls.deno_bin)
        main = Path(ls.bridge_dir) / "main.ts"
        if deno is None:
            raise FileNotFoundError(f"deno not found ({ls.deno_bin})")
        if not main.exists():
            raise FileNotFoundError(f"livesync-bridge not found at {ls.bridge_dir}")
        return [deno, "run", "-A", str(main)]

    async def run(self) -> None:
        """Start the bridge and restart it whenever it exits."""
        watcher = asyncio.create_task(self._watch_health())
        try:
            await self._supervise()
        finally:
            watcher.cancel()

    async def _supervise(self) -> None:
        backoff = 5
        while not self._stopping:
            try:
                self.write_config()
                cmd = self._command()
            except (OSError, FileNotFoundError) as exc:
                self._log(f"[rmbridge] can't start livesync-bridge: {exc}", logging.ERROR)
                await asyncio.sleep(60)
                continue
            env = {
                **os.environ,
                "LSB_CONFIG": str(self.config_file),
                "LSB_HEALTH_FILE": str(self.health_file),
                "NO_COLOR": "1",
            }
            self.health_file.unlink(missing_ok=True)
            self._log("[rmbridge] starting livesync-bridge")
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.settings.livesync.bridge_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self.started_at = time.time()
            assert self.proc.stdout is not None
            async for line in self.proc.stdout:
                self._log(line.decode(errors="replace").rstrip())
            code = await self.proc.wait()
            self.proc = None
            if self._stopping:
                break
            if self._restart_requested:
                self._restart_requested = False
                backoff = 5
                continue
            uptime = time.time() - (self.started_at or 0)
            backoff = 5 if uptime > 300 else min(backoff * 2, 300)
            self.restarts += 1
            self._log(f"[rmbridge] livesync-bridge exited ({code}); restarting in {backoff}s", logging.WARNING)
            await asyncio.sleep(backoff)

    async def restart(self) -> None:
        if self.proc and self.proc.returncode is None:
            self._restart_requested = True
            self.proc.terminate()  # run() starts it again right away

    async def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except TimeoutError:
                self.proc.kill()

    def status(self) -> dict:
        ls = self.settings.livesync
        health = None
        try:
            health = json.loads(self.health_file.read_text())
        except (OSError, ValueError):
            pass
        running = self.proc is not None and self.proc.returncode is None
        # The bridge rewrites the file every 10 s; older means it's wedged.
        fresh = bool(health) and time.time() - health.get("ts", 0) / 1000 < 60
        return {
            "running": running,
            "ok": running and fresh and bool(health.get("ok")),
            "peers": (health or {}).get("peers", []),
            "url": ls.url,
            "database": ls.database,
            "vault_folder": ls.vault_folder,
            "restarts": self.restarts,
            "last_log": self.logs[-1] if self.logs else "",
        }

    async def _watch_health(self) -> None:
        """Log when the CouchDB connection comes up or goes down."""
        since = time.monotonic()
        was_ok: bool | None = None
        while True:
            await asyncio.sleep(self.health_interval)
            status = self.status()
            ok = status["ok"]
            if ok == was_ok or (not ok and was_ok is None and time.monotonic() - since < self.health_grace):
                continue
            target = f"{status['url']}/{status['database']}"
            if ok:
                log.info("LiveSync connected to %s (vault folder %s/)", target, status["vault_folder"])
            else:
                problem = next((l for l in reversed(self.logs) if ERROR_LINE.search(l)), status["last_log"])
                log.error(
                    "LiveSync is not connected to %s: %s (full log: /api/livesync/logs)",
                    target,
                    problem or "no output from livesync-bridge",
                )
            was_ok = ok

    def _log(self, line: str, level: int | None = None) -> None:
        if not line:
            return
        self.logs.append(line)
        if level is None:
            level = logging.WARNING if ERROR_LINE.search(line) else logging.DEBUG
        log.log(level, "livesync: %s", line)
