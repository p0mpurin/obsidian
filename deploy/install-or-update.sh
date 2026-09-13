#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo: sudo ./deploy/install-or-update.sh" >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_dir=$(dirname -- "$script_dir")
install_dir=/opt/minecraftdash
environment_file="$install_dir/.env"
minecraft_root=/home/purin/minecraft

if ! getent group minecraft >/dev/null 2>&1; then
    groupadd --system minecraft
fi
if ! id minecraft-dashboard >/dev/null 2>&1; then
    useradd --system --home-dir "$install_dir" --shell /usr/sbin/nologin --gid minecraft minecraft-dashboard
fi
if ! id minecraft >/dev/null 2>&1; then
    useradd --system --home-dir "$minecraft_root" --shell /usr/sbin/nologin --gid minecraft minecraft
fi

install -d -o minecraft-dashboard -g minecraft -m 2770 "$install_dir" "$install_dir/data"
install -d -o minecraft-dashboard -g minecraft -m 0755 "$install_dir/static" "$install_dir/tests"
install -o minecraft-dashboard -g minecraft -m 0644 "$source_dir/app.py" "$install_dir/app.py"
install -o minecraft-dashboard -g minecraft -m 0644 "$source_dir/requirements.txt" "$install_dir/requirements.txt"
install -o minecraft-dashboard -g minecraft -m 0644 "$source_dir/README.md" "$install_dir/README.md"
install -o minecraft-dashboard -g minecraft -m 0644 "$source_dir/static/index.html" "$install_dir/static/index.html"
install -o minecraft-dashboard -g minecraft -m 0644 "$source_dir/tests/smoke.py" "$install_dir/tests/smoke.py"

if [ ! -f "$environment_file" ]; then
    install -o root -g minecraft -m 0640 "$source_dir/.env.example" "$environment_file"
    echo "Created $environment_file from .env.example"
fi

if [ ! -x "$install_dir/.venv/bin/python" ]; then
    python3 -m venv "$install_dir/.venv"
fi
"$install_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r "$install_dir/requirements.txt"

install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/sbin
install -o root -g root -m 0755 "$source_dir/deploy/minecraft-dashboard-ctl" /usr/local/sbin/minecraft-dashboard-ctl
install -o root -g root -m 0755 "$source_dir/deploy/minecraft-server-wrapper" /usr/local/libexec/minecraft-server-wrapper
install -o root -g root -m 0644 "$source_dir/deploy/minecraft@.service" /etc/systemd/system/minecraft@.service
install -o root -g root -m 0644 "$source_dir/deploy/minecraft-dashboard.service" /etc/systemd/system/minecraft-dashboard.service
install -o root -g root -m 0440 "$source_dir/deploy/sudoers-minecraft-dashboard" /etc/sudoers.d/minecraft-dashboard
visudo -cf /etc/sudoers.d/minecraft-dashboard >/dev/null

install -d -o minecraft-dashboard -g minecraft -m 2770 \
    "$minecraft_root/instances" "$minecraft_root/.staging" "$minecraft_root/.trash"
if command -v setfacl >/dev/null 2>&1; then
    setfacl -m u:minecraft-dashboard:--x /home/purin
    setfacl -m u:minecraft-dashboard:rwx,u:minecraft:rwx \
        "$minecraft_root/instances" "$minecraft_root/.staging" "$minecraft_root/.trash"
    setfacl -m d:u:minecraft-dashboard:rwx,d:u:minecraft:rwx \
        "$minecraft_root/instances" "$minecraft_root/.staging" "$minecraft_root/.trash"
fi

systemctl daemon-reload
systemctl enable minecraft-dashboard.service >/dev/null
systemctl restart minecraft-dashboard.service

echo "Obsidian updated successfully."
echo "Open http://192.168.1.70:18080"
echo "Existing worlds, instances, database, and .env were preserved."
