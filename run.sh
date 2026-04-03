#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#   run.sh  —  Smart launcher with auto-restart & crash recovery
# ═══════════════════════════════════════════════════════════════

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

LOG_FILE="logs/bot.log"
PID_FILE=".bot.pid"
CRASH_COUNT_FILE=".crash_count"
MAX_RESTARTS=10
RESTART_DELAY=5     # seconds between restarts
BACKOFF_MAX=120     # max delay after many crashes

mkdir -p logs

# ── Colours ──────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

# ── Check .env ───────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo -e "${RED}[✗] .env not found! Copy .env.example and fill it in.${NC}"
    exit 1
fi

TOKEN=$(grep "^BOT_TOKEN=" .env | cut -d= -f2 | tr -d '[:space:]')
if [ -z "$TOKEN" ] || [ "$TOKEN" = "YOUR_BOT_TOKEN_HERE" ]; then
    echo -e "${RED}[✗] BOT_TOKEN not set in .env!${NC}"
    exit 1
fi

# ── Prevent duplicate instances ──────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo -e "${YELLOW}[!] Bot already running (PID $OLD_PID). Use stop.sh first.${NC}"
        exit 1
    fi
fi

# ── Wake lock ────────────────────────────────────────────────────
termux-wake-lock 2>/dev/null || true

# ── Memory optimiser for Vivo V9 (4GB) ──────────────────────────
# Limit Python to use at most 512 MB
export PYTHONMALLOC=malloc
export MALLOC_TRIM_THRESHOLD_=65536
export PYTHONOPTIMIZE=1           # removes assert & __debug__ blocks
export PYTHONDONTWRITEBYTECODE=1  # no .pyc files (saves storage)

# ── Rotate logs > 5 MB ───────────────────────────────────────────
if [ -f "$LOG_FILE" ] && [ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOG_FILE" "logs/bot_$(date +%Y%m%d_%H%M%S).log"
    echo "$(date) — Log rotated" > "$LOG_FILE"
fi

# ── Initialize crash counter ─────────────────────────────────────
echo 0 > "$CRASH_COUNT_FILE"
START_TIME=$(date +%s)

echo -e "${GREEN}[✓] Starting TeraBox Bot...${NC}"
echo "    Logs → $LOG_FILE"
echo "    Press Ctrl+C to stop"
echo ""

# ── Main restart loop ────────────────────────────────────────────
while true; do
    CRASH_COUNT=$(cat "$CRASH_COUNT_FILE" 2>/dev/null || echo 0)

    # Exponential back-off after many crashes
    if [ "$CRASH_COUNT" -gt 3 ]; then
        DELAY=$(( RESTART_DELAY * (2 ** (CRASH_COUNT - 3)) ))
        DELAY=$(( DELAY > BACKOFF_MAX ? BACKOFF_MAX : DELAY ))
        echo "$(date) — Crash #$CRASH_COUNT, waiting ${DELAY}s before restart…" | tee -a "$LOG_FILE"
        sleep "$DELAY"
    fi

    # Stop if too many crashes in a short time
    if [ "$CRASH_COUNT" -ge "$MAX_RESTARTS" ]; then
        ELAPSED=$(( $(date +%s) - START_TIME ))
        if [ "$ELAPSED" -lt 300 ]; then   # 5 minutes
            echo "$(date) — Too many crashes ($CRASH_COUNT) in ${ELAPSED}s. Stopping." | tee -a "$LOG_FILE"
            termux-notification --title "TeraBox Bot STOPPED" \
                --content "Too many crashes. Run: bash run.sh" 2>/dev/null || true
            exit 1
        fi
        echo 0 > "$CRASH_COUNT_FILE"
        START_TIME=$(date +%s)
    fi

    # Start bot
    echo "$(date) — Starting bot (attempt $((CRASH_COUNT+1)))…" >> "$LOG_FILE"
    python bot.py >> "$LOG_FILE" 2>&1 &
    BOT_PID=$!
    echo "$BOT_PID" > "$PID_FILE"

    echo -e "${GREEN}[✓] Bot started (PID $BOT_PID)${NC}"
    termux-notification --title "TeraBox Bot ✅" \
        --content "Bot is running (PID $BOT_PID)" 2>/dev/null || true

    # Wait for process to exit
    wait "$BOT_PID"
    EXIT_CODE=$?

    rm -f "$PID_FILE"

    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "$(date) — Bot exited cleanly (code 0). Stopping." | tee -a "$LOG_FILE"
        break
    fi

    echo "$(date) — Bot crashed (exit code $EXIT_CODE). Restarting in ${RESTART_DELAY}s…" | tee -a "$LOG_FILE"
    termux-notification --title "TeraBox Bot ⚠️" \
        --content "Bot crashed. Auto-restarting..." 2>/dev/null || true

    echo $(( CRASH_COUNT + 1 )) > "$CRASH_COUNT_FILE"
    sleep "$RESTART_DELAY"
done
