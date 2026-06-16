#!/usr/bin/env bash
# marginalia installer
#
# Usage (one-liner):
#   curl -sSL https://raw.githubusercontent.com/samfoy/marginalia/main/install.sh | bash
#
# Provider override (default: openai):
#   curl -sSL ... | MARGINALIA_PROVIDER=anthropic bash
#   curl -sSL ... | MARGINALIA_PROVIDER=bedrock bash
#
# Install location override (default: ~/.marginalia):
#   curl -sSL ... | MARGINALIA_DIR=~/tools/marginalia bash
#
set -euo pipefail

INSTALL_DIR="${MARGINALIA_DIR:-$HOME/.marginalia}"
PROVIDER="${MARGINALIA_PROVIDER:-openai}"

# ── Helpers ───────────────────────────────────────────────────────────────────

bold()  { printf '\033[1m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
cyan()  { printf '\033[36m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
dim()   { printf '\033[2m%s\033[0m'  "$*"; }

step()  { echo; echo "  $(cyan '▸') $(bold "$*")"; }
ok()    { echo "  $(green '✓') $*"; }
err()   { echo "  $(red '✗') $*" >&2; exit 1; }

# ── Checks ────────────────────────────────────────────────────────────────────

step "Checking requirements"

if ! command -v git &>/dev/null; then
    err "git not found. Install it from https://git-scm.com and re-run."
fi

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c 'import sys; print(sys.version_info >= (3,11))' 2>/dev/null)
        if [ "$ver" = "True" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "Python 3.11+ not found. Install it from https://python.org and re-run."
fi

ok "Python: $($PYTHON --version)"
ok "git: $(git --version | cut -d' ' -f3)"

# ── Clone or update ───────────────────────────────────────────────────────────

step "Installing to $INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
    ok "Existing install found — updating"
    git -C "$INSTALL_DIR" pull --ff-only --quiet
else
    git clone --quiet https://github.com/samfoy/marginalia "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
fi

# ── Virtual environment ───────────────────────────────────────────────────────

step "Setting up Python environment"

VENV="$INSTALL_DIR/.venv"
if [ ! -d "$VENV" ]; then
    "$PYTHON" -m venv "$VENV"
    ok "Created venv at $VENV"
else
    ok "Venv exists"
fi

PIP="$VENV/bin/pip"
MARGINALIA_BIN="$VENV/bin/marginalia"

# ── Install ───────────────────────────────────────────────────────────────────

step "Installing marginalia [$PROVIDER,embed]"

"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -e "$INSTALL_DIR/[$PROVIDER,embed]"
ok "Installed"

# ── PATH hint ─────────────────────────────────────────────────────────────────

LINK_DST="/usr/local/bin/marginalia"
if [ -w "/usr/local/bin" ] || sudo -n true 2>/dev/null; then
    ln -sf "$MARGINALIA_BIN" "$LINK_DST" 2>/dev/null \
        || sudo ln -sf "$MARGINALIA_BIN" "$LINK_DST" 2>/dev/null \
        || true
fi

if ! command -v marginalia &>/dev/null; then
    echo
    echo "  $(dim 'Add marginalia to your PATH, or use the full path:')"
    echo "  $(dim "  export PATH=\"$VENV/bin:\$PATH\"")"
    echo "  $(dim "  # Add this to your ~/.zshrc or ~/.bashrc to make it permanent")"
fi

# ── Setup wizard ──────────────────────────────────────────────────────────────

echo
echo "  $(bold "$(green '✓') Installation complete!") $(dim "($INSTALL_DIR)")"
echo
echo "  Running setup wizard…"
echo

"$MARGINALIA_BIN" setup
