#!/usr/bin/env bash
# install.sh — Install marginalia as a macOS LaunchAgent
set -euo pipefail

PLIST_SRC="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )/com.sam.marginalia.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.sam.marginalia.plist"
LABEL="com.sam.marginalia"

# Verify Python 3 is available
if ! /opt/homebrew/bin/python3 --version &>/dev/null; then
    echo "ERROR: python3 not found at /opt/homebrew/bin/python3"
    echo "Install Homebrew Python: brew install python"
    exit 1
fi

# boto3 is only needed for AWS Bedrock providers.
# If you are using OpenAI or Anthropic direct APIs only, you can skip this.
if ! /opt/homebrew/bin/python3 -c "import boto3" 2>/dev/null; then
    echo "NOTE: boto3 not found — required only for AWS Bedrock providers."
    echo "      Install with: pip3 install boto3"
    echo "      Continuing install (you can add boto3 later)."
fi

# Install plist
cp "$PLIST_SRC" "$PLIST_DST"
echo "✓ Installed plist → $PLIST_DST"

# Load (or reload) the agent
if launchctl list | grep -q "$LABEL"; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    sleep 2
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
echo "✓ LaunchAgent loaded"

# Quick health check
sleep 2
if curl -sf http://localhost:7731/ping >/dev/null; then
    echo "✓ Bridge is alive — http://localhost:7731/ping → pong"
    echo "  Monitor: http://localhost:7731/monitor"
else
    echo "⚠ Bridge didn't respond yet — check logs:"
    echo "  tail -f ~/Library/Logs/marginalia.log"
fi

echo ""
echo "To manage the bridge:"
echo "  launchctl stop  $LABEL   # stop"
echo "  launchctl start $LABEL   # start"
echo "  launchctl bootout gui/\$(id -u)/$LABEL  # remove from login items"
echo "  tail -f ~/Library/Logs/marginalia.log"
