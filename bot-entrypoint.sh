#!/usr/bin/env sh
set -e

# Clean up any stale Xvfb lock
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Chromium/Pydoll needs the virtual display
Xvfb :99 -screen 0 1280x900x24 -ac +extension GLX +render -noreset &

sleep 1

# Window manager used by the original SpotiFLAC image
fluxbox -display :99 >/dev/null 2>&1 &

export DISPLAY=:99
export TS_DEBUG_VISIBLE=1

echo "========================================"
echo "SpotiFLAC Telegram Bot"
echo "DISPLAY=$DISPLAY"
echo "========================================"

exec python3 /app/bot/bot.py
