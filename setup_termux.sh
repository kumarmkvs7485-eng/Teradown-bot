#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#   TeraBox Bot — Termux Setup Script
#   Device: Vivo V9 (4GB RAM / 64GB)
#   Run once: bash setup_termux.sh
# ═══════════════════════════════════════════════════════════════

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║   TeraBox Bot — Termux Installer         ║"
echo "║   Optimised for Vivo V9 (4GB RAM)        ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Step 1: Storage permission ───────────────────────────────────
info "Requesting storage access…"
termux-setup-storage 2>/dev/null || true
sleep 2

# ── Step 2: Update packages ──────────────────────────────────────
info "Updating package list…"
pkg update -y -o Dpkg::Options::="--force-confold" 2>/dev/null
pkg upgrade -y 2>/dev/null || true
log "Packages updated"

# ── Step 3: Install system dependencies ─────────────────────────
info "Installing system packages…"
pkg install -y \
    python \
    python-pip \
    git \
    curl \
    wget \
    ffmpeg \
    tesseract \
    libjpeg-turbo \
    libpng \
    freetype \
    openssl \
    openssl-tool \
    pkg-config \
    clang \
    make \
    libxml2 \
    libxslt \
    2>/dev/null

log "System packages installed"

# ── Step 4: Install Termux:API (for notifications & wakelock) ───
info "Checking Termux:API…"
pkg install -y termux-api 2>/dev/null || warn "termux-api not available — install from F-Droid for best experience"

# ── Step 5: Python environment ───────────────────────────────────
info "Upgrading pip…"
pip install --upgrade pip --quiet

info "Installing Python dependencies…"
pip install \
    "python-telegram-bot==20.7" \
    "requests==2.31.0" \
    "Pillow==10.2.0" \
    "qrcode[pil]==7.4.2" \
    "pytesseract==0.3.10" \
    "APScheduler==3.10.4" \
    "python-dotenv==1.0.0" \
    "aiohttp==3.9.3" \
    "aiofiles==23.2.1" \
    "pytz==2024.1" \
    --quiet

log "Python packages installed"

# ── Step 6: Create .env from template ───────────────────────────
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

if [ ! -f ".env" ]; then
    cp .env.example .env
    warn ".env created from template — EDIT IT before starting the bot!"
    echo ""
    echo -e "${BOLD}  Open .env and fill in:${NC}"
    echo "    BOT_TOKEN  → from @BotFather"
    echo "    ADMIN_IDS  → your Telegram numeric user ID"
    echo "    UPI_ID     → your UPI address"
    echo "    UPI_NAME   → your name"
    echo ""
fi

# ── Step 7: Create required directories ─────────────────────────
mkdir -p data downloads qrcodes logs

# ── Step 8: Termux:Boot auto-start ──────────────────────────────
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
cat > "$BOOT_DIR/start_terabox_bot.sh" << 'BOOTSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# Wait for network
sleep 15
# Acquire wakelock
termux-wake-lock 2>/dev/null || true
# Start bot
cd "$HOME/terabox_bot"
bash run.sh >> logs/boot.log 2>&1 &
BOOTSCRIPT
chmod +x "$BOOT_DIR/start_terabox_bot.sh"
log "Termux:Boot auto-start configured"

# ── Step 9: Acquire wake lock now ───────────────────────────────
termux-wake-lock 2>/dev/null && log "Wake lock acquired (prevents CPU sleep)" || true

# ── Step 10: Git setup ───────────────────────────────────────────
if [ ! -d ".git" ]; then
    git init
    git add .
    git commit -m "Initial commit" --quiet
    info "Git repository initialised. Push to GitHub with:"
    echo "  git remote add origin https://github.com/YOUR_USER/terabox-bot.git"
    echo "  git push -u origin main"
fi

# ── Done ─────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════╗"
echo "║   ✅  Setup Complete!                    ║"
echo "╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo "  1. nano .env           (fill in tokens)"
echo "  2. bash run.sh         (start bot)"
echo "  3. bash update.sh      (pull updates from GitHub)"
echo ""
echo -e "  ${BOLD}Useful commands:${NC}"
echo "  • View live logs:   tail -f logs/bot.log"
echo "  • Stop bot:         bash stop.sh"
echo "  • GitHub update:    bash update.sh"
echo ""
