"""
payment.py — UPI payment verification + QR code generation
Auto-approves genuine payments, flags suspicious ones for admin review.
"""
import io, re, hashlib, logging
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = (
        "/data/data/com.termux/files/usr/bin/tesseract"
    )
    pytesseract.image_to_string(Image.new("RGB", (10, 10)))
    OCR_AVAILABLE = True
    logger.info("OCR: Tesseract ready")
except Exception:
    OCR_AVAILABLE = False
    logger.warning("OCR: Tesseract not available — using hash-only mode")

try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    logger.warning("qrcode not installed")

# ── QR Code ───────────────────────────────────────────────────────────────────
def generate_upi_qr(upi_id: str, name: str, amount: float,
                    plan_name: str, save_path: str) -> str:
    if not QR_AVAILABLE:
        # Fallback: blank white image with text
        img = Image.new("RGB", (300, 300), "white")
        draw = ImageDraw.Draw(img)
        draw.text((20, 120), f"Pay ₹{amount:.0f}", fill="black")
        draw.text((20, 150), f"UPI: {upi_id}", fill="black")
        img.save(save_path)
        return save_path

    upi_uri = (
        f"upi://pay?pa={upi_id}"
        f"&pn={name.replace(' ', '%20')}"
        f"&am={amount:.2f}&cu=INR"
        f"&tn={plan_name.replace(' ', '%20')}"
    )
    qr = qrcode.QRCode(version=3,
                        error_correction=qrcode.constants.ERROR_CORRECT_H,
                        box_size=9, border=3)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0d1b2a", back_color="white").convert("RGB")
    w, h = img.size

    # Label bar
    canvas = Image.new("RGB", (w, h + 45), "#0d1b2a")
    canvas.paste(img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((w // 2, h + 22), f"Pay ₹{amount:.0f}  |  {upi_id}",
              fill="white", anchor="mm")
    canvas.save(save_path)
    return save_path

# ── Screenshot hash ───────────────────────────────────────────────────────────
def hash_screenshot(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# ── OCR Patterns ─────────────────────────────────────────────────────────────
_TXN = [
    r"(?i)(?:utr|ref(?:erence)?|txn|transaction\s*id)[^\w]*([A-Z0-9]{8,25})",
    r"\b([A-Z]{2,5}[0-9]{8,18})\b",
    r"\b(\d{12,16})\b",
]
_AMT = [
    r"(?i)(?:rs\.?|inr|₹)\s*(\d[\d,]*(?:\.\d{1,2})?)",
    r"(\d[\d,]*(?:\.\d{1,2})?)\s*(?:rs\.?|inr|₹)",
    r"(?i)(?:paid|amount|total)[^\d]{0,10}(\d[\d,]*(?:\.\d{1,2})?)",
]
_DT  = [
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
    r"(?i)(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4})",
    r"(?i)((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+\d{4})",
    r"\b(\d{4}-\d{2}-\d{2})\b",
]
BAD  = ["cancelled","cancel","failed","failure","declined","decline",
        "reversed","refunded","insufficient","error","invalid",
        "test payment","demo","sample","fake",]
GOOD = ["success","successful","completed","approved","paid","credited",
        "received","payment done","payment successful","debited","debit",]

def _ocr_extract(image_bytes: bytes) -> dict:
    r = {"txn": None, "amount": None, "date": None,
         "raw": "", "bad_words": [], "good_words": [], "score": 0}
    if not OCR_AVAILABLE:
        return r
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        raw = pytesseract.image_to_string(img, config="--psm 6")
        r["raw"] = raw
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return r

    low = raw.lower()
    r["bad_words"]  = [w for w in BAD  if w in low]
    r["good_words"] = [w for w in GOOD if w in low]

    for p in _TXN:
        m = re.search(p, raw, re.I)
        if m:
            r["txn"] = m.group(1).upper().strip()
            break

    for p in _AMT:
        m = re.search(p, raw, re.I)
        if m:
            try:
                r["amount"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    for p in _DT:
        m = re.search(p, raw, re.I)
        if m:
            r["date"] = m.group(0)
            break

    s = 0
    if r["txn"]:          s += 3
    if r["amount"]:        s += 2
    if r["date"]:          s += 1
    if r["good_words"]:    s += 2
    if not r["bad_words"]: s += 1
    r["score"] = s
    return r

# ── Main verification ─────────────────────────────────────────────────────────
def verify_payment(image_bytes: bytes, expected_amount: float,
                   screenshot_hash: str, hash_exists: bool,
                   mode: str = "auto") -> dict:
    """
    Returns:
      approved bool, reason str, transaction_id str|None,
      needs_manual_review bool, suspicious bool, ocr dict
    """
    def _r(approved, reason, txn=None, manual=False, suspicious=False, ocr=None):
        return {
            "approved": approved, "reason": reason,
            "transaction_id": txn, "needs_manual_review": manual,
            "suspicious": suspicious, "ocr_info": ocr or {},
        }

    # Duplicate screenshot
    if hash_exists:
        return _r(False, "duplicate_screenshot", suspicious=True, manual=True)

    ocr = _ocr_extract(image_bytes)

    # Fake/failed keywords
    if ocr["bad_words"]:
        return _r(False, f"fake_screenshot ({', '.join(ocr['bad_words'])})",
                  suspicious=True, manual=True, ocr=ocr)

    # Amount mismatch (only if OCR found an amount)
    if ocr["amount"] is not None and expected_amount > 0:
        diff = abs(ocr["amount"] - expected_amount)
        if diff > 1.5:
            return _r(False,
                      f"amount_mismatch (found ₹{ocr['amount']:.0f}, expected ₹{expected_amount:.0f})",
                      txn=ocr["txn"], manual=True, suspicious=diff > 5, ocr=ocr)

    auto_txn = ocr["txn"] or f"AUTO_{screenshot_hash[:12].upper()}"

    # Manual mode — never auto approve
    if mode == "manual":
        return _r(False, "manual_review_required", txn=auto_txn, manual=True, ocr=ocr)

    # OCR unavailable — approve with manual review flag
    if not OCR_AVAILABLE:
        return _r(True, "ocr_unavailable_auto_approved",
                  txn=auto_txn, manual=True, ocr=ocr)

    # Strict mode — only approve high confidence
    if mode == "strict" and ocr["score"] < 6:
        return _r(False, "strict_mode_low_confidence",
                  txn=auto_txn, manual=True, ocr=ocr)

    # Auto mode
    if ocr["score"] >= 5:
        return _r(True, "auto_approved_high_confidence",
                  txn=auto_txn, manual=False, ocr=ocr)

    if ocr["score"] >= 3:
        # Approve but flag for admin
        return _r(True, "auto_approved_medium_confidence",
                  txn=auto_txn, manual=True, ocr=ocr)

    # Low confidence — ask for better screenshot, don't flag as suspicious
    return _r(False, "low_confidence_screenshot",
              txn=auto_txn, manual=False, ocr=ocr)
