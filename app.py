from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ROOT_DIR = Path(os.getenv("MC_ROOT_DIR", "/srv/minecraft")).expanduser()
INSTANCES_DIR = ROOT_DIR / "instances"
TRASH_DIR = ROOT_DIR / ".trash"
DB_FILE = Path(os.getenv("MC_DB_FILE", str(BASE_DIR / "data/servers.db"))).expanduser()
MC_HOST = os.getenv("MC_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("MC_DEFAULT_PORT", "25565"))
PORT_MIN = int(os.getenv("MC_PORT_MIN", "25565"))
PORT_MAX = int(os.getenv("MC_PORT_MAX", "25665"))
SHUTDOWN_TIMEOUT = int(os.getenv("MC_SHUTDOWN_TIMEOUT", "60"))
MAX_UPLOAD_BYTES = int(os.getenv("MC_MAX_UPLOAD_BYTES", str(250 * 1024 * 1024)))
MOCK_MODE = os.getenv("MC_MOCK", "0") == "1"
MAX_COMMAND_LENGTH = 512
CONTROL_HELPER = os.getenv("MC_CONTROL_HELPER", "/usr/local/sbin/minecraft-dashboard-ctl")
DOWNLOAD_USER_AGENT = os.getenv(
    "MC_DOWNLOAD_USER_AGENT",
    "Obsidian-Minecraft-Dashboard/2.0 (https://github.com/purin/minecraftdash)",
)
CATALOG_TTL_SECONDS = int(os.getenv("MC_CATALOG_TTL_SECONDS", "21600"))

app = FastAPI(title="Obsidian Minecraft Control Room")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
SERVER_LOCKS: dict[str, asyncio.Lock] = {}
MOCK_STATES: dict[str, float | None] = {}

PROPERTY_FIELDS: dict[str, dict[str, Any]] = {
    "motd": {"default": "A Minecraft Server", "kind": "text"},
    "difficulty": {"default": "easy", "kind": "select", "options": ["peaceful", "easy", "normal", "hard"]},
    "gamemode": {"default": "survival", "kind": "select", "options": ["survival", "creative", "adventure", "spectator"]},
    "max-players": {"default": "20", "kind": "number", "min": 1, "max": 1000},
    "online-mode": {"default": "true", "kind": "boolean"},
    "pvp": {"default": "true", "kind": "boolean"},
    "allow-flight": {"default": "false", "kind": "boolean"},
    "white-list": {"default": "false", "kind": "boolean"},
    "view-distance": {"default": "10", "kind": "number", "min": 2, "max": 32},
    "simulation-distance": {"default": "10", "kind": "number", "min": 2, "max": 32},
    "spawn-protection": {"default": "16", "kind": "number", "min": 0, "max": 128},
    "server-port": {"default": "25565", "kind": "number", "min": 1, "max": 65535},
}
ALLOWED_SOFTWARE = {"vanilla", "paper", "fabric"}
DISTRIBUTIONS: dict[str, dict[str, Any]] = {
    "vanilla": {"name": "Vanilla", "description": "The official Minecraft server. Best for a pure survival world.", "addons": "datapacks", "available": True},
    "paper": {"name": "Paper", "description": "A fast server with Bukkit and Paper plugins.", "addons": "plugins", "available": True},
    "fabric": {"name": "Fabric", "description": "A lightweight mod loader for Fabric mods.", "addons": "mods", "available": True},
    "forge": {"name": "Forge", "description": "An installer-based mod loader. Provider support is being added safely.", "addons": "mods", "available": False},
    "neoforge": {"name": "NeoForge", "description": "An installer-based modern mod loader. Provider support is being added safely.", "addons": "mods", "available": False},
}
SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._-]{1,15}$")
RAM_RE = re.compile(r"^[1-9][0-9]{0,3}[MG]$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorldSettings(BaseModel):
    seed: str | None = Field(default=None, max_length=128)
    gamemode: str = "survival"
    difficulty: str = "normal"
    max_players: int = Field(default=20, ge=1, le=1000)
    whitelist: bool = False
    online_mode: bool = True
    pvp: bool = True


class CreateServerRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    id: str | None = None
    minecraft_version: str = Field(min_length=1, max_length=32)
    distribution: str = "vanilla"
    distribution_version: str = "latest-stable"
    port: int | None = Field(default=None, ge=1, le=65535)
    min_ram: str = "1G"
    max_ram: str = "2G"
    motd: str | None = Field(default=None, max_length=300)
    world: WorldSettings = Field(default_factory=WorldSettings)
    eula_accepted: bool = False


def _job_row(job_id: str) -> sqlite3.Row:
    db = _db()
    try:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        db.close()
    if not row:
        raise HTTPException(status_code=404, detail="Provisioning job not found")
    return row


def _job_payload(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value.pop("payload", None)
    return value


def _set_job(job_id: str, *, state: str | None = None, phase: str | None = None,
             progress: int | None = None, message: str | None = None,
             error_code: str | None = None, error_detail: str | None = None,
             finished: bool = False) -> None:
    updates: list[str] = []
    values: list[Any] = []
    for key, value in (("state", state), ("phase", phase), ("progress", progress),
                       ("message", message), ("error_code", error_code),
                       ("error_detail", error_detail)):
        if value is not None:
            updates.append(f"{key} = ?")
            values.append(value)
    if finished:
        updates.append("finished_at = ?")
        values.append(_now())
    if not updates:
        return
    values.append(job_id)
    db = _db()
    try:
        db.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
    finally:
        db.close()


def _cache_get(cache_key: str) -> tuple[Any | None, bool]:
    db = _db()
    try:
        row = db.execute("SELECT payload, expires_at FROM catalog_cache WHERE cache_key = ?", (cache_key,)).fetchone()
    finally:
        db.close()
    if not row:
        return None, False
    try:
        return json.loads(row["payload"]), float(row["expires_at"]) >= time.time()
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, False


def _cache_put(cache_key: str, payload: Any, ttl: int = CATALOG_TTL_SECONDS) -> None:
    now = time.time()
    db = _db()
    try:
        db.execute(
            "INSERT INTO catalog_cache(cache_key, payload, fetched_at, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
            (cache_key, json.dumps(payload), now, now + ttl),
        )
        db.commit()
    finally:
        db.close()


def _db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            software TEXT NOT NULL,
            port INTEGER NOT NULL UNIQUE,
            min_ram TEXT NOT NULL,
            max_ram TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # These additive migrations keep the prototype database usable in place.
    # A later release can replace this with numbered migration files without
    # forcing the current deployed dashboard to lose its server list.
    existing = {row[1] for row in db.execute("PRAGMA table_info(servers)")}
    for column, definition in {
        "install_state": "TEXT NOT NULL DEFAULT 'ready'",
        "loader_version": "TEXT",
        "eula_accepted_at": "TEXT",
        "install_error": "TEXT",
        "updated_at": "TEXT",
    }.items():
        if column not in existing:
            db.execute(f"ALTER TABLE servers ADD COLUMN {column} {definition}")
    db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            server_id TEXT NOT NULL,
            state TEXT NOT NULL,
            phase TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            error_code TEXT,
            error_detail TEXT,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS catalog_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    db.commit()
    return db


def _server_row(server_id: str) -> sqlite3.Row:
    db = _db()
    try:
        row = db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
    finally:
        db.close()
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")
    return row


def _instance_dir(server_id: str) -> Path:
    if not SERVER_ID_RE.fullmatch(server_id):
        raise HTTPException(status_code=400, detail="Invalid server id")
    path = (INSTANCES_DIR / server_id).resolve()
    if path.parent != INSTANCES_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid instance path")
    return path


def _minecraft_varint(value: int) -> bytes:
    output = bytearray()
    value &= 0xFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(output)


def _read_varint(sock: socket.socket) -> int:
    value = 0
    shift = 0
    for _ in range(5):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("Minecraft status connection closed")
        value |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return value
        shift += 7
    raise ValueError("Invalid Minecraft VarInt")


def _minecraft_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _minecraft_varint(len(encoded)) + encoded


def _query_server(port: int) -> tuple[int | None, int | None]:
    with socket.create_connection((MC_HOST, port), timeout=1.25) as sock:
        handshake = b"\x00" + _minecraft_string(MC_HOST) + struct.pack(">H", port) + b"\x01"
        sock.sendall(_minecraft_varint(len(handshake)) + handshake + b"\x01\x00")
        packet_length = _read_varint(sock)
        packet = bytearray()
        while len(packet) < packet_length:
            chunk = sock.recv(packet_length - len(packet))
            if not chunk:
                break
            packet.extend(chunk)
        if not packet or packet[0] != 0:
            raise ValueError("Invalid Minecraft status response")
        index = 1
        payload_length = 0
        shift = 0
        for _ in range(5):
            byte = packet[index]
            index += 1
            payload_length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        payload = json.loads(bytes(packet[index:index + payload_length]).decode("utf-8"))
        players = payload.get("players") or {}
        return players.get("online"), players.get("max")


def _fallback_log_players(log_file: Path) -> tuple[int | None, int | None]:
    try:
        content = log_file.read_text(encoding="utf-8", errors="ignore")[-20000:]
    except OSError:
        return None, None
    matches = list(re.finditer(r"There are (\d+) of a max of (\d+) players online", content))
    return (int(matches[-1].group(1)), int(matches[-1].group(2))) if matches else (None, None)


def _systemd_properties(server_id: str) -> dict[str, str]:
    if MOCK_MODE:
        started = MOCK_STATES.get(server_id)
        return {"ActiveState": "active" if started else "inactive", "SubState": "running" if started else "dead", "ActiveEnterTimestampMonotonic": str(int(started * 1_000_000)) if started else "0"}
    unit = f"minecraft@{server_id}.service"
    result = subprocess.run(["systemctl", "show", unit, "--property=ActiveState", "--property=SubState", "--property=ActiveEnterTimestampMonotonic"], capture_output=True, text=True, check=False, timeout=12)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _status(row: sqlite3.Row) -> dict[str, Any]:
    server_id = row["id"]
    values = _systemd_properties(server_id)
    state = values.get("ActiveState", "unknown")
    substate = values.get("SubState", "unknown")
    running = state == "active"
    uptime_seconds: int | None = None
    started = values.get("ActiveEnterTimestampMonotonic", "0")
    if running and started.isdigit() and int(started) > 0:
        uptime_seconds = int(max(0, time.monotonic() - int(started) / 1_000_000))
    online, maximum = (None, None)
    if running:
        if MOCK_MODE:
            online, maximum = 2, 20
        else:
            try:
                online, maximum = _query_server(row["port"])
            except (OSError, ValueError, json.JSONDecodeError, ConnectionError):
                online, maximum = _fallback_log_players(_instance_dir(server_id) / "logs/latest.log")
    install_state = row["install_state"] if "install_state" in row.keys() else "ready"
    failed = state == "failed" or substate in {"failed", "auto-restart"} or install_state in {"failed", "repair_required"}
    preparing = install_state in {"queued", "provisioning"}
    transitioning = state in {"activating", "deactivating"}
    allowed_actions = {
        "start": state in {"inactive", "failed"} and not preparing and install_state == "ready",
        # Stop must remain possible while a unit is activating or crashing.
        "stop": state in {"active", "activating", "deactivating", "failed"} or substate in {"running", "start", "auto-restart", "failed"},
        "restart": not transitioning and not preparing and install_state == "ready",
        "repair": failed or install_state == "repair_required",
        "delete": state == "inactive" and not preparing,
    }
    return {
        **dict(row), "running": running, "state": state, "substate": substate,
        "lifecycle": "provisioning" if preparing else "failed" if failed else "starting" if state == "activating" else "stopping" if state == "deactivating" else "running" if running else "stopped",
        "allowed_actions": allowed_actions,
        "uptime_seconds": uptime_seconds, "players_online": online,
        "players_max": maximum, "players_available": online is not None,
    }


def _control(action: str, server_id: str) -> None:
    if MOCK_MODE:
        if action == "start":
            MOCK_STATES[server_id] = time.monotonic()
        elif action == "stop":
            MOCK_STATES[server_id] = None
        return
    result = subprocess.run(["sudo", "-n", CONTROL_HELPER, action, server_id], capture_output=True, text=True, check=False, timeout=15)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or f"systemctl {action} failed").strip())


def _validate_id(value: Any) -> str:
    server_id = str(value or "").strip().lower()
    if not SERVER_ID_RE.fullmatch(server_id):
        raise HTTPException(status_code=400, detail="Use 2–32 lowercase letters, numbers, or hyphens")
    return server_id


def _validate_ram(value: Any, field: str) -> str:
    result = str(value or "").strip().upper()
    if not RAM_RE.fullmatch(result):
        raise HTTPException(status_code=400, detail=f"{field} must look like 1G or 1024M")
    return result


def _validate_port(value: Any) -> int:
    try:
        port = int(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Port must be a number") from exc
    if not PORT_MIN <= port <= PORT_MAX:
        raise HTTPException(status_code=400, detail=f"Port must be between {PORT_MIN} and {PORT_MAX}")
    return port


def _validate_create(payload: dict[str, Any]) -> dict[str, Any]:
    server_id = _validate_id(payload.get("id") or payload.get("name"))
    name = str(payload.get("name") or server_id).strip()[:64]
    version = str(payload.get("version") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Server name is required")
    if not VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="Enter a valid Minecraft version")
    software = str(payload.get("software") or "vanilla").lower()
    if software not in ALLOWED_SOFTWARE:
        raise HTTPException(status_code=400, detail="Unsupported server software")
    port = _validate_port(payload.get("port", DEFAULT_PORT))
    min_ram = _validate_ram(payload.get("min_ram", "1G"), "Minimum RAM")
    max_ram = _validate_ram(payload.get("max_ram", "2G"), "Maximum RAM")
    motd = str(payload.get("motd") or name).strip()[:300]
    return {"id": server_id, "name": name, "motd": motd, "version": version, "software": software, "port": port, "min_ram": min_ram, "max_ram": max_ram, "eula": payload.get("eula") is True}


def _http_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _minecraft_versions() -> dict[str, Any]:
    cached, fresh = _cache_get("minecraft-versions")
    if cached is not None and (fresh or MOCK_MODE):
        return {"versions": cached, "stale": not fresh}
    if MOCK_MODE:
        payload = [
            {"id": "1.21.4", "type": "release", "release_time": "2024-12-03T00:00:00Z"},
            {"id": "1.21.1", "type": "release", "release_time": "2024-08-08T00:00:00Z"},
            {"id": "1.20.1", "type": "release", "release_time": "2023-06-12T00:00:00Z"},
        ]
        _cache_put("minecraft-versions", payload)
        return {"versions": payload, "stale": False}
    try:
        manifest = _http_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        payload = [
            {"id": item["id"], "type": item["type"], "release_time": item.get("releaseTime", item.get("time"))}
            for item in manifest.get("versions", [])
            if item.get("type") in {"release", "snapshot"}
        ]
        _cache_put("minecraft-versions", payload)
        return {"versions": payload, "stale": False}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if cached is not None:
            return {"versions": cached, "stale": True}
        raise RuntimeError(f"Could not load the Minecraft version catalog: {exc}") from exc


def _paper_builds(version: str) -> list[dict[str, Any]]:
    cache_key = f"paper-builds:{version}"
    cached, fresh = _cache_get(cache_key)
    if cached is not None and (fresh or MOCK_MODE):
        return cached
    if MOCK_MODE:
        payload = [{"id": 1, "channel": "STABLE", "downloads": {"server:default": {"url": "mock://paper", "checksums": {"sha256": ""}}}}]
        _cache_put(cache_key, payload)
        return payload
    try:
        payload = _http_json(f"https://fill.papermc.io/v3/projects/paper/versions/{version}/builds")
        if not isinstance(payload, list):
            raise ValueError("Paper returned an invalid build list")
        _cache_put(cache_key, payload)
        return payload
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if cached is not None:
            return cached
        raise RuntimeError(f"Could not load Paper builds for {version}: {exc}") from exc


def _resolve_artifact(version: str, software: str, requested_build: str = "latest-stable") -> dict[str, Any]:
    if software == "vanilla":
        manifest = _http_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        match = next((item for item in manifest["versions"] if item["id"] == version), None)
        if not match:
            raise ValueError("Minecraft version was not found")
        server = _http_json(match["url"])["downloads"]["server"]
        return {"url": server["url"], "checksum": server.get("sha1"), "algorithm": "sha1", "resolved_build": None}
    if software == "paper":
        builds = _paper_builds(version)
        stable = [build for build in builds if build.get("channel") == "STABLE"]
        if requested_build not in {"", "latest-stable"}:
            stable = [build for build in stable if str(build.get("id")) == requested_build or str(build.get("number")) == requested_build]
        if not stable:
            raise ValueError(f"No stable Paper build is available for Minecraft {version}")
        build = stable[0]
        download = (build.get("downloads") or {}).get("server:default") or {}
        if not download.get("url"):
            raise ValueError("Paper build did not include a server download")
        checksums = download.get("checksums") or {}
        return {"url": download["url"], "checksum": checksums.get("sha256"), "algorithm": "sha256", "resolved_build": str(build.get("id", build.get("number")))}
    if software != "fabric":
        raise ValueError(f"{software.title()} uses an installer and is not enabled in this release yet")
    loaders = _http_json(f"https://meta.fabricmc.net/v2/versions/loader/{version}")
    installers = _http_json("https://meta.fabricmc.net/v2/versions/installer")
    if not loaders or not installers:
        raise ValueError(f"No Fabric loader is available for Minecraft {version}")
    loader_version = requested_build if requested_build not in {"", "latest-stable"} else loaders[0]["loader"]["version"]
    if not any(item.get("loader", {}).get("version") == loader_version for item in loaders):
        raise ValueError(f"Fabric loader {loader_version} is not compatible with Minecraft {version}")
    return {
        "url": f"https://meta.fabricmc.net/v2/versions/loader/{version}/{loader_version}/{installers[0]['version']}/server/jar",
        "checksum": None,
        "algorithm": None,
        "resolved_build": loader_version,
    }


def _download_artifact(artifact: dict[str, Any], destination: Path) -> None:
    if MOCK_MODE:
        destination.write_bytes(b"mock")
        os.chmod(destination, 0o640)
        return
    request = urllib.request.Request(artifact["url"], headers={"User-Agent": DOWNLOAD_USER_AGENT})
    digest = hashlib.new(artifact["algorithm"]) if artifact.get("checksum") and artifact.get("algorithm") else None
    with urllib.request.urlopen(request, timeout=180) as response, tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".download-", delete=False) as temp:
        temporary = Path(temp.name)
        try:
            while chunk := response.read(1024 * 1024):
                temp.write(chunk)
                if digest:
                    digest.update(chunk)
            if digest and digest.hexdigest().lower() != str(artifact["checksum"]).lower():
                raise ValueError("Downloaded artifact checksum did not match provider metadata")
            os.chmod(temporary, 0o640)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _create_files(config: dict[str, Any], instance: Path | None = None) -> None:
    instance = instance or _instance_dir(config["id"])
    instance.mkdir(parents=True, exist_ok=False)
    for child in ("mods", "plugins", "logs", "backups"):
        (instance / child).mkdir()
    (instance / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    properties = {key: str(meta["default"]) for key, meta in PROPERTY_FIELDS.items()}
    world = config.get("world") or {}
    properties.update({
        "motd": config["motd"],
        "server-port": str(config["port"]),
        "gamemode": str(world.get("gamemode", properties["gamemode"])),
        "difficulty": str(world.get("difficulty", properties["difficulty"])),
        "max-players": str(world.get("max_players", properties["max-players"])),
        "white-list": str(world.get("whitelist", False)).lower(),
        "online-mode": str(world.get("online_mode", True)).lower(),
        "pvp": str(world.get("pvp", True)).lower(),
    })
    seed = str(world.get("seed") or "").strip()
    if seed:
        properties["level-seed"] = seed
    (instance / "server.properties").write_text("\n".join(f"{key}={value}" for key, value in properties.items()) + "\n", encoding="utf-8")
    (instance / "server.env").write_text(f"MC_MIN_RAM={config['min_ram']}\nMC_MAX_RAM={config['max_ram']}\n", encoding="utf-8")
    if MOCK_MODE:
        (instance / "logs/latest.log").write_text("[Obsidian] Mock console ready.\n", encoding="utf-8")
    artifact = _resolve_artifact(config["version"], config["software"], config.get("loader_version", "latest-stable")) if not MOCK_MODE else {"url": "mock://server"}
    _download_artifact(artifact, instance / "server.jar")
    (instance / "install.json").write_text(json.dumps({"distribution": config["software"], "minecraft_version": config["version"], "resolved_build": artifact.get("resolved_build")}, indent=2) + "\n", encoding="utf-8")


def _ensure_server_jar(server_id: str) -> None:
    jar = _instance_dir(server_id) / "server.jar"
    if not jar.is_file():
        raise RuntimeError(f"Server {server_id} is incomplete: server.jar is missing. Delete it and create it again from the dashboard.")
    try:
        mode = stat.S_IMODE(jar.stat().st_mode)
        if not mode & stat.S_IRGRP:
            os.chmod(jar, mode | stat.S_IRGRP)
        if not os.access(jar, os.R_OK):
            raise PermissionError("the dashboard account cannot read the file")
    except OSError as exc:
        raise RuntimeError(f"Server {server_id} has an unreadable server.jar. Check the instance permissions: {exc}") from exc


async def _wait_stopped(server_id: str) -> bool:
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if not _status(_server_row(server_id))["running"]:
            return True
        await asyncio.sleep(1)
    return not _status(_server_row(server_id))["running"]


def _addon_root(server_id: str) -> tuple[Path, str]:
    row = _server_row(server_id)
    software = row["software"]
    if software == "vanilla":
        raise HTTPException(status_code=409, detail="Vanilla servers do not load plugin or mod JARs")
    folder = "plugins" if software == "paper" else "mods"
    return (_instance_dir(server_id) / folder).resolve(), "Plugin" if folder == "plugins" else "Mod"


def _mod_path(server_id: str, filename: str) -> Path:
    name = Path(filename).name
    if not name or name != filename or name.startswith(".") or len(name) > 180 or "\x00" in name or not name.lower().endswith(".jar"):
        raise HTTPException(status_code=400, detail="Only a plain .jar filename is allowed")
    root, _ = _addon_root(server_id)
    path = (root / name).resolve()
    if path.parent != root:
        raise HTTPException(status_code=400, detail="Invalid mod path")
    return path


def _read_properties(server_id: str) -> dict[str, str]:
    values = {key: str(meta["default"]) for key, meta in PROPERTY_FIELDS.items()}
    try:
        lines = (_instance_dir(server_id) / "server.properties").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            if key in values:
                values[key] = value
    return values


def _validate_properties(server_id: str, payload: dict[str, Any]) -> dict[str, str]:
    unknown = set(payload) - set(PROPERTY_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported properties: {', '.join(sorted(unknown))}")
    values = _read_properties(server_id)
    for key, raw in payload.items():
        value = str(raw).strip()
        meta = PROPERTY_FIELDS[key]
        if meta["kind"] == "select" and value not in meta["options"]:
            raise HTTPException(status_code=400, detail=f"Invalid value for {key}")
        if meta["kind"] == "boolean" and value.lower() not in {"true", "false"}:
            raise HTTPException(status_code=400, detail=f"Invalid boolean for {key}")
        if meta["kind"] == "number":
            try:
                number = int(value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{key} must be a number") from exc
            if not meta["min"] <= number <= meta["max"]:
                raise HTTPException(status_code=400, detail=f"{key} must be between {meta['min']} and {meta['max']}")
        if key == "motd" and len(value) > 300:
            raise HTTPException(status_code=400, detail="motd is too long")
        values[key] = value.lower() if meta["kind"] == "boolean" else value
    return values


def _save_properties(server_id: str, values: dict[str, str]) -> None:
    path = _instance_dir(server_id) / "server.properties"
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    seen: set[str] = set()
    lines: list[str] = []
    for line in original.splitlines():
        stripped = line.strip()
        key = stripped.split("=", 1)[0] if "=" in stripped and not stripped.startswith("#") else ""
        if key in values:
            lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            lines.append(line)
    for key in PROPERTY_FIELDS:
        if key not in seen:
            lines.append(f"{key}={values[key]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o640
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp.write("\n".join(lines).rstrip("\n") + "\n")
        temp_path = Path(temp.name)
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)


def _mods(server_id: str) -> list[dict[str, Any]]:
    root, _ = _addon_root(server_id)
    if not root.exists():
        return []
    result = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".jar":
            stat = path.stat()
            result.append({"filename": path.name, "bytes": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")})
    return result


def _backup_root(server_id: str) -> Path:
    instance = _instance_dir(server_id)
    root = (instance / "backups").resolve()
    if root.parent != instance.resolve():
        raise HTTPException(status_code=400, detail="Invalid backup directory")
    return root


def _backup_path(server_id: str, filename: str) -> Path:
    name = Path(filename).name
    if name != filename or not re.fullmatch(r"backup-[0-9]{8}-[0-9]{6}(?:-[a-f0-9]{6})?\.tar\.gz", name):
        raise HTTPException(status_code=400, detail="Invalid backup name")
    root = _backup_root(server_id)
    path = (root / name).resolve()
    if path.parent != root:
        raise HTTPException(status_code=400, detail="Invalid backup path")
    return path


def _backups(server_id: str) -> list[dict[str, Any]]:
    root = _backup_root(server_id)
    if not root.exists():
        return []
    result = []
    for path in sorted(root.glob("backup-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            result.append({"filename": path.name, "bytes": info.st_size, "created_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return result


def _create_backup(server_id: str) -> dict[str, Any]:
    instance = _instance_dir(server_id)
    root = _backup_root(server_id)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"backup-{stamp}-{uuid.uuid4().hex[:6]}.tar.gz"
    destination = root / filename
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".backup-", delete=False) as temp:
            temp_path = Path(temp.name)
        with tarfile.open(temp_path, "w:gz") as archive:
            for child in instance.iterdir():
                if child.name in {"backups", "logs", "console.in"} or child.is_fifo():
                    continue
                archive.add(child, arcname=child.name, recursive=True)
        os.chmod(temp_path, 0o640)
        os.replace(temp_path, destination)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
    info = destination.stat()
    return {"filename": filename, "bytes": info.st_size, "created_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds")}


def _log_read(path: Path, position: int) -> tuple[str, int]:
    try:
        stat = path.stat()
        if stat.st_size < position:
            position = 0
        with path.open("rb") as log:
            log.seek(position)
            data = log.read()
        return data.decode("utf-8", errors="replace"), stat.st_size
    except OSError:
        return "", position


def _slugify(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:32] or "minecraft-server"


def _port_available(port: int) -> bool:
    db = _db()
    try:
        reserved = db.execute("SELECT 1 FROM servers WHERE port = ?", (port,)).fetchone()
    finally:
        db.close()
    if reserved:
        return False
    if MOCK_MODE:
        return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True


def _allocate_port(requested: int | None) -> int:
    if requested is not None:
        port = _validate_port(requested)
        if not _port_available(port):
            raise HTTPException(status_code=409, detail="That port is already in use or reserved")
        return port
    for port in range(DEFAULT_PORT, PORT_MAX + 1):
        if _port_available(port):
            return port
    raise HTTPException(status_code=503, detail="No free Minecraft ports are available in the configured range")


def _create_config(request: CreateServerRequest) -> dict[str, Any]:
    server_id = _validate_id(request.id or _slugify(request.display_name))
    software = request.distribution.lower().strip()
    if software not in DISTRIBUTIONS:
        raise HTTPException(status_code=400, detail="Unknown server type")
    if not DISTRIBUTIONS[software]["available"]:
        raise HTTPException(status_code=409, detail=f"{DISTRIBUTIONS[software]['name']} is installer-based and is not available until its safe provider is installed")
    version = request.minecraft_version.strip()
    if not VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="Choose a Minecraft version from the catalog")
    if not request.eula_accepted:
        raise HTTPException(status_code=400, detail="You must accept the Minecraft EULA before creating a server")
    min_ram = _validate_ram(request.min_ram, "Minimum RAM")
    max_ram = _validate_ram(request.max_ram, "Maximum RAM")
    if _ram_to_megabytes(min_ram) > _ram_to_megabytes(max_ram):
        raise HTTPException(status_code=400, detail="Minimum RAM cannot be larger than maximum RAM")
    if request.world.gamemode not in PROPERTY_FIELDS["gamemode"]["options"]:
        raise HTTPException(status_code=400, detail="Invalid game mode")
    if request.world.difficulty not in PROPERTY_FIELDS["difficulty"]["options"]:
        raise HTTPException(status_code=400, detail="Invalid difficulty")
    return {
        "id": server_id,
        "name": request.display_name.strip(),
        "version": version,
        "software": software,
        "loader_version": request.distribution_version.strip() or "latest-stable",
        "port": _allocate_port(request.port),
        "min_ram": min_ram,
        "max_ram": max_ram,
        "motd": (request.motd or request.display_name).strip(),
        "world": request.world.model_dump(),
    }


def _ram_to_megabytes(value: str) -> int:
    return int(value[:-1]) * (1024 if value.endswith("G") else 1)


def _provision_job_sync(job_id: str) -> None:
    row = _job_row(job_id)
    config = json.loads(row["payload"])
    server_id = config["id"]
    staging_root = ROOT_DIR / ".staging" / job_id
    staging_instance = staging_root / "instance"
    final_instance = _instance_dir(server_id)
    moved = False
    db = _db()
    try:
        db.execute("UPDATE jobs SET state='running', started_at=? WHERE id=?", (_now(), job_id))
        db.commit()
    finally:
        db.close()
    _set_job(job_id, phase="resolving", progress=8, message="Resolving the selected server build")
    try:
        staging_root.mkdir(parents=True, exist_ok=False)
        _set_job(job_id, phase="preparing", progress=18, message="Preparing an isolated staging directory")
        _set_job(job_id, phase="downloading", progress=32, message="Downloading and verifying server files")
        _create_files(config, staging_instance)
        _set_job(job_id, phase="checking", progress=75, message="Checking server files and launch permissions")
        jar = staging_instance / "server.jar"
        if not jar.is_file() or jar.stat().st_size == 0:
            raise RuntimeError("The downloaded server JAR is missing or empty")
        _set_job(job_id, phase="registering", progress=88, message="Publishing the server instance")
        INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
        if final_instance.exists():
            raise RuntimeError("An instance directory already exists for this server id")
        os.replace(staging_instance, final_instance)
        moved = True
        if not MOCK_MODE:
            _control("enable", server_id)
        db = _db()
        try:
            db.execute(
                "UPDATE servers SET install_state='ready', loader_version=?, eula_accepted_at=?, updated_at=?, install_error=NULL WHERE id=?",
                (config.get("loader_version"), _now(), _now(), server_id),
            )
            db.commit()
        finally:
            db.close()
        _set_job(job_id, state="completed", phase="ready", progress=100, message="Server is ready to start", finished=True)
    except Exception as exc:
        safe_message = str(exc)[:600] or "The server could not be prepared"
        db = _db()
        try:
            db.execute("UPDATE servers SET install_state=?, install_error=?, updated_at=? WHERE id=?", ("repair_required" if moved else "failed", safe_message, _now(), server_id))
            db.commit()
        finally:
            db.close()
        _set_job(job_id, state="failed", phase="failed", progress=100, message="Provisioning failed", error_code="provision_failed", error_detail=safe_message, finished=True)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)


async def _queue_provision(request: CreateServerRequest) -> dict[str, Any]:
    config = await asyncio.to_thread(_create_config, request)
    db = _db()
    job_id = str(uuid.uuid4())
    try:
        if db.execute("SELECT 1 FROM servers WHERE id = ?", (config["id"],)).fetchone():
            raise HTTPException(status_code=409, detail="A server with that id already exists")
        db.execute(
            "INSERT INTO servers (id, name, version, software, port, min_ram, max_ram, created_at, install_state, loader_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (config["id"], config["name"], config["version"], config["software"], config["port"], config["min_ram"], config["max_ram"], _now(), config["loader_version"], _now()),
        )
        db.execute(
            "INSERT INTO jobs (id, kind, server_id, state, phase, progress, message, payload, created_at) VALUES (?, 'provision', ?, 'queued', 'queued', 0, 'Server creation is queued', ?, ?)",
            (job_id, config["id"], json.dumps(config), _now()),
        )
        db.commit()
    finally:
        db.close()
    asyncio.create_task(asyncio.to_thread(_provision_job_sync, job_id))
    return {"ok": True, "job_id": job_id, "server_id": config["id"], "job_url": f"/api/v1/jobs/{job_id}"}


@app.on_event("startup")
async def startup() -> None:
    _db().close()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v1/catalog/minecraft-versions")
async def catalog_minecraft_versions(channel: str = "release") -> dict[str, Any]:
    if channel not in {"release", "all"}:
        raise HTTPException(status_code=400, detail="channel must be release or all")
    result = await asyncio.to_thread(_minecraft_versions)
    versions = result["versions"]
    if channel == "release":
        versions = [version for version in versions if version["type"] == "release"]
    return {"versions": versions, "stale": result["stale"], "source": "Mojang version manifest"}


@app.get("/api/v1/catalog/distributions")
async def catalog_distributions(minecraft_version: str | None = None) -> dict[str, Any]:
    items = []
    for key, data in DISTRIBUTIONS.items():
        entry = {"key": key, **data, "compatible": data["available"], "reason": None}
        if not data["available"]:
            entry["reason"] = "Installer provider is not enabled yet"
        elif key == "paper" and minecraft_version and not MOCK_MODE:
            try:
                entry["compatible"] = bool(await asyncio.to_thread(_paper_builds, minecraft_version))
                if not entry["compatible"]:
                    entry["reason"] = f"No stable Paper build is available for {minecraft_version}"
            except RuntimeError:
                entry["compatible"] = False
                entry["reason"] = "Paper catalog is temporarily unavailable"
        items.append(entry)
    return {"distributions": items}


@app.get("/api/v1/catalog/distributions/{distribution}/builds")
async def catalog_distribution_builds(distribution: str, minecraft_version: str) -> dict[str, Any]:
    distribution = distribution.lower()
    if distribution not in DISTRIBUTIONS:
        raise HTTPException(status_code=404, detail="Unknown server type")
    if not DISTRIBUTIONS[distribution]["available"]:
        return {"builds": [], "reason": "Installer provider is not enabled yet"}
    if distribution == "paper":
        builds = await asyncio.to_thread(_paper_builds, minecraft_version)
        return {"builds": [{"id": str(item.get("id", item.get("number"))), "label": f"Build {item.get('id', item.get('number'))}", "channel": item.get("channel")} for item in builds if item.get("channel") == "STABLE"]}
    if distribution == "fabric":
        if MOCK_MODE:
            return {"builds": [{"id": "latest-stable", "label": "Latest stable loader", "channel": "stable"}]}
        loaders = await asyncio.to_thread(_http_json, f"https://meta.fabricmc.net/v2/versions/loader/{minecraft_version}")
        return {"builds": [{"id": item["loader"]["version"], "label": f"Fabric Loader {item['loader']['version']}", "channel": "stable" if item["loader"].get("stable") else "beta"} for item in loaders]}
    return {"builds": [{"id": "latest-stable", "label": "Official server", "channel": "stable"}]}


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    return {"job": await asyncio.to_thread(_job_payload, _job_row(job_id))}


@app.post("/api/v1/servers", status_code=202)
async def create_server_v1(request: CreateServerRequest) -> dict[str, Any]:
    return await _queue_provision(request)


@app.get("/api/servers")
async def list_servers() -> dict[str, Any]:
    db = _db()
    try:
        rows = db.execute("SELECT * FROM servers ORDER BY created_at").fetchall()
    finally:
        db.close()
    return {"servers": await asyncio.gather(*(asyncio.to_thread(_status, row) for row in rows))}


@app.post("/api/servers", status_code=202)
async def create_server(request: Request) -> dict[str, Any]:
    """Compatibility endpoint for the previous single-page UI."""
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Server payload must be an object")
    try:
        typed = CreateServerRequest(
            display_name=payload.get("name") or payload.get("id") or "",
            id=payload.get("id"),
            minecraft_version=payload.get("version") or "",
            distribution=payload.get("software") or "vanilla",
            distribution_version=payload.get("loader_version") or "latest-stable",
            port=payload.get("port"),
            min_ram=payload.get("min_ram") or "1G",
            max_ram=payload.get("max_ram") or "2G",
            motd=payload.get("motd"),
            eula_accepted=payload.get("eula") is True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _queue_provision(typed)


@app.get("/api/servers/{server_id}")
async def get_server(server_id: str) -> dict[str, Any]:
    return {"server": await asyncio.to_thread(_status, _server_row(server_id))}


@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: str, request: Request) -> dict[str, Any]:
    row = _server_row(server_id)
    payload = await request.json()
    if payload.get("confirm") != row["name"]:
        raise HTTPException(status_code=400, detail="Type the exact server name to confirm deletion")
    async with SERVER_LOCKS.setdefault(server_id, asyncio.Lock()):
        if _status(row)["running"]:
            raise HTTPException(status_code=409, detail="Stop the server before deleting it")
        await asyncio.to_thread(_control, "disable", server_id)
        instance = _instance_dir(server_id)
        if instance.exists():
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(instance), str(TRASH_DIR / f"{server_id}-{int(time.time())}"))
        db = _db()
        try:
            db.execute("DELETE FROM servers WHERE id = ?", (server_id,))
            db.commit()
        finally:
            db.close()
        MOCK_STATES.pop(server_id, None)
    return {"ok": True, "message": f"{row['name']} moved to recoverable trash"}


@app.post("/api/servers/{server_id}/{action}")
async def server_action(server_id: str, action: str) -> dict[str, Any]:
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail="Unknown server action")
    row = _server_row(server_id)
    async with SERVER_LOCKS.setdefault(server_id, asyncio.Lock()):
        try:
            current = await asyncio.to_thread(_status, row)
            if not current["allowed_actions"].get(action, False):
                raise HTTPException(status_code=409, detail=f"{action.title()} is not available while this server is {current['lifecycle']}")
            if action in {"start", "restart"}:
                await asyncio.to_thread(_ensure_server_jar, server_id)
            if action == "restart":
                await asyncio.to_thread(_control, "stop", server_id)
                if not await _wait_stopped(server_id):
                    raise HTTPException(status_code=504, detail="Server did not finish shutting down in time")
                await asyncio.to_thread(_control, "start", server_id)
            else:
                await asyncio.to_thread(_control, action, server_id)
        except HTTPException:
            raise
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "message": f"{action.title()} command sent", "server": await asyncio.to_thread(_status, _server_row(server_id))}


@app.get("/api/servers/{server_id}/mods")
async def list_mods(server_id: str) -> dict[str, Any]:
    root, label = await asyncio.to_thread(_addon_root, server_id)
    return {"mods": await asyncio.to_thread(_mods, server_id), "kind": label.lower(), "folder": root.name}


@app.post("/api/servers/{server_id}/mods")
async def upload_mod(server_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    row = _server_row(server_id)
    if (await asyncio.to_thread(_status, row))["running"]:
        raise HTTPException(status_code=409, detail="Stop the server before changing plugins or mods")
    _, label = await asyncio.to_thread(_addon_root, server_id)
    destination = _mod_path(server_id, file.filename or "")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise HTTPException(status_code=409, detail=f"A {label.lower()} with that filename already exists")
    total = 0
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".upload-", delete=False) as temp:
            temp_path = Path(temp.name)
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"{label} file is too large")
                temp.write(chunk)
            os.chmod(temp_path, 0o640)
        os.replace(temp_path, destination)
    finally:
        await file.close()
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return {"ok": True, "kind": label.lower(), "mod": {"filename": destination.name, "bytes": total}}


@app.delete("/api/servers/{server_id}/mods/{filename}")
async def delete_mod(server_id: str, filename: str) -> dict[str, Any]:
    row = _server_row(server_id)
    if (await asyncio.to_thread(_status, row))["running"]:
        raise HTTPException(status_code=409, detail="Stop the server before changing plugins or mods")
    path = _mod_path(server_id, filename)
    if not path.exists() or path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail="Plugin or mod not found")
    await asyncio.to_thread(path.unlink)
    return {"ok": True, "message": f"Deleted {path.name}"}


@app.get("/api/servers/{server_id}/backups")
async def list_backups(server_id: str) -> dict[str, Any]:
    _server_row(server_id)
    return {"backups": await asyncio.to_thread(_backups, server_id)}


@app.post("/api/servers/{server_id}/backups")
async def create_backup(server_id: str) -> dict[str, Any]:
    async with SERVER_LOCKS.setdefault(server_id, asyncio.Lock()):
        row = _server_row(server_id)
        if (await asyncio.to_thread(_status, row))["running"]:
            raise HTTPException(status_code=409, detail="Stop the server before creating a backup")
        backup = await asyncio.to_thread(_create_backup, server_id)
    return {"ok": True, "message": "Backup created", "backup": backup}


@app.get("/api/servers/{server_id}/backups/{filename}")
async def download_backup(server_id: str, filename: str) -> FileResponse:
    _server_row(server_id)
    path = _backup_path(server_id, filename)
    if not path.exists() or path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, filename=path.name, media_type="application/gzip")


@app.get("/api/servers/{server_id}/properties")
async def get_properties(server_id: str) -> dict[str, Any]:
    _server_row(server_id)
    return {"properties": await asyncio.to_thread(_read_properties, server_id), "fields": PROPERTY_FIELDS}


@app.put("/api/servers/{server_id}/properties")
async def update_properties(server_id: str, request: Request) -> dict[str, Any]:
    _server_row(server_id)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Properties payload must be an object")
    values = await asyncio.to_thread(_validate_properties, server_id, payload)
    await asyncio.to_thread(_save_properties, server_id, values)
    return {"ok": True, "message": "Properties saved; restart required", "properties": values, "restart_required": True}


def _send_command(server_id: str, command: str) -> None:
    if MOCK_MODE:
        return
    fifo = _instance_dir(server_id) / "console.in"
    if not fifo.exists():
        raise FileNotFoundError(f"Console FIFO not found: {fifo}")
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, (command + "\n").encode("utf-8"))
    finally:
        os.close(fd)


async def _socket_command(websocket: WebSocket, server_id: str, text: str) -> None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        await websocket.send_json({"type": "error", "message": "Invalid message"})
        return
    if payload.get("type") != "command":
        return
    command = str(payload.get("command", "")).strip()
    if not command or len(command) > MAX_COMMAND_LENGTH or any(char in command for char in "\x00\r\n"):
        await websocket.send_json({"type": "error", "message": "Command is empty or contains invalid characters"})
        return
    try:
        await asyncio.to_thread(_send_command, server_id, command)
        await websocket.send_json({"type": "command_result", "ok": True, "message": "Command sent"})
    except (OSError, RuntimeError) as exc:
        await websocket.send_json({"type": "command_result", "ok": False, "message": str(exc)})


@app.websocket("/ws/servers/{server_id}/console")
async def console(websocket: WebSocket, server_id: str) -> None:
    row = _server_row(server_id)
    await websocket.accept()
    log_file = _instance_dir(server_id) / "logs/latest.log"
    try:
        initial = log_file.read_bytes()[-16000:] if log_file.exists() else b""
        position = log_file.stat().st_size if log_file.exists() else 0
        await websocket.send_json({"type": "history", "data": initial.decode("utf-8", errors="replace")})
        await websocket.send_json({"type": "status", "data": await status_for_row(row)})
        last_status = time.monotonic()
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=.75)
                await _socket_command(websocket, server_id, message)
            except asyncio.TimeoutError:
                pass
            output, position = await asyncio.to_thread(_log_read, log_file, position)
            if output:
                await websocket.send_json({"type": "console", "data": output})
            if time.monotonic() - last_status >= 3:
                await websocket.send_json({"type": "status", "data": await status_for_row(_server_row(server_id))})
                last_status = time.monotonic()
    except (WebSocketDisconnect, RuntimeError, HTTPException):
        return


async def status_for_row(row: sqlite3.Row) -> dict[str, Any]:
    return await asyncio.to_thread(_status, row)
