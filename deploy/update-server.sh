#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

git pull --ff-only
sudo "$repo_dir/deploy/install-or-update.sh"

