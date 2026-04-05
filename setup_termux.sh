#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#   Downloader Hut — Termux Setup (Vivo V9)
#   Run once:  bash setup_termux.sh
# ═══════════════════════════════════════════════════════════════
set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║   Downloader Hut Bot — Termux Setup      ║"
echo "║   Optimised for Vivo V9 (4GB RAM)        ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# Storage
termux-setup-storage 2>/dev/null || true; sleep 1

# Packages
info "Updating packages…"
pkg update -y -o Dpkg::Options::="--force-confold" 2>/dev/null || true
pkg upgrade -y 2>/dev/null || true

info "Installing system packages…"
pkg install -y python python-pip git curl wget ffmpeg \
    tesseract libjpeg-turbo libpng freetype openssl \
    openssl-tool termux-api 2>/dev/null || true
log "System packages done"

# Python packages — NEVER upgrade pip on Termux
info "Installing Python packages…"
PKGS=(
    "python-telegram-bot==20.7"
    "yt-dlp"
    "requests"
    "python-dotenv"
    "APScheduler==3.10.4"
    "pytz"
    "qrcode"
    "pytesseract"
    "aiohttp"
)
for p in "${PKGS[@]}"; do
    echo -ne "  $p … "
    pip install "$p" -q 2>/dev/null && echo -e "${GREEN}✓${NC}" || echo -e "${YELLOW}skip${NC}"
done

# Pillow via pkg (avoids build issues on ARM)
info "Installing Pillow via pkg…"
pkg install -y python-pillow 2>/dev/null && log "Pillow installed via pkg" || \
    { pip install Pillow -q 2>/dev/null && log "Pillow installed via pip"; }

# .env
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"
if [ ! -f ".env" ]; then
    cat > .env << 'ENVEOF'
BOT_TOKEN=YOUR_BOT_TOKEN_HERE
ADMIN_IDS=123456789
UPI_ID=yourname@paytm
UPI_NAME=Your Name
ENVEOF
    warn ".env created! Edit it now:  nano .env"
else
    log ".env already exists"
fi

# Dirs
mkdir -p data downloads qrcodes logs

# Termux:Boot
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
cat > "$BOOT_DIR/start_bot.sh" << 'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
sleep 20
termux-wake-lock 2>/dev/null || true
cd "$HOME/Teradown-bot"
nohup bash run.sh >> logs/boot.log 2>&1 &
BOOT
chmod +x "$BOOT_DIR/start_bot.sh"
log "Termux:Boot configured"

# Wake lock
termux-wake-lock 2>/dev/null && log "Wake lock acquired" || true

# Git
if [ ! -d ".git" ]; then
    git init -q
    git add .
    git commit -m "Initial commit" -q 2>/dev/null || true
fi

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════╗"
echo "║   ✅  Setup Complete!                    ║"
echo "╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo "  1.  nano .env       ← fill in your tokens"
echo "  2.  bash run.sh     ← start the bot"
echo ""
