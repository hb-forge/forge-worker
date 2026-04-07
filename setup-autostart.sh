#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Forge Worker — Auto-start installer
#
# Sets up the menu bar app to launch automatically on login via launchd.
# Run once per machine.
#
# Usage:
#   ./setup-autostart.sh             # install
#   ./setup-autostart.sh --uninstall # remove
#   ./setup-autostart.sh --restart   # reload (after updates)
# ─────────────────────────────────────────────────────────────────────────────
set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_LABEL="com.hawthornbloom.forge-worker"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_PATH="$HOME/.forge-worker/app.log"
PYTHON="$APP_DIR/.venv/bin/python3"

mkdir -p "$HOME/.forge-worker"
mkdir -p "$HOME/Library/LaunchAgents"

# ── Uninstall ──
if [[ "${1:-}" == "--uninstall" ]]; then
  echo "Stopping Forge Worker…"
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "✓ Removed. Log remains at $LOG_PATH"
  exit 0
fi

# ── Restart ──
if [[ "${1:-}" == "--restart" ]]; then
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load -w "$PLIST_PATH"
  echo "✓ Forge Worker restarted"
  exit 0
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Forge Worker — Auto-start Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Check Python venv ──
if [[ ! -f "$PYTHON" ]]; then
  echo "  Creating Python venv…"
  python3 -m venv "$APP_DIR/.venv"
  PYTHON="$APP_DIR/.venv/bin/python3"
fi
echo "  Installing dependencies…"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "  ✓ Dependencies ready"

# ── Write plist ──
# LimitLoadToSessionType = Aqua ensures it runs in the GUI login session
# (required for menu bar apps — won't launch from SSH/daemon sessions)
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${APP_DIR}/app.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${APP_DIR}</string>

  <!-- Aqua = GUI login session only (required for menu bar apps) -->
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>

  <key>KeepAlive</key>
  <true/>

  <key>RunAtLoad</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>${LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_PATH}</string>
</dict>
</plist>
PLIST

# ── Load service ──
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

echo "  ✓ Forge Worker installed and started"
echo ""
echo "  The ⚒ icon will appear in your menu bar."
echo ""
echo "  Logs:      tail -f $LOG_PATH"
echo "  Status:    launchctl list | grep forge"
echo "  Restart:   $APP_DIR/setup-autostart.sh --restart"
echo "  Uninstall: $APP_DIR/setup-autostart.sh --uninstall"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
