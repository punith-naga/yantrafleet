#!/usr/bin/env bash
# Yantrika one-shot installer for Linux/macOS.
#
#   bash install.sh          # create .venv, install everything editable
#   bash install.sh --run    # ... then start the loopback demo immediately
#
# Idempotent: safe to re-run; an existing venv is reused and installs are
# refreshed in place. Override the venv location with INSTALL_VENV_DIR
# (default: .venv next to this script).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${INSTALL_VENV_DIR:-$REPO_ROOT/.venv}"
RUN_AFTER=0
[ "${1:-}" = "--run" ] && RUN_AFTER=1

say()  { printf '\n== %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1) Find a Python >= 3.10
# ---------------------------------------------------------------------------
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
           >/dev/null 2>&1; then
        PY="$(command -v "$cand")"
        break
    fi
done
[ -n "$PY" ] || fail "no Python >= 3.10 found on PATH.
Yantrika needs Python 3.10 or newer. Install it via your package manager
(e.g. 'sudo apt install python3', 'brew install python@3.12') or from
https://www.python.org/downloads/ and re-run this script."
say "using $("$PY" --version 2>&1) at $PY"

# ---------------------------------------------------------------------------
# 2) Create (or reuse) the virtual environment
# ---------------------------------------------------------------------------
VPY="$VENV_DIR/bin/python"
if [ -x "$VPY" ]; then
    say "reusing existing venv at $VENV_DIR"
else
    say "creating venv at $VENV_DIR"
    "$PY" -m venv "$VENV_DIR" || fail "could not create a venv at $VENV_DIR.
On Debian/Ubuntu you may need: sudo apt install python3-venv"
fi
[ -x "$VPY" ] || fail "venv python missing at $VPY"

# ---------------------------------------------------------------------------
# 3) Install the packages (editable) + copilot requirements
# ---------------------------------------------------------------------------
say "upgrading pip"
"$VPY" -m pip install --quiet --upgrade pip

say "installing Yantrika packages (editable)"
"$VPY" -m pip install --quiet \
    -e "$REPO_ROOT/core" \
    -e "$REPO_ROOT/sim" \
    -e "$REPO_ROOT/connector" \
    -e "$REPO_ROOT/detector" \
    -e "$REPO_ROOT/notifier" \
    -e "$REPO_ROOT/ops"

say "installing copilot (sarathi) requirements"
"$VPY" -m pip install --quiet -r "$REPO_ROOT/copilot/requirements.txt"

# ---------------------------------------------------------------------------
# 4) Sanity check
# ---------------------------------------------------------------------------
say "verifying: python -m yantraops --help"
"$VPY" -m yantraops --help >/dev/null || fail "yantraops did not import cleanly"

# Optional deeper check; older/newer versions may not ship this subcommand.
"$VPY" -m yantraops doctor >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 5) Done
# ---------------------------------------------------------------------------
cat <<EOF

============================================================
 Yantrika installed.

 Start the full loopback demo (no cloud, no keys) with:

   $VENV_DIR/bin/python -m yantraops up --loopback

 It prints the console URL and opens your browser. Ctrl-C stops
 everything. Re-run this script any time to refresh the install.
============================================================
EOF

if [ "$RUN_AFTER" -eq 1 ]; then
    say "starting the loopback demo (--run)"
    exec "$VPY" -m yantraops up --loopback
fi
