#!/usr/bin/env bash
# install_deps.sh — Install Python dependencies for dnstt-balancer
# Tries pip install from the internet first; falls back to local vendor/ wheels.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REQ="$SCRIPT_DIR/requirements.txt"
VENDOR="$SCRIPT_DIR/vendor"

# ── Locate Python ────────────────────────────────────────────────────────────
find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            echo "$cmd"
            return
        fi
    done
    echo ""
}

PYTHON="$(find_python)"
if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3 is not installed or not in PATH."
    echo "        Please install Python 3.8+ and try again."
    exit 1
fi

PY_VER="$($PYTHON --version 2>&1)"
echo "[*] Found: $PY_VER ($PYTHON)"

# ── Check if requirements.txt has any real packages ──────────────────────────
has_deps() {
    # Return 0 (true) if requirements.txt has at least one non-comment,
    # non-empty line.
    grep -qvE '^\s*($|#)' "$REQ" 2>/dev/null
}

if [ ! -f "$REQ" ]; then
    echo "[*] No requirements.txt found — nothing to install."
    echo "[OK] dnstt-balancer uses only the Python standard library."
    exit 0
fi

if ! has_deps; then
    echo "[*] requirements.txt has no dependencies listed."
    echo "[OK] dnstt-balancer uses only the Python standard library."
    exit 0
fi

# ── Try online install ───────────────────────────────────────────────────────
echo "[*] Attempting online install via pip..."
if $PYTHON -m pip install -r "$REQ" 2>/dev/null; then
    echo "[OK] Dependencies installed from the internet."
    exit 0
fi

echo "[!] Online install failed (no internet?). Trying local vendor/ wheels..."

# ── Fallback: offline install from vendor/ ───────────────────────────────────
if [ ! -d "$VENDOR" ] || [ -z "$(ls -A "$VENDOR"/*.whl 2>/dev/null)" ]; then
    echo "[ERROR] No .whl files found in $VENDOR/"
    echo "        To prepare offline packages, run on a machine with internet:"
    echo ""
    echo "          pip download -r requirements.txt -d vendor/ --only-binary=:all:"
    echo ""
    exit 1
fi

echo "[*] Installing from $VENDOR/ ..."
$PYTHON -m pip install --no-index --find-links "$VENDOR" -r "$REQ"

echo "[OK] Dependencies installed from local vendor/ wheels."
