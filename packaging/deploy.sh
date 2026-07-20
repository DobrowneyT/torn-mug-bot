#!/usr/bin/env bash
# Copy the built .deb to the box. Install is the one-liner printed at the end (matches your
# "I just dpkg -i on the server" workflow).
#
#   packaging/deploy.sh [version]                      # default 0.1.0
#   MUG_SSH=monchoon@5.161.91.183 packaging/deploy.sh  # override target
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-0.1.0}"
SERVER="${MUG_SSH:-monchoon@5.161.91.183}"
DEB="$HERE/build/mug-bot_${VERSION}.deb"

[ -f "$DEB" ] || { echo "Not built yet — run: packaging/build-deb.sh $VERSION"; exit 1; }

echo "==> Copying $(basename "$DEB") to $SERVER:~/"
scp "$DEB" "$SERVER:~/"
echo
echo "Then install it on the server:"
echo "  ssh $SERVER 'sudo dpkg -i ~/mug-bot_${VERSION}.deb'"
echo "  ssh $SERVER 'journalctl -u mug-bot -f'"
