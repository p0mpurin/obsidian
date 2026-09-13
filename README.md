# Obsidian Minecraft control room

Obsidian is a single-page FastAPI dashboard for creating and managing multiple isolated Minecraft server instances on one Debian host. The interface is deliberately monochrome: black OLED canvas, grayscale surfaces, soft white status glow.

## Install and update from GitHub

Clone the repository once on the Debian server, then run the installer:

```bash
git clone https://github.com/p0mpurin/obsidian.git /home/purin/obsidian
cd /home/purin/obsidian
sudo ./deploy/install-or-update.sh
```

After later changes are pushed, update the server with one command:

```bash
cd /home/purin/obsidian
./deploy/update-server.sh
```

The update script pulls only fast-forward Git changes, updates the application, Python dependencies, helper scripts, and systemd units, and restarts the dashboard. It preserves `/opt/minecraftdash/.env`, `/opt/minecraftdash/data`, `/home/purin/minecraft/instances`, `.staging`, `.trash`, and the existing top-level Minecraft server.

## Safe local test

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
MC_MOCK=1 MC_ROOT_DIR=/tmp/obsidian-minecraft MC_DB_FILE=/tmp/obsidian-minecraft/servers.db uvicorn app:app --reload --port 8080
```

Open `http://127.0.0.1:8080`. Mock mode lets you create server records, start/stop them, edit properties, upload mods, and test the console WebSocket without touching systemd or a real Minecraft installation. Server creation uses a mock JAR in this mode.

The rebuilt create flow now uses `/api/v1`: it loads release versions from Mojang's manifest, shows server-type/build choices, requires an explicit EULA acceptance, reserves a port, and returns a durable provisioning job instead of holding the browser request open. Run the dependency-free mock smoke check with:

```bash
MC_MOCK=1 python tests/smoke.py
```

The first live provider slice supports Vanilla, Paper (Fill v3 stable builds), and Fabric. Forge and NeoForge are visible in the wizard but deliberately disabled until their installer-specific provider and launch runner are deployed; they cannot safely be treated as a `server.jar` download.

## Debian deployment

The intended runtime host is the Debian machine. Copy the project to `/opt/minecraftdash`, create a virtual environment, install `requirements.txt`, and copy `.env.example` to `/opt/minecraftdash/.env`. Use the real paths below unless the host has an established layout:

```env
MC_ROOT_DIR=/srv/minecraft
MC_DB_FILE=/opt/minecraftdash/data/servers.db
MC_HOST=127.0.0.1
MC_DASHBOARD_HOST=127.0.0.1
MC_DASHBOARD_PORT=18080
MC_PORT_MIN=25565
MC_PORT_MAX=25665
```

Create dedicated `minecraft` and `minecraft-dashboard` users. The dashboard user needs controlled access to the instance directories and the template service. Install the root-owned helper:

```bash
sudo install -o root -g root -m 0755 deploy/minecraft-dashboard-ctl /usr/local/sbin/minecraft-dashboard-ctl
sudo install -o root -g root -m 0755 deploy/minecraft-server-wrapper /usr/local/libexec/minecraft-server-wrapper
sudo install -m 0644 deploy/minecraft@.service /etc/systemd/system/minecraft@.service
sudo install -m 0440 deploy/sudoers-minecraft-dashboard /etc/sudoers.d/minecraft-dashboard
sudo systemctl daemon-reload
sudo systemctl enable --now minecraft-dashboard.service
```

For an existing single-server directory at `/home/purin/minecraft`, keep its current `world`, `plugins`, and top-level files in place and set `MC_ROOT_DIR=/home/purin/minecraft`. Create separate managed directories and grant only the two service accounts access to them:

```bash
sudo setfacl -m u:minecraft-dashboard:--x /home/purin
sudo install -d -o minecraft-dashboard -g minecraft -m 2770 /home/purin/minecraft/instances /home/purin/minecraft/.trash
sudo setfacl -m d:u:minecraft-dashboard:rwx,d:u:minecraft:rwx /home/purin/minecraft/instances
sudo setfacl -m d:u:minecraft-dashboard:rwx,d:u:minecraft:rwx /home/purin/minecraft/.trash
```

If `setfacl` is not installed, install the Debian `acl` package first. Set `MC_DASHBOARD_HOST=0.0.0.0` when the dashboard should be reachable directly from another device on the LAN.

The dashboard accepts the Minecraft EULA in its create form and writes `eula=true` into the new instance automatically; no manual server-file edit is required. The service listens on `127.0.0.1` by default. To visit it directly from another device on the same network, set `MC_DASHBOARD_HOST=0.0.0.0` in `.env`, then reload the service and open `http://<server-ip>:18080`. Because this control panel can start services and execute console commands, use a VPN or authenticated reverse proxy before exposing it beyond a trusted LAN.

The helper accepts only a validated server id and a small fixed set of systemd actions. The dashboard never gets unrestricted shell or systemctl access. The template unit’s Java memory values are defaults; update the unit or extend the generated per-instance configuration if you need each server’s RAM values to be honored.

The dashboard service must be allowed to use its restricted `sudo` helper, so do not enable `NoNewPrivileges=true` on `minecraft-dashboard.service`; the helper and `/etc/sudoers.d/minecraft-dashboard` provide the privilege boundary instead.

For real instances, the create flow downloads Vanilla, Paper, or Fabric server JARs from their official metadata endpoints, writes an EULA and server.properties, and leaves the new service stopped. New instances are created under `${MC_ROOT_DIR}/instances/<id>`; an existing single-server directory can therefore remain in place beside the managed instances. Commands are delivered through `${MC_ROOT_DIR}/instances/<id>/console.in`; the service wrapper connects that FIFO to the Java process’s stdin. If the server setup uses RCON or tmux instead, replace `_send_command` in `app.py` with that transport.

Deletion requires the server to be stopped and the exact display name to be typed. The directory is moved to `/srv/minecraft/.trash/<id>-<timestamp>` instead of being permanently removed, so an administrator can recover it.

Bind the dashboard to localhost and expose it through a VPN or authenticated reverse proxy. This application can control services, write JARs, and execute Minecraft console commands; do not expose it unauthenticated to the LAN or internet.
