#!/usr/bin/env bash
# One-time setup for a new box. Idempotent — safe to re-run after a config change.
#
#   ./deploy/provision.sh
#
# Creates the data dir, builds the venv, seeds .env, installs+enables the systemd unit, and tells
# you the two things it deliberately does NOT do for you: install the deploy cron line, and move an
# existing database. Ongoing updates are deploy/update.sh.
#
# Replaces the old .deb (packaging/), which pushed from a laptop over SSH and targeted a
# decommissioned host. Deploys are pull-based now — see deploy/update.sh.
set -euo pipefail

main() {
  local repo_dir run_user data_dir service unit_dir unit
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  run_user="$(id -un)"
  data_dir="${MUG_DATA_DIR:-/var/lib/mug-bot}"
  service="${MUG_SERVICE:-mug-bot}"
  # Overridable: some distros use /usr/lib/systemd/system, and it lets this script be exercised
  # against a scratch directory instead of the real one.
  unit_dir="${MUG_SYSTEMD_DIR:-/etc/systemd/system}"
  unit="${unit_dir}/${service}.service"

  echo "==> provisioning $service"
  echo "    repo:  $repo_dir"
  echo "    user:  $run_user"
  echo "    data:  $data_dir"

  if [ "$run_user" = "root" ]; then
    echo "ERROR: run this as the user the bot should run as, not root (it uses sudo where needed)." >&2
    exit 1
  fi

  # ---- data dir (outside the checkout, so a re-clone or git clean can never touch the DB) ----
  if [ ! -d "$data_dir" ]; then
    echo "==> creating $data_dir (needs sudo)"
    sudo mkdir -p "$data_dir"
  fi
  sudo chown "$run_user":"$run_user" "$data_dir"

  # ---- venv ----
  if [ ! -d "$repo_dir/.venv" ]; then
    echo "==> creating venv"
    python3 -m venv "$repo_dir/.venv"
  fi
  echo "==> installing dependencies"
  "$repo_dir/.venv/bin/pip" install --quiet --upgrade pip
  "$repo_dir/.venv/bin/pip" install --quiet -r "$repo_dir/requirements.txt"

  # ---- .env (seeded once; never clobbered) ----
  if [ ! -f "$repo_dir/.env" ]; then
    cp "$repo_dir/.env.example" "$repo_dir/.env"
    echo "==> created .env from .env.example — EDIT IT before starting:"
    echo "    Discord token + channel ids, TORN_API_KEYS, and"
    echo "    MUG_DB_PATH=$data_dir/mug_bot.db"
  elif ! grep -qs '^MUG_DB_PATH=.\+' "$repo_dir/.env"; then
    echo "[warn] MUG_DB_PATH is unset or empty in .env."
    echo "       Without it the bot may fall back to putting the database INSIDE this git checkout."
    echo "       Set: MUG_DB_PATH=$data_dir/mug_bot.db"
  fi

  # ---- systemd unit, GENERATED from the real paths ----
  # Generated rather than copied: a unit file with hardcoded paths is exactly how the committed one
  # drifted from reality (it still said monchoon@/home/monchoon/torn/mug-bot long after the move).
  echo "==> writing $unit (needs sudo)"
  sudo mkdir -p "$unit_dir"
  sed -e "s|@RUN_USER@|$run_user|g" \
      -e "s|@WORKING_DIR@|$repo_dir|g" \
      "$repo_dir/systemd/mug-bot.service.template" | sudo tee "$unit" >/dev/null

  sudo systemctl daemon-reload
  sudo systemctl enable "$service" >/dev/null
  echo "==> $service enabled"

  check_sudo_nopasswd "$run_user" "$service"
  warn_db_in_checkout "$repo_dir" "$data_dir"
  print_cron "$repo_dir"

  echo
  echo "==> next: fill in .env, then:  sudo systemctl start $service"
  echo "    watch it:                  journalctl -u $service -f"
}

# The deploy cron runs unattended, so its `systemctl restart` must not prompt for a password.
check_sudo_nopasswd() {
  local run_user="$1" service="$2"
  if sudo -n systemctl status "$service" >/dev/null 2>&1 || sudo -n true 2>/dev/null; then
    echo "==> passwordless sudo: ok"
    return 0
  fi
  echo
  echo "[action needed] The deploy cron needs passwordless sudo to restart the service."
  echo "  sudo visudo -f /etc/sudoers.d/mug-bot     and add exactly:"
  echo
  echo "  $run_user ALL=(root) NOPASSWD: /bin/systemctl restart $service, /bin/systemctl status $service"
  echo
  echo "  (check the path first: command -v systemctl — some distros use /usr/bin/systemctl)"
}

# The DB living inside the git working tree is a data-loss risk, not a style problem: git clean, a
# re-clone or a bad reset destroys it. Detect and instruct; never move live data automatically.
warn_db_in_checkout() {
  local repo_dir="$1" data_dir="$2"
  local found=0 f
  for f in "$repo_dir"/*.db; do
    [ -e "$f" ] || continue
    found=1
    echo
    echo "[action needed] A database is inside the git checkout: $f"
    echo "  This is destroyed by 'git clean -xfd' or a re-clone. Move it, with the service stopped:"
    echo
    echo "    sudo systemctl stop ${MUG_SERVICE:-mug-bot}"
    echo "    sqlite3 '$f' \".backup '$data_dir/mug_bot.db'\""
    echo "    sqlite3 '$data_dir/mug_bot.db' 'PRAGMA integrity_check;'   # expect: ok"
    echo "    # compare row counts against the old file before removing anything, then set"
    echo "    # MUG_DB_PATH=$data_dir/mug_bot.db in .env and start the service again."
  done
  [ "$found" = 0 ] || true
}

print_cron() {
  local repo_dir="$1"
  echo
  echo "==> auto-deploy is NOT installed automatically. To enable it, 'crontab -e' and add:"
  echo
  echo "  */5 * * * * $repo_dir/deploy/update.sh --if-changed >> /var/log/mug-bot-deploy.log 2>&1"
  echo
  echo "  (run ./deploy/update.sh by hand a few times first and watch it before automating)"
}

main "$@"
