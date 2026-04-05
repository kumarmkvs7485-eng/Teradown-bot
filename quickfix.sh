#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#   quickfix.sh — Fix download issue, run this NOW
# ═══════════════════════════════════════════════════════════════
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

echo -e "${YELLOW}━━━ Downloader Hut Quick Fix ━━━${NC}"

# Step 1: Stop bot
echo -e "${YELLOW}[1/5]${NC} Stopping bot…"
bash stop.sh 2>/dev/null; sleep 2

# Step 2: Install yt-dlp (the main fix)
echo -e "${YELLOW}[2/5]${NC} Installing yt-dlp…"
pip install yt-dlp -q 2>/dev/null \
    && echo -e "${GREEN}  ✓ yt-dlp installed${NC}" \
    || echo -e "${RED}  ✗ yt-dlp install failed (will use API fallbacks)${NC}"

# Step 3: Update yt-dlp if already installed (important for TeraBox support)
echo -e "${YELLOW}[3/5]${NC} Updating yt-dlp to latest…"
pip install yt-dlp --upgrade -q 2>/dev/null \
    && echo -e "${GREEN}  ✓ yt-dlp updated${NC}" || true

# Step 4: Verify yt-dlp works
echo -e "${YELLOW}[4/5]${NC} Verifying yt-dlp…"
YTDLP_VER=$(yt-dlp --version 2>/dev/null || echo "not found")
if [ "$YTDLP_VER" = "not found" ]; then
    echo -e "${RED}  ✗ yt-dlp not in PATH${NC}"
    echo "  Trying: pip install yt-dlp --force-reinstall -q"
    pip install yt-dlp --force-reinstall -q 2>/dev/null || true
else
    echo -e "${GREEN}  ✓ yt-dlp $YTDLP_VER${NC}"
fi

# Step 5: Restart bot
echo -e "${YELLOW}[5/5]${NC} Starting bot…"
sleep 1
nohup bash run.sh > /dev/null 2>&1 &
sleep 2

if [ -f ".bot.pid" ]; then
    PID=$(cat .bot.pid)
    echo -e "${GREEN}✅ Bot restarted (PID $PID)${NC}"
    echo ""
    echo "  Watch logs:  tail -f logs/bot.log"
    echo "  Test now:    Send a TeraBox link to your bot"
else
    echo -e "${RED}✗ Bot failed to start. Check: python bot.py${NC}"
fi
