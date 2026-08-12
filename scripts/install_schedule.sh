#!/bin/bash
# Installs a macOS launchd job that runs the daily refresh at 7:00 AM local time.
# This does NOT run automatically — you run it yourself when ready:
#   bash scripts/install_schedule.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python3"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.micronmonitor.refresh.plist"

mkdir -p "$PLIST_DIR"

sed -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    "$PROJECT_ROOT/scripts/com.micronmonitor.refresh.plist.template" > "$PLIST_PATH"

echo "Wrote $PLIST_PATH"
echo ""
echo "To activate (runs daily at 7:00 AM):"
echo "  launchctl load $PLIST_PATH"
echo ""
echo "To run it once immediately as a test:"
echo "  launchctl start com.micronmonitor.refresh"
echo ""
echo "To deactivate later:"
echo "  launchctl unload $PLIST_PATH"
