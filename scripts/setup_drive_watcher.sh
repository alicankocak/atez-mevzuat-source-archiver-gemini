#!/usr/bin/env bash
# ==============================================================================
# ATEZ Mevzuat Radarı - Drive Request Watcher macOS Launchd Servis Kurulumu
# ==============================================================================

set -e

PLIST_NAME="com.atez.drivewatcher"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
PROJECT_DIR="/Users/alican/Documents/Mevzuat-Monitor"
PYTHON_PATH="${PROJECT_DIR}/.venv/bin/python3"
LOG_DIR="${PROJECT_DIR}/logs"

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

echo "=== ATEZ Drive Watcher Servisi Kuruluyor ==="

# Unload if already loaded
launchctl unload "$PLIST_PATH" 2>/dev/null || true

cat <<EOF > "$PLIST_PATH"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_PATH}</string>
        <string>-m</string>
        <string>src.drive_watcher</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/drive_watcher_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/drive_watcher_stderr.log</string>
</dict>
</plist>
EOF

launchctl load "$PLIST_PATH"
echo "✅ Drive Watcher servisi başarıyla kuruldu ve arka planda başlatıldı!"
echo "Loglar: ${LOG_DIR}/drive_watcher_stdout.log"
