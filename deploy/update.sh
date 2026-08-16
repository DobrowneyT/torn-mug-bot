#!/usr/bin/env bash
# Pull the latest code onto this box and restart the bot.
#
#   ./deploy/update.sh                # pull + deps + backup + restart + verify
#   ./deploy/update.sh --if-changed   # cron: quiet unless origin has moved since the last
#                                     #   SUCCESSFUL deploy (not since the last pull — see marker)
#   SKIP_BACKUP=1 ./deploy/update.sh  # skip the pre-restart safety dump
#
# To force a redeploy of the current checkout — the recovery path after an interrupted run — drop
# the flag. Detached, so a dropped connection cannot kill it half-way:
#   setsid nohup ./deploy/update.sh >> /var/log/mug-bot-deploy.log 2>&1 &
#
# Deploys whatever branch this checkout is ON. Pull-based on purpose: the box asks GitHub for
# changes rather than CI pushing to the box, so no credentials to this machine live anywhere off it.
# Anything on main has already passed CI (branch protection), so this script does not re-run tests.
#
# First-time setup for a box is deploy/provision.sh, not this script.
#
# Operator state is never touched: .env and the SQLite DB are gitignored / live outside the
# checkout, so `git merge` cannot clobber them.
set -euo pipefail

# Everything lives in main() so the whole script is parsed into memory before it runs — this script
# updates its own file mid-execution, and bash reads scripts lazily, so a top-level body could be
# corrupted by the very `git merge` it just performed.
main() {
  local repo_dir env_file branch local_sha remote_sha marker deployed_sha
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$repo_dir"
  env_file="$repo_dir/.env"
  marker="$repo_dir/.last-deployed"

  # One deploy at a time — a 5-minute cron must never overlap a slow run (pip, a cold restart).
  exec 9>"/tmp/mug-bot-update.lock"
  flock -n 9 || { echo "another update is already running — skipping"; exit 0; }

  local service port db_path ah_db_path
  service="${MUG_SERVICE:-mug-bot}"
  port="$(env_get "$env_file" MUG_SERVER_PORT)"; port="${port:-3001}"
  db_path="$(env_get "$env_file" MUG_DB_PATH)"; db_path="${db_path:-/var/lib/mug-bot/mug_bot.db}"
  ah_db_path="$(env_get "$env_file" MUG_AH_DB_PATH)"; ah_db_path="${ah_db_path:-/var/lib/mug-bot/mug_ah.db}"

  branch="$(git rev-parse --abbrev-ref HEAD)"
  git fetch --quiet origin "$branch"
  local_sha="$(git rev-parse HEAD)"
  remote_sha="$(git rev-parse "origin/$branch")"

  # Nothing to do: exit before any other check, so a cron run stays silent and a dirty tree on an
  # up-to-date box isn't reported as a failure every five minutes.
  #
  # ⚠️ Compared against what was last successfully DEPLOYED, not against HEAD. The merge below
  # happens before the deps, the backup and the restart, so an interrupted run (a closed SSH
  # session is the usual way) leaves HEAD already equal to origin with none of the work done — and
  # a HEAD-based guard would then answer "nothing to do" forever, silently, with the box left on
  # the old code. This cost the dashboard box five hours on 2026-08-15. An absent marker reads as
  # "never deployed", so the first run after this change deploys once and re-arms the mechanism.
  deployed_sha="$(cat "$marker" 2>/dev/null || true)"
  if [ "${1:-}" = "--if-changed" ] && [ "$deployed_sha" = "$remote_sha" ]; then
    exit 0
  fi

  # Locally MODIFIED TRACKED files mean someone edited the deployed code in place; merging over that
  # would silently discard their work, so stop. Checked before anything is reported or changed, so
  # the refusal is the first thing you see.
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: tracked files have local changes — refusing to deploy. Commit, stash or revert:" >&2
    git status --short --untracked-files=no >&2
    exit 1
  fi

  # UNTRACKED files are only noted. They can't be lost by a fast-forward, and if an incoming commit
  # would overwrite one, git itself refuses the merge with a precise message. Blocking on them was
  # too strict and produced repeated false stops on the real box — a database backup, SQLite WAL
  # sidecars, a stray shell redirect — none of which endanger anything.
  local untracked
  untracked="$(git ls-files --others --exclude-standard)"
  if [ -n "$untracked" ]; then
    echo "[note] untracked files present (not a problem for the deploy):"
    printf '%s\n' "$untracked" | sed 's/^/  /'
  fi

  if [ "$local_sha" = "$remote_sha" ]; then
    echo "already up to date at $(git rev-parse --short HEAD) — restarting anyway"
  fi

  echo "==> deploying $branch: $(git rev-parse --short HEAD) -> $(git rev-parse --short "origin/$branch")"
  # --ff-only: a server checkout must never invent merge commits. If this fails, someone committed
  # on the server — resolve it by hand rather than forcing.
  git merge --ff-only "origin/$branch" --quiet

  # Reinstall deps only when they actually changed — pip is by far the slowest step here.
  if [ "$local_sha" != "$remote_sha" ] \
     && git diff --name-only "$local_sha" "$remote_sha" | grep -qx 'requirements.txt'; then
    echo "==> requirements.txt changed — updating the venv"
    # `python -m pip` rather than `.venv/bin/pip`: console-script shebangs hardcode an absolute
    # interpreter path, so they break if the checkout is ever renamed or moved, while the
    # interpreter symlink keeps working. Running the module sidesteps that entirely.
    "$repo_dir/.venv/bin/python" -m pip install --quiet -r "$repo_dir/requirements.txt"
  fi

  if [ "${SKIP_BACKUP:-0}" != "1" ]; then
    backup_db "$db_path" mug_bot
    # The AH ledger only exists once the scraper has run, and backup_db skips a missing file
    # quietly-ish. It is worth snapshotting because it may NOT be re-derivable: how far back Torn
    # serves auction history is capped by an amount we do not yet know, so a backfill that reached
    # further than the current window can't simply be run again.
    backup_db "$ah_db_path" mug_ah
  fi

  echo "==> restarting $service"
  sudo systemctl restart "$service"

  # Only reached when wait_ready succeeded — it returns non-zero otherwise and `set -e` stops the
  # script here, deliberately leaving the marker stale so the next cron tick retries. A broken
  # deploy that repeats loudly beats one that marks itself done and goes quiet.
  wait_ready "$port" "$service"
  git rev-parse HEAD > "$marker"
  echo "==> deployed $(git rev-parse --short HEAD) ($(date -u +%FT%TZ))"
}

# Read one KEY=value out of .env, stripping any inline comment and surrounding whitespace/quotes.
# Absent file, absent key, or an empty value all yield "" so callers can apply their own default.
env_get() {
  local file="$1" key="$2" line
  [ -f "$file" ] || return 0
  line="$(grep -s -m1 "^${key}=" "$file" || true)"
  [ -n "$line" ] || return 0
  line="${line#*=}"
  line="$(printf '%s' "$line" | sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# Consistent snapshot of the live DB next to it, keeping the most recent few. A failed backup warns
# but does not block the deploy — a missing sqlite3 does too, but loudly, because that silently
# disables the safety net entirely.
backup_db() {
  local db="$1" prefix="${2:-mug_bot}" dir stamp name
  if [ ! -f "$db" ]; then
    # The AH ledger legitimately does not exist until the scraper has run for the first time, so
    # only the main database being absent is worth alarming about.
    if [ "$prefix" = "mug_bot" ]; then
      echo "[warn] no database at $db — skipping backup (is MUG_DB_PATH set? see deploy/provision.sh)"
    else
      echo "[note] no database at $db yet — nothing to back up"
    fi
    return 0
  fi
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "[warn] sqlite3 not installed — SKIPPING the pre-deploy backup. Install it: sudo apt install sqlite3"
    return 0
  fi
  dir="$(dirname "$db")/backups"
  mkdir -p "$dir"
  # Timestamp + the SHA being replaced: two deploys in the same second would otherwise write the
  # same filename, and the SHA tells you at a glance which version a backup belongs to.
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  name="${prefix}-${stamp}-$(git rev-parse --short HEAD).db"
  echo "==> backing up $db"
  # .backup (not cp) so the WAL is folded in and the copy is never torn.
  if ! sqlite3 "$db" ".backup '$dir/$name'"; then
    echo "[warn] backup failed — continuing with the deploy"
    return 0
  fi
  # Keep the newest 10, by mtime. find+sort rather than parsing `ls` output (shellcheck SC2012).
  find "$dir" -maxdepth 1 -name "${prefix}-*.db" -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n +11 | cut -d' ' -f2- \
    | while read -r old; do rm -f "$old"; done
}

# Poll /health until the freshly-restarted service answers. Polling rather than a single shot: a
# cold start takes a few seconds, and a single check would log a spurious failure on every deploy.
wait_ready() {
  local port="$1" service="$2" i
  echo "==> waiting for /health on 127.0.0.1:$port"
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      echo "  ok   healthy after ${i}x2s"
      return 0
    fi
    sleep 2
  done
  echo "  FAIL service did not become healthy within 60s" >&2
  echo "       check: journalctl -u ${service} -n 50 --no-pager" >&2
  return 1
}

main "$@"
