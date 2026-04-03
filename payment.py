"""
payment.py  —  UPI payment verification + QR code generation
               Termux-compatible with graceful OCR fallback
"""
import io
import re
import hashlib
import logging
from datetime import datetime

import qrcode
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

try:
    import pytesseract
    # On Termux, tesseract lives here
    pytesseract.pytesseract.tesseract_cmd = "/data/data/com.termux/files/usr/bin/tesseract"
    pytesseract.image_to_string(Image.new("RGB", (10, 10)))  # test call
    OCR_AVAILABLE = True
    logger.info("Tesseract OCR ready.")
except Exception:
    OCR_AVAILABLE = False
    logger.warning("Tesseract OCR not available — manual review mode active.")

# ─── QR Code Generation ───────────────────────────────────────────────────────
def generate_upi_qr(upi_id: str, name: str, amount: float,
                    plan_name: str, save_path: str) -> str:
    upi_uri = (
        f"upi://pay?pa={upi_id}&pn={name.replace(' ', '%20')}"
        f"&am={amount:.2f}&cu=INR"
        f"&tn={plan_name.replace(' ', '%20')}"
    )

    qr = qrcode.QRCode(
        version=3,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0d1b2a", back_color="#ffffff").convert("RGBA")
    w, h = img.size

    # Add label bar at bottom
    bar_height = 50
    final = Image.new("RGBA", (w, h + bar_height), "#ffffff")
    final.paste(img, (0, 0))

    draw = ImageDraw.Draw(final)
    draw.rectangle([(0, h), (w, h + bar_height)], fill="#0d1b2a")

    label = f"Pay ₹{amount:.0f}  |  {upi_id}"
    # Use default font (no extra font needed on Termux)
    draw.text((w // 2, h + bar_height // 2), label,
              fill="white", anchor="mm")

    final.save(save_path)
    logger.info(f"QR saved: {save_path}")
    return save_path

# ─── Screenshot Hash ──────────────────────────────────────────────────────────
def hash_screenshot(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()

# ─── OCR Extraction ───────────────────────────────────────────────────────────
_TXN_PATTERNS = [
    r"(?i)(?:utr|ref(?:erence)?|txn|transaction)[^\w]*([A-Z0-9]{8,25})",
    r"\b([A-Z]{2,4}[0-9]{8,20})\b",
    r"\b(\d{12,16})\b",
]
_AMOUNT_PATTERNS = [
    r"(?i)(?:rs\.?|inr|₹)\s*(\d+(?:[.,]\d{1,2})?)",
    r"(\d+(?:[.,]\d{1,2})?)\s*(?:rs\.?|inr|₹)",
    r"(?i)(?:paid|amount|total)[^\d]*(\d+(?:[.,]\d{1,2})?)",
]
_DATE_PATTERNS = [
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
    r"\b(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b",
    r"(?i)(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{2,4})",
    r"(?i)((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+\d{4})",
]
FAKE_KEYWORDS = [
    "cancelled", "cancel", "failed", "decline", "declined",
    "reversed", "refunded", "insufficient", "error", "invalid",
    "test payment", "demo", "sample", "fake", "simulation",
    "pending approval",
]
SUCCESS_KEYWORDS = [
    "success", "successful", "completed", "approved",
    "paid", "credited", "received", "payment done",
    "payment successful", "transaction successful",
]

def extract_payment_info(image_bytes: bytes) -> dict:
    result = {
        "transaction_id": None,
        "amount": None,
        "date_str": None,
        "raw_text": "",
        "confidence": "low",
        "suspicious_flags": [],
        "success_signals": [],
    }

    if not OCR_AVAILABLE:
        result["suspicious_flags"].append("ocr_unavailable")
        return result

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # Upscale for better OCR on mobile screenshots
        scale = 2
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        raw = pytesseract.image_to_string(img, lang="eng", config="--psm 6")
        result["raw_text"] = raw
    except Exception as e:
        logger.error(f"OCR error: {e}")
        result["suspicious_flags"].append("ocr_error")
        return result

    text_low = raw.lower()

    for w in FAKE_KEYWORDS:
        if w in text_low:
            result["suspicious_flags"].append(f"bad_keyword:{w}")

    for w in SUCCESS_KEYWORDS:
        if w in text_low:
            result["success_signals"].append(w)

    for pat in _TXN_PATTERNS:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            result["transaction_id"] = m.group(1).upper().strip()
            break

    for pat in _AMOUNT_PATTERNS:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            try:
                result["amount"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    for pat in _DATE_PATTERNS:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            result["date_str"] = m.group(0)
            break

    if not result["date_str"]:
        result["suspicious_flags"].append("no_date")

    # Confidence
    score = 0
    if result["transaction_id"]:  score += 3
    if result["amount"]:           score += 2
    if result["date_str"]:         score += 1
    if result["success_signals"]:  score += 2
    if not result["suspicious_flags"]: score += 1

    result["confidence"] = "high" if score >= 6 else "medium" if score >= 3 else "low"
    return result

# ─── Full Verification Pipeline ───────────────────────────────────────────────
def verify_payment(image_bytes: bytes, expected_amount: float,
                   screenshot_hash: str, hash_exists: bool) -> dict:
    if hash_exists:
        return {
            "approved": False,
            "reason": "duplicate_screenshot",
            "transaction_id": None,
            "ocr_info": {},
            "needs_manual_review": True,
        }

    ocr = extract_payment_info(image_bytes)

    bad_keywords = [f for f in ocr["suspicious_flags"] if f.startswith("bad_keyword:")]
    if bad_keywords:
        return {
            "approved": False,
            "reason": f"suspicious_content ({', '.join(bad_keywords)})",
            "transaction_id": None,
            "ocr_info": ocr,
            "needs_manual_review": True,
        }

    if ocr["amount"] is not None and expected_amount > 0:
        if abs(ocr["amount"] - expected_amount) > 1.0:
            return {
                "approved": False,
                "reason": f"amount_mismatch (found ₹{ocr['amount']:.0f}, expected ₹{expected_amount:.0f})",
                "transaction_id": ocr["transaction_id"],
                "ocr_info": ocr,
                "needs_manual_review": True,
            }

    # If OCR unavailable → queue for manual review but don't block
    if not OCR_AVAILABLE:
        auto_txn = f"MANUAL_{screenshot_hash[:10].upper()}"
        return {
            "approved": True,
            "reason": "ocr_unavailable_manual_review",
            "transaction_id": auto_txn,
            "ocr_info": ocr,
            "needs_manual_review": True,
        }

    if ocr["confidence"] == "low":
        return {
            "approved": False,
            "reason": "low_confidence_screenshot",
            "transaction_id": ocr["transaction_id"],
            "ocr_info": ocr,
            "needs_manual_review": True,
        }

    txn = ocr["transaction_id"] or f"AUTO_{screenshot_hash[:10].upper()}"

    if ocr["confidence"] == "medium":
        return {
            "approved": True,
            "reason": "medium_confidence_auto_approved",
            "transaction_id": txn,
            "ocr_info": ocr,
            "needs_manual_review": True,
        }

    return {
        "approved": True,
        "reason": "auto_verified_high_confidence",
        "transaction_id": txn,
        "ocr_info": ocr,
        "needs_manual_review": False,
    }
