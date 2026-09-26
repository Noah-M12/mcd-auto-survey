#!/usr/bin/env bash
# Set up mcdvoice.py on Linux Mint.
#
#   chmod +x setup-mint.sh && ./setup-mint.sh
#
# Mint is Ubuntu underneath, but two things trip up the generic instructions:
#
#   1. Playwright's "install-deps" reads /etc/os-release, finds ID=linuxmint,
#      and bails out with "unsupported distribution" - even though the Ubuntu
#      packages underneath are exactly right. This script installs them itself.
#   2. Package names changed in Ubuntu 24.04 (Mint 22): libasound2 became
#      libasound2t64, and so on. This script asks apt which names exist rather
#      than assuming, so it works on Mint 21 and 22 alike.
#
# Safe to re-run.

set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$*"; }

# --- identify the system -----------------------------------------------------
DISTRO=""; RELEASE=""; UBASE=""
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO="${ID:-}"; RELEASE="${VERSION_ID:-}"
  UBASE="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
fi

say "system"
echo "    ${PRETTY_NAME:-unknown}"
[ -n "$UBASE" ] && echo "    ubuntu base: $UBASE"
if [ "$DISTRO" != "linuxmint" ] && [ "$DISTRO" != "ubuntu" ] && [ "$DISTRO" != "debian" ]; then
  warn "this script targets Mint; on $DISTRO try ./setup-linux.sh instead"
fi

# --- python ------------------------------------------------------------------
say "python"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is missing. Install it with:"
  echo "    sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    sys.exit("need python 3.9+, found %d.%d" % sys.version_info[:2])
print("    python %d.%d" % sys.version_info[:2])
PY

# Mint ships python3 without the venv module; this is the most common failure.
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  say "installing python3-venv (needed to create the environment)"
  sudo apt update
  sudo apt install -y python3-venv python3-pip
fi

# --- virtualenv --------------------------------------------------------------
say "virtualenv"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ok "created .venv"
else
  ok ".venv already exists"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
ok "pip ready"

say "installing playwright"
python -m pip install playwright

say "downloading chromium"
python -m playwright install chromium

# --- chromium's system libraries --------------------------------------------
# Ask apt which of these exist rather than hardcoding one Ubuntu generation's
# names. Mint 21 (jammy) wants libasound2; Mint 22 (noble) wants libasound2t64.
say "installing chromium's system libraries"
CANDIDATES="libnss3 libnspr4 \
libatk1.0-0t64 libatk1.0-0 \
libatk-bridge2.0-0t64 libatk-bridge2.0-0 \
libatspi2.0-0t64 libatspi2.0-0 \
libcups2t64 libcups2 \
libasound2t64 libasound2 \
libdrm2 libgbm1 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
libxrandr2 libpango-1.0-0 libcairo2 libxss1"

WANT=""
for p in $CANDIDATES; do
  if apt-cache show "$p" >/dev/null 2>&1; then WANT="$WANT $p"; fi
done

# Prefer the t64 name when both are offered - installing both is harmless but
# noisy, and on noble the plain name is only a transitional shim.
FINAL=""
for p in $WANT; do
  case "$p" in
    *t64) FINAL="$FINAL $p" ;;
    *)    echo "$WANT" | grep -qw "${p}t64" || FINAL="$FINAL $p" ;;
  esac
done

echo "    packages:$FINAL"
if command -v sudo >/dev/null 2>&1; then
  sudo apt update
  # shellcheck disable=SC2086
  sudo apt install -y $FINAL
  ok "system libraries installed"
else
  warn "no sudo - skipping. If chromium won't start, that's why."
fi

# --- verify ------------------------------------------------------------------
say "verifying"
python - <<'PY'
import os
from playwright.sync_api import sync_playwright
headed = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
args = [] if headed else ["--no-sandbox", "--disable-dev-shm-usage"]
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=not headed, args=args)
    p = b.new_page()
    p.set_content("<h1 id=x>ok</h1>")
    assert p.inner_text("#x") == "ok"
    b.close()
print("    chromium launches %s and renders"
      % ("with a visible window" if headed else "headless"))
PY
python mcdvoice.py --help >/dev/null && ok "mcdvoice.py runs"

say "done"
cat <<'EOF'

  Each new terminal needs the environment activated:

      source .venv/bin/activate

  Then:

      python3 mcdvoice.py add my_codes.txt
      python3 mcdvoice.py list
      python3 mcdvoice.py run --only 12321 --delay 5 --express "highly satisfied"

  Mint has a desktop, so the browser opens in a visible window by default and
  you can watch it or take over. Force it either way with --headed / --headless.

  Your codes and settings live in ~/mcdvoice/ (codes.json, profile.json,
  comments.txt). Copy that folder from the Mac to carry everything across -
  and move it rather than duplicating it, or the two machines will disagree
  about which codes are already used.

EOF
