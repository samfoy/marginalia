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
elif [[ -n "${MARGINALIA_AWS_PROFILE:-}" ]]; then
    API_KEY_VAR="MARGINALIA_AWS_PROFILE"
    API_KEY_VAL="$MARGINALIA_AWS_PROFILE"
elif [[ -n "${AWS_PROFILE:-}" ]]; then
    API_KEY_VAR="MARGINALIA_AWS_PROFILE"
    API_KEY_VAL="$AWS_PROFILE"
fi

if [[ -z "$API_KEY_VAR" ]]; then
    echo ""
    echo "No credentials found in environment."
    echo "  openai    — enter key starting with sk-..."
    echo "  anthropic — enter key starting with sk-ant-..."
    echo "  aws       — enter your AWS profile name (e.g. default)"
    echo "  Leave empty to add manually to $PLIST_DST"
    read -r -p "  Key or profile (Enter to skip): " API_KEY_VAL
    if [[ "$API_KEY_VAL" == sk-ant-* ]]; then
        API_KEY_VAR="MARGINALIA_ANTHROPIC_API_KEY"
    elif [[ "$API_KEY_VAL" == sk-* ]]; then
        API_KEY_VAR="MARGINALIA_OPENAI_API_KEY"
    elif [[ -n "$API_KEY_VAL" ]]; then
        API_KEY_VAR="MARGINALIA_AWS_PROFILE"
    fi
fi

if [[ -n "$API_KEY_VAR" ]]; then
    [[ "$API_KEY_VAR" == "MARGINALIA_AWS_PROFILE" ]] \
        && echo "✓ AWS profile: $API_KEY_VAL" \
        || echo "✓ API key: ${API_KEY_VAR} (${API_KEY_VAL:0:8}...)"
else
    echo "⚠ No credentials set — Book Index generation will fail until you configure $PLIST_DST"
fi

# Set a sensible default model ID to match the provider
if [[ "$API_KEY_VAR" == "MARGINALIA_ANTHROPIC_API_KEY" ]]; then
    MODEL_DEFAULT="${MARGINALIA_MODEL_ID:-anthropic:claude-opus-4-5}"
elif [[ "$API_KEY_VAR" == "MARGINALIA_OPENAI_API_KEY" ]]; then
    MODEL_DEFAULT="${MARGINALIA_MODEL_ID:-openai:gpt-4o}"
elif [[ -n "${MARGINALIA_AWS_PROFILE:-}" ]]; then
    MODEL_DEFAULT="${MARGINALIA_MODEL_ID:-us.anthropic.claude-sonnet-4-6}"
else
    MODEL_DEFAULT="${MARGINALIA_MODEL_ID:-openai:gpt-4o}"
fi
echo "✓ Model: $MODEL_DEFAULT (set MARGINALIA_MODEL_ID to override)" 

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
if [[ -n "${API_KEY_VAR:-}" || -n "${MODEL_DEFAULT:-}" ]]; then
    python3 - "$PLIST_DST" "${API_KEY_VAR:-}" "${API_KEY_VAL:-}" "${MODEL_DEFAULT:-}" << 'PYEOF'
import re, sys
path, var, val, model = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ''
plist = open(path).read()
# Inject API key if provided
if var and val:
    live = f'        <key>{var}</key>\n        <string>{val}</string>'
    plist = re.sub(rf'<!--\s*<key>{re.escape(var)}</key><string>[^<]*</string>\s*-->', live, plist)
    if f'<key>{var}</key>' not in plist:
        plist = plist.replace('<key>MARGINALIA_PORT</key>', f'{live}\n        <key>MARGINALIA_PORT</key>', 1)
# Update model ID to match provider
if model:
    plist = re.sub(r'(<key>MARGINALIA_MODEL_ID</key>\s*<string>)[^<]*(</string>)', rf'\g<1>{model}\2', plist)
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
echo "  launchctl kill TERM gui/$(id -u)/${LABEL}   # temporary stop (KeepAlive restarts in ~10s)"
echo "  launchctl bootout ${GUI_DOMAIN}/${LABEL}     # remove"
echo "  tail -f $LOG_PATH"
echo "────────────────────────────────────────────────────────" 
