#!/usr/bin/env bash
# Build the mug-bot .deb: run the unit tests, then (only on success) package the app.
#
#   packaging/build-deb.sh [version]      # default version 0.1.0
#
# Output: build/mug-bot_<version>.deb
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # mug-bot/
cd "$HERE"

VERSION="${1:-0.1.0}"
PKG="mug-bot"
INSTALL_DIR="/home/monchoon/torn/mug-bot"
BUILD="$HERE/build"
STAGE="$BUILD/stage"

command -v dpkg-deb >/dev/null || { echo "dpkg-deb not found (install dpkg)"; exit 1; }
command -v rsync    >/dev/null || { echo "rsync not found (apt install rsync)"; exit 1; }

echo "==> [1/4] Unit tests (build gates on these passing)"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --quiet -r requirements-dev.txt
python -m pytest -q

echo "==> [2/4] Stage app files -> $INSTALL_DIR"
rm -rf "$STAGE"
mkdir -p "$STAGE${INSTALL_DIR}" "$STAGE/DEBIAN" "$STAGE/etc/systemd/system"
rsync -a \
  --exclude '.venv' --exclude 'build' --exclude '.git' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.env' --exclude '*.db' --exclude '*.db-*' \
  --exclude 'bot/state.json' --exclude 'bot/state.json.*' \
  --exclude 'scratch/dumps' \
  ./ "$STAGE${INSTALL_DIR}/"

echo "==> [3/4] Control + maintainer scripts + systemd unit"
sed "s/__VERSION__/$VERSION/" packaging/control > "$STAGE/DEBIAN/control"
install -m 0755 packaging/postinst "$STAGE/DEBIAN/postinst"
install -m 0755 packaging/prerm    "$STAGE/DEBIAN/prerm"
install -m 0755 packaging/postrm   "$STAGE/DEBIAN/postrm"
install -m 0644 systemd/mug-bot.service "$STAGE/etc/systemd/system/mug-bot.service"

echo "==> [4/4] Build .deb"
OUT="$BUILD/${PKG}_${VERSION}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo
echo "Built: $OUT"
dpkg-deb --info "$OUT" | sed 's/^/  /'
