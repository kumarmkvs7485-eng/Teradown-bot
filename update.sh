#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#   update.sh  —  Pull latest code from GitHub & hot-reload bot
# ═══════════════════════════════════════════════════════════════

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

echo -e "${CYAN}━━━ TeraBox Bot Updater ━━━${NC}"

# ── Check if git remote is set ────────────────────────────────────
REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [ -z "$REMOTE" ]; then
    warn "No GitHub remote configured!"
    echo "  Run: git remote add origin https://github.com/YOUR_USER/terabox-bot.git"
    echo ""
    echo "Or pull manually and restart:"
    echo "  bash stop.sh && bash run.sh"
    exit 1
fi

# ── Stash any local changes to protected files ────────────────────
info "Stashing local config…"
git stash push -m "pre-update-stash" -- "*.py" "*.txt" 2>/dev/null || true

# ── Pull latest code ─────────────────────────────────────────────
info "Pulling from $REMOTE…"
if ! git pull --rebase origin main 2>&1; then
    warn "Pull failed. Trying to reset…"
    git fetch origin
    git reset --hard origin/main
fi

# ── Restore local .env (never overwritten) ───────────────────────
git stash pop 2>/dev/null || true

# ── Check for new Python dependencies ────────────────────────────
info "Checking dependencies…"
pip install -r requirements.txt --quiet --upgrade 2>/dev/null
log "Dependencies up to date"

# ── Show what changed ────────────────────────────────────────────
CHANGES=$(git log --oneline -5 2>/dev/null || echo "")
if [ -n "$CHANGES" ]; then
    echo ""
    echo -e "${CYAN}Recent commits:${NC}"
    echo "$CHANGES"
    echo ""
fi

# ── Restart bot ──────────────────────────────────────────────────
info "Restarting bot…"
bash stop.sh 2>/dev/null
sleep 2
nohup bash run.sh > /dev/null 2>&1 &
log "Bot restarted with latest code!"
echo ""
echo "  View logs:  tail -f logs/bot.log"
