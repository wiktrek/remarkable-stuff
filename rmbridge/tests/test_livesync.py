import asyncio
import json
import stat
import sys
from dataclasses import replace

import pytest

from rmbridge.config import LiveSyncSettings, Settings
from rmbridge.livesync import LiveSyncBridge, bridge_config

ENV = {
    "LIVESYNC_URL": "https://couch.lan/",
    "LIVESYNC_DATABASE": "vault",
    "LIVESYNC_USERNAME": "me",
    "LIVESYNC_PASSWORD": "pw",
    "LIVESYNC_PASSPHRASE": "secret",
    "LIVESYNC_OBFUSCATE_PASSPHRASE": "secret",
}


@pytest.fixture
def ls_env(monkeypatch, tmp_path):
    for key in list(ENV) + ["LIVESYNC_ID_KEY", "LIVESYNC_VAULT_FOLDER", "VAULT_DIR", "NAS_ROOT"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "state"))
    return tmp_path


def test_settings_from_env(ls_env, monkeypatch):
    settings = Settings.from_env()
    assert settings.livesync.url == "https://couch.lan"
    assert settings.vault_dir == ls_env / "state" / "vault"

    monkeypatch.setenv("LIVESYNC_URL", "")
    assert Settings.from_env().livesync is None


def test_missing_credentials_is_a_clear_error(ls_env, monkeypatch):
    monkeypatch.setenv("LIVESYNC_PASSWORD", "")
    with pytest.raises(SystemExit, match="LIVESYNC_PASSWORD"):
        LiveSyncSettings.from_env()


def test_bridge_config(ls_env, monkeypatch):
    monkeypatch.setenv("LIVESYNC_ID_KEY", "sls-id-v1:abc")
    monkeypatch.setenv("LIVESYNC_VAULT_FOLDER", "/Tablet/")
    settings = Settings.from_env()
    couch, storage = bridge_config(settings)["peers"]
    assert couch["baseDir"] == "Tablet/"
    assert couch["idDerivationKey"] == "sls-id-v1:abc"
    assert couch["passphrase"] == couch["obfuscatePassphrase"] == "secret"
    assert storage["baseDir"] == str(settings.vault_dir) + "/"


async def test_supervisor_runs_and_restarts(ls_env, tmp_path):
    # A stand-in for `deno run -A main.ts`: logs, writes a health file, exits.
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "main.ts").write_text("")
    deno = tmp_path / "deno"
    deno.write_text(
        f"#!{sys.executable}\n"
        "import json, os, time\n"
        "cfg = json.load(open(os.environ['LSB_CONFIG']))\n"
        "print('peers', len(cfg['peers']), flush=True)\n"
        "json.dump({'ts': time.time() * 1000, 'ok': True, 'peers': []}, open(os.environ['LSB_HEALTH_FILE'], 'w'))\n"
        "time.sleep(0.3)\n"
    )
    deno.chmod(deno.stat().st_mode | stat.S_IEXEC)
    settings = Settings.from_env()
    settings = replace(settings, livesync=replace(settings.livesync, bridge_dir=str(bridge_dir), deno_bin=str(deno)))
    bridge = LiveSyncBridge(settings)

    task = asyncio.create_task(bridge.run())
    for _ in range(50):
        await asyncio.sleep(0.05)
        if bridge.status()["ok"]:
            break
    status = bridge.status()
    assert status["running"] and status["ok"]
    assert "peers 2" in bridge.logs
    assert (bridge.config_file.stat().st_mode & 0o777) == 0o600
    assert json.loads(bridge.config_file.read_text())["peers"][0]["password"] == "pw"

    await asyncio.sleep(0.5)  # child exits -> supervisor schedules a restart
    assert bridge.restarts == 1 and not bridge.status()["running"]
    await bridge.stop()
    task.cancel()


async def test_manual_restart_is_immediate(ls_env, tmp_path):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "main.ts").write_text("")
    deno = tmp_path / "deno"
    deno.write_text(f"#!{sys.executable}\nimport time\nprint('up', flush=True)\ntime.sleep(30)\n")
    deno.chmod(deno.stat().st_mode | stat.S_IEXEC)
    settings = Settings.from_env()
    settings = replace(settings, livesync=replace(settings.livesync, bridge_dir=str(bridge_dir), deno_bin=str(deno)))
    bridge = LiveSyncBridge(settings)
    task = asyncio.create_task(bridge.run())
    while list(bridge.logs).count("up") < 1:
        await asyncio.sleep(0.05)
    await bridge.restart()
    async with asyncio.timeout(3):
        while list(bridge.logs).count("up") < 2:
            await asyncio.sleep(0.05)
    assert bridge.restarts == 0
    await bridge.stop()
    task.cancel()


async def test_connection_problems_are_logged(ls_env, tmp_path, caplog):
    # Bridge that can't reach CouchDB: complains and never reports healthy.
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "main.ts").write_text("")
    deno = tmp_path / "deno"
    deno.write_text(
        f"#!{sys.executable}\nimport time\n"
        "print('CouchDB not ready: HTTP 401', flush=True)\n"
        "print('retrying', flush=True)\ntime.sleep(30)\n"
    )
    deno.chmod(deno.stat().st_mode | stat.S_IEXEC)
    settings = Settings.from_env()
    settings = replace(settings, livesync=replace(settings.livesync, bridge_dir=str(bridge_dir), deno_bin=str(deno)))
    bridge = LiveSyncBridge(settings)
    bridge.health_interval, bridge.health_grace = 0.05, 0.2

    caplog.set_level("INFO", logger="rmbridge.livesync")
    task = asyncio.create_task(bridge.run())
    async with asyncio.timeout(3):
        while not any(r.levelname == "ERROR" for r in caplog.records):
            await asyncio.sleep(0.05)
    await bridge.stop()
    task.cancel()

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert warnings == ["livesync: CouchDB not ready: HTTP 401"]
    assert len(errors) == 1
    assert "not connected to https://couch.lan/vault: CouchDB not ready: HTTP 401" in errors[0]
