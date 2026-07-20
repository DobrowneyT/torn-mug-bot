#!/usr/bin/env python3
"""
All-in-one release from your laptop: run the unit tests, build the .deb, scp it to the box, and
`apt install` it over SSH — which restarts mug-bot.service via the package's postinst.

    python packaging/release.py                  # test -> build -> scp -> apt install (default)
    python packaging/release.py --version 0.2.0
    python packaging/release.py --env            # also push your local mug-bot/.env
    python packaging/release.py --no-install     # just build + scp (install it yourself)
    python packaging/release.py --server monchoon@1.2.3.4

`apt install --reinstall` forces a reinstall even when the version is unchanged (plain apt skips
a same-version reinstall). The deb is staged in /tmp so apt's sandbox can read it; install uses
`ssh -t` so the sudo prompt works. The whole run aborts if the unit tests fail — nothing is copied.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # mug-bot/
DEFAULT_SERVER = "monchoon@5.161.91.183"
INSTALL_DIR = "/home/monchoon/torn/mug-bot"
RUN_USER = "monchoon"


def run(cmd):
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument("--server", default=os.environ.get("MUG_SSH", DEFAULT_SERVER))
    ap.add_argument("--env", action="store_true", help="also push your local mug-bot/.env")
    ap.add_argument("--no-install", action="store_true", help="build + scp only; don't install over SSH")
    args = ap.parse_args()

    deb = os.path.join(ROOT, "build", f"mug-bot_{args.version}.deb")
    deb_name = os.path.basename(deb)
    remote_deb = f"/tmp/{deb_name}"

    # 1) Build (runs pytest first). Aborts here if tests fail.
    run(["bash", os.path.join(HERE, "build-deb.sh"), args.version])
    if not os.path.exists(deb):
        sys.exit(f"expected {deb} but it was not produced")

    # 2) Copy the .deb (to /tmp so apt's sandbox can read it) and optionally the .env.
    run(["scp", deb, f"{args.server}:{remote_deb}"])
    if args.env:
        env_path = os.path.join(ROOT, ".env")
        if not os.path.exists(env_path):
            sys.exit(f"--env given but {env_path} not found (cp .env.example .env and fill it)")
        run(["scp", env_path, f"{args.server}:~/mug-bot.env"])

    install_cmd = f"sudo apt install -y --reinstall --allow-downgrades {remote_deb}"
    if args.no_install:
        print(f"\nCopied to {args.server}:{remote_deb}. Install it there with:")
        print(f"  {install_cmd}")
        return

    # 3) Install over SSH. --reinstall forces a same-version reinstall; apt resolves deps; the
    #    postinst rebuilds the venv (if needed) + restarts. -t gives a TTY for the sudo prompt.
    remote = install_cmd
    if args.env:
        remote += (
            f" && sudo cp ~/mug-bot.env {INSTALL_DIR}/.env"
            f" && sudo chown {RUN_USER}:{RUN_USER} {INSTALL_DIR}/.env"
            f" && sudo systemctl restart mug-bot"
        )
    print(f"\n==> Installing on {args.server} (enter your sudo password if prompted)")
    run(["ssh", "-t", args.server, remote])
    print(f"\n✅ Deployed. Tail logs with:  ssh {args.server} 'journalctl -u mug-bot -f'")


if __name__ == "__main__":
    main()
