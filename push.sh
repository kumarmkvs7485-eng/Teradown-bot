#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════════
#   push.sh  —  Commit & push changes to GitHub
# ═══════════════════════════════════════════════════════════════

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

MSG="${1:-Auto-update $(date '+%Y-%m-%d %H:%M')}"

echo -e "${CYAN}━━━ Pushing to GitHub ━━━${NC}"

git add -A
git status --short

if git diff --cached --quiet; then
    echo "[i] Nothing to commit."
    exit 0
fi

git commit -m "$MSG"
git push origin main

echo -e "${GREEN}[✓] Pushed: $MSG${NC}"
