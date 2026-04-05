#!/data/data/com.termux/files/usr/bin/bash
# Smart launcher with auto-restart, crash recovery, log rotation

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
LOG="logs/bot.log"; PID_FILE=".bot.pid"; CRASH_FILE=".crashes"
MAX_CRASHES=10; BASE_DELAY=5; MAX_DELAY=120

mkdir -p logs

# Check .env
if [ ! -f ".env" ]; then
    cat > .env << 'ENVEOF'
BOT_TOKEN=YOUR_BOT_TOKEN_HERE
ADMIN_IDS=123456789
UPI_ID=yourname@paytm
UPI_NAME=Your Name
ENVEOF
    echo -e "${RED}[✗] .env was missing — created now.${NC}"
    echo "    Edit it:  nano .env"
    exit 1
fi

TOKEN=$(grep "^BOT_TOKEN=" .env | cut -d= -f2 | tr -d '[:space:]')
if [ -z "$TOKEN" ] || [ "$TOKEN" = "YOUR_BOT_TOKEN_HERE" ]; then
    echo -e "${RED}[✗] BOT_TOKEN not set! Edit .env first:  nano .env${NC}"
    exit 1
fi

# Kill existing instance
if [ -f "$PID_FILE" ]; then
    OLD=$(cat "$PID_FILE")
    if kill -0 "$OLD" 2>/dev/null; then
        echo -e "${YELLOW}[!] Bot already running (PID $OLD). Stop with: bash stop.sh${NC}"
        exit 1
    fi
fi

# Memory settings for 4GB device
export PYTHONOPTIMIZE=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONMALLOC=malloc
export MALLOC_TRIM_THRESHOLD_=65536

# Wake lock
termux-wake-lock 2>/dev/null || true

# Log rotation
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    mv "$LOG" "logs/bot_$(date +%Y%m%d_%H%M).log"
fi

echo 0 > "$CRASH_FILE"
START_TS=$(date +%s)

echo -e "${GREEN}[✓] Starting Downloader Hut Bot…${NC}"
echo "    Logs: tail -f $LOG"
echo "    Stop: bash stop.sh"

while true; do
    CRASHES=$(cat "$CRASH_FILE" 2>/dev/null || echo 0)

    if [ "$CRASHES" -ge "$MAX_CRASHES" ]; then
        ELAPSED=$(( $(date +%s) - START_TS ))
        if [ "$ELAPSED" -lt 300 ]; then
            echo "$(date) — Too many crashes. Stopping." | tee -a "$LOG"
            termux-notification --title "Bot STOPPED" --content "Too many crashes. Run: bash run.sh" 2>/dev/null || true
            exit 1
        fi
        echo 0 > "$CRASH_FILE"; START_TS=$(date +%s)
    fi

    if [ "$CRASHES" -gt 2 ]; then
        DELAY=$(( BASE_DELAY * (2 ** (CRASHES - 2)) ))
        [ "$DELAY" -gt "$MAX_DELAY" ] && DELAY=$MAX_DELAY
        echo "$(date) — Crash #$CRASHES, waiting ${DELAY}s…" >> "$LOG"
        sleep "$DELAY"
    fi

    echo "$(date) — Starting (crash count: $CRASHES)" >> "$LOG"
    python bot.py >> "$LOG" 2>&1 &
    BOT_PID=$!
    echo "$BOT_PID" > "$PID_FILE"
    echo -e "${GREEN}[✓] Bot PID: $BOT_PID${NC}"
    termux-notification --title "Bot Running ✅" --content "PID $BOT_PID" 2>/dev/null || true

    wait "$BOT_PID"
    CODE=$?
    rm -f "$PID_FILE"

    [ "$CODE" -eq 0 ] && { echo "$(date) — Clean exit." >> "$LOG"; break; }

    echo "$(date) — Crashed (code $CODE). Restarting…" | tee -a "$LOG"
    termux-notification --title "Bot Restarting ⚠️" --content "Crashed, auto-restarting..." 2>/dev/null || true
    echo $(( CRASHES + 1 )) > "$CRASH_FILE"
    sleep "$BASE_DELAY"
done
