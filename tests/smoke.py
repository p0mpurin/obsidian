"""Dependency-free smoke check for the first dashboard rebuild slice.

Run with: python tests/smoke.py
It uses a temporary mock Minecraft root and never contacts a provider or systemd.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import tarfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(tempfile.mkdtemp(prefix="minecraftdash-smoke-"))
os.environ.update({
    "MC_MOCK": "1",
    "MC_ROOT_DIR": str(ROOT / "minecraft"),
    "MC_DB_FILE": str(ROOT / "dashboard" / "servers.db"),
    "MC_DEFAULT_PORT": "25565",
    "MC_PORT_MIN": "25565",
    "MC_PORT_MAX": "25570",
})

import app  # noqa: E402


def main() -> None:
    try:
        catalog = app._minecraft_versions()
        assert catalog["versions"][0]["type"] == "release"

        request = app.CreateServerRequest(
            display_name="Smoke world",
            id="smoke-world",
            minecraft_version=catalog["versions"][0]["id"],
            distribution="vanilla",
            eula_accepted=True,
        )
        config = app._create_config(request)
        job_id = str(uuid.uuid4())
        db = app._db()
        db.execute(
            "INSERT INTO servers (id, name, version, software, port, min_ram, max_ram, created_at, install_state, loader_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (config["id"], config["name"], config["version"], config["software"], config["port"], config["min_ram"], config["max_ram"], app._now(), "queued", config["loader_version"], app._now()),
        )
        db.execute(
            "INSERT INTO jobs (id, kind, server_id, state, phase, progress, message, payload, created_at) VALUES (?, 'provision', ?, 'queued', 'queued', 0, 'queued', ?, ?)",
            (job_id, config["id"], json.dumps(config), app._now()),
        )
        db.commit()
        db.close()

        app._provision_job_sync(job_id)
        job = app._job_payload(app._job_row(job_id))
        server = app._status(app._server_row(config["id"]))
        assert job["state"] == "completed", job
        assert server["install_state"] == "ready", server
        assert server["allowed_actions"]["start"] is True, server
        assert (app._instance_dir(config["id"]) / "eula.txt").read_text(encoding="utf-8") == "eula=true\n"

        properties = app._validate_properties(config["id"], {"difficulty": "hard", "max-players": "42", "pvp": "false"})
        app._save_properties(config["id"], properties)
        assert app._read_properties(config["id"])["max-players"] == "42"

        db = app._db()
        db.execute("UPDATE servers SET software = 'paper' WHERE id = ?", (config["id"],))
        db.commit()
        db.close()
        addon_root, addon_kind = app._addon_root(config["id"])
        assert addon_root.name == "plugins"
        assert addon_kind == "Plugin"
        (addon_root / "example.jar").write_bytes(b"test plugin")
        assert app._mods(config["id"])[0]["filename"] == "example.jar"

        backup = app._create_backup(config["id"])
        assert app._backups(config["id"])[0]["filename"] == backup["filename"]
        with tarfile.open(app._backup_path(config["id"], backup["filename"]), "r:gz") as archive:
            assert "plugins/example.jar" in archive.getnames()

        original_systemd_properties = app._systemd_properties
        app._systemd_properties = lambda _server_id: {"ActiveState": "deactivating", "SubState": "stop-sigint", "ActiveEnterTimestampMonotonic": "0"}
        stopping = app._status(app._server_row(config["id"]))
        app._systemd_properties = original_systemd_properties
        assert stopping["lifecycle"] == "stopping"
        assert stopping["allowed_actions"]["delete"] is False
        print("smoke check passed")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
