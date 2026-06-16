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

# ── Python detection (venv auto-activate first so we get the right python) ────────

# Activate .venv if it exists in the repo root and no venv is currently active
if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -f "$BRIDGE_DIR/../.venv/bin/python3" ]]; then
    echo "Found .venv in repo — activating it."
    source "$BRIDGE_DIR/../.venv/bin/activate"
fi

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

# Warn if marginalia is not importable from the selected Python
if ! "$PYTHON" -c "import bridge.cli" 2>/dev/null && \
   ! "$PYTHON" -c "import marginalia" 2>/dev/null; then
    echo ""
    echo "⚠ Warning: marginalia not importable from $PYTHON."
    echo "  If you installed into a venv, activate it first: source .venv/bin/activate"
    echo "  Continuing — update the Python path in the plist after install."
    echo ""
fi

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

# ── API key (optional at install time — can also be added to plist later) ────

API_KEY_VAR=""
API_KEY_VAL=""

# Use env vars if already set
if [[ -n "${MARGINALIA_OPENAI_API_KEY:-}" ]]; then
    API_KEY_VAR="MARGINALIA_OPENAI_API_KEY"
    API_KEY_VAL="$MARGINALIA_OPENAI_API_KEY"
elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
    API_KEY_VAR="MARGINALIA_OPENAI_API_KEY"
    API_KEY_VAL="$OPENAI_API_KEY"
elif [[ -n "${MARGINALIA_ANTHROPIC_API_KEY:-}" ]]; then
    API_KEY_VAR="MARGINALIA_ANTHROPIC_API_KEY"
    API_KEY_VAL="$MARGINALIA_ANTHROPIC_API_KEY"
elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    API_KEY_VAR="MARGINALIA_ANTHROPIC_API_KEY"
    API_KEY_VAL="$ANTHROPIC_API_KEY"
fi

if [[ -z "$API_KEY_VAR" ]]; then
    echo ""
    echo "No API key found in environment."
    echo "Providers: openai (sk-...) or anthropic (sk-ant-...). Leave empty to add later."
    read -r -p "  API key (or Enter to skip): " API_KEY_VAL
    if [[ "$API_KEY_VAL" == sk-ant-* ]]; then
        API_KEY_VAR="MARGINALIA_ANTHROPIC_API_KEY"
    elif [[ -n "$API_KEY_VAL" ]]; then
        API_KEY_VAR="MARGINALIA_OPENAI_API_KEY"
    fi
fi

[[ -n "$API_KEY_VAR" ]] && echo "✓ API key: ${API_KEY_VAR} (${API_KEY_VAL:0:8}...)" || echo "⚠ No API key set — Book Index generation will fail until you add one to $PLIST_DST" 

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

# Uncomment and populate the API key in the installed plist (if collected)
if [[ -n "${API_KEY_VAR:-}" && -n "${API_KEY_VAL:-}" ]]; then
    python3 - "$PLIST_DST" "$API_KEY_VAR" "$API_KEY_VAL" << 'PYEOF'
import re, sys
path, var, val = sys.argv[1], sys.argv[2], sys.argv[3]
plist = open(path).read()
live = f'        <key>{var}</key>\n        <string>{val}</string>'
# Uncomment existing commented-out entry
plist = re.sub(rf'<!--\s*<key>{re.escape(var)}</key><string>[^<]*</string>\s*-->', live, plist)
# If not found as a comment, insert before MARGINALIA_PORT
if f'<key>{var}</key>' not in plist:
    plist = plist.replace('<key>MARGINALIA_PORT</key>', f'{live}\n        <key>MARGINALIA_PORT</key>', 1)
open(path, 'w').write(plist)
PYEOF
fi

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
if [[ -z "${API_KEY_VAR:-}" ]]; then
    echo "⚠  ACTION REQUIRED: no API key was configured."
    echo "   Edit $PLIST_DST"
    echo "   Uncomment MARGINALIA_OPENAI_API_KEY or MARGINALIA_ANTHROPIC_API_KEY"
    echo "   Then reload:"
    echo "     launchctl bootout ${GUI_DOMAIN}/${LABEL}"
    echo "     launchctl bootstrap ${GUI_DOMAIN} $PLIST_DST"
    echo ""
fi
echo "Bridge management:"
echo "  launchctl kill TERM gui/$(id -u)/${LABEL}   # stop"
echo "  launchctl bootout ${GUI_DOMAIN}/${LABEL}     # remove"
echo "  tail -f $LOG_PATH"
echo "────────────────────────────────────────────────────────" 
