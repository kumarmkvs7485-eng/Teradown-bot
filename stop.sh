#!/data/data/com.termux/files/usr/bin/bash
# stop.sh — Gracefully stop the bot

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$BOT_DIR/.bot.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "[!] No PID file found. Bot may not be running."
    # Kill any stray python bot.py processes anyway
    pkill -f "python bot.py" 2>/dev/null && echo "[✓] Killed stray bot process." || echo "[i] No stray process found."
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    kill -SIGTERM "$PID"
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID"
        echo "[✓] Bot force-stopped (PID $PID)."
    else
        echo "[✓] Bot stopped gracefully (PID $PID)."
    fi
else
    echo "[!] Process $PID not running."
fi

rm -f "$PID_FILE"
termux-wake-unlock 2>/dev/null || true
termux-notification-remove 1 2>/dev/null || true
echo "[✓] Done."
