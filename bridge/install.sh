#!/usr/bin/env bash
# install.sh — Install marginalia bridge as a macOS LaunchAgent
#
# Usage:
#   ./install.sh                         # interactive — prompts for vault path
#   MARGINALIA_VAULT=~/Documents/MyVault ./install.sh  # non-interactive
#
# The script substitutes your real paths into the plist template and installs
# it as a LaunchAgent that starts at login and restarts on crash.
set -euo pipefail

BRIDGE_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PLIST_TEMPLATE="$BRIDGE_DIR/com.marginalia.bridge.plist"
LABEL="com.marginalia.bridge"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="$HOME/Library/Logs/marginalia.log"

# ── Venv detection ───────────────────────────────────────────────────────────

# If no venv is active, check if one exists in the repo root
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    REPO_VENV="$BRIDGE_DIR/../.venv"
    if [[ -f "$REPO_VENV/bin/python3" ]]; then
        echo "Found .venv in repo — activating it."
        source "$REPO_VENV/bin/activate"
    else
        # Check if marginalia is importable from the detected Python
        if ! "$PYTHON" -c "import bridge.cli" 2>/dev/null && \
           ! "$PYTHON" -m bridge.cli --help &>/dev/null 2>&1; then
            echo ""
            echo "⚠ Warning: 'marginalia' package not found in $PYTHON."
            echo "  If you installed into a venv, activate it first:"
            echo "    source .venv/bin/activate"
            echo "  Then re-run this script."
            echo "  Continuing anyway — edit the plist to use the correct Python path."
            echo ""
        fi
    fi
fi

# ── Python detection ─────────────────────────────────────────────────────────────

detect_python() {
    for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if command -v "$candidate" &>/dev/null; then
            local ver
            ver=$("$candidate" -c "import sys; print(sys.version_info[:2] >= (3,11))" 2>/dev/null)
            if [[ "$ver" == "True" ]]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    echo ""
}

PYTHON=$(detect_python)
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.11+ not found. Install it first:"
    echo "  macOS: brew install python"
    echo "  https://python.org/downloads"
    exit 1
fi
echo "✓ Python: $PYTHON ($("$PYTHON" --version))"

# ── Vault path ────────────────────────────────────────────────────────────────

VAULT_PATH="${MARGINALIA_VAULT:-}"
if [[ -z "$VAULT_PATH" ]]; then
    echo ""
    echo "Where is your Obsidian vault? (the folder containing .obsidian/)"
    read -r -p "  Vault path [~/Documents]: " VAULT_PATH
    VAULT_PATH="${VAULT_PATH:-$HOME/Documents}"
fi
# Expand ~ to $HOME
VAULT_PATH="${VAULT_PATH/#\~/$HOME}"
echo "✓ Vault: $VAULT_PATH"

# ── Build PATH for the LaunchAgent (inherits brew, pyenv, etc.) ───────────────

PATH_DIRS="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
# Add venv bin if marginalia was installed into one
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    PATH_DIRS="$VIRTUAL_ENV/bin:$PATH_DIRS"
fi

# ── Substitute template → installed plist ────────────────────────────────────

mkdir -p "$(dirname "$PLIST_DST")"
mkdir -p "$(dirname "$LOG_PATH")"

sed \
  -e "s|{{PYTHON}}|$PYTHON|g" \
  -e "s|{{BRIDGE_SERVER}}|$BRIDGE_DIR/server.py|g" \
  -e "s|{{VAULT_PATH}}|$VAULT_PATH|g" \
  -e "s|{{LOG_PATH}}|$LOG_PATH|g" \
  -e "s|{{PATH_DIRS}}|$PATH_DIRS|g" \
  "$PLIST_TEMPLATE" > "$PLIST_DST"

echo "✓ Installed plist → $PLIST_DST"

# ── Load (or reload) the LaunchAgent ─────────────────────────────────────────

GUI_DOMAIN="gui/$(id -u)"

# Unload existing instance if running
if launchctl print "${GUI_DOMAIN}/${LABEL}" &>/dev/null 2>&1; then
    launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
    sleep 2
fi

# Bootstrap with retry (race condition on first install)
if ! launchctl bootstrap "${GUI_DOMAIN}" "$PLIST_DST" 2>/dev/null; then
    sleep 4
    launchctl bootstrap "${GUI_DOMAIN}" "$PLIST_DST"
fi
echo "✓ LaunchAgent loaded — starts at login, restarts on crash"

# ── Health check ──────────────────────────────────────────────────────────────

echo ""
echo "Waiting for bridge to start..."
sleep 3
if curl -sf http://localhost:7731/ping >/dev/null; then
    echo "✓ Bridge is alive — http://localhost:7731/ping → pong"
else
    echo "⚠ Bridge didn't respond yet — it may still be starting."
    echo "  Check logs: tail -f $LOG_PATH"
fi

# ── Next steps ────────────────────────────────────────────────────────────────

echo ""
echo "────────────────────────────────────────────────────────"
echo "Next: set your AI provider key in $PLIST_DST"
echo "  Edit the EnvironmentVariables section and add:"
echo "    MARGINALIA_OPENAI_API_KEY   for OpenAI"
echo "    MARGINALIA_ANTHROPIC_API_KEY for Anthropic"
echo "  Then reload: launchctl bootout ${GUI_DOMAIN}/${LABEL}"
echo "              launchctl bootstrap ${GUI_DOMAIN} $PLIST_DST"
echo ""
echo "Bridge management:"
echo "  launchctl stop  ${LABEL}          # stop"
echo "  launchctl start ${LABEL}          # start"
echo "  launchctl bootout ${GUI_DOMAIN}/${LABEL}  # remove"
echo "  tail -f $LOG_PATH"
echo "────────────────────────────────────────────────────────"
