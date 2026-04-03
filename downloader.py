"""
downloader.py  —  TeraBox downloader with multiple API fallbacks,
                   mobile-network retry logic, and gzip compression.
"""
import os
import re
import time
import gzip
import shutil
import logging
import requests
from urllib.parse import urlparse, parse_qs, quote

from config import (
    DOWNLOADS_DIR, MAX_FILE_SIZE_MB,
    CHUNK_SIZE_KB, DOWNLOAD_RETRIES, RETRY_DELAY_SEC,
    REQUESTS_POOL_SIZE,
)

logger = logging.getLogger(__name__)

SESSION = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=REQUESTS_POOL_SIZE,
    pool_maxsize=REQUESTS_POOL_SIZE,
    max_retries=requests.adapters.Retry(
        total=3, backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
    )
)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 8.1; Vivo V9) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Mobile Safari/537.36"
    ),
    "Referer": "https://www.terabox.com/",
    "Accept-Language": "en-US,en;q=0.9",
})

_TERABOX_DOMAINS = {
    "terabox.com", "1024terabox.com", "teraboxapp.com",
    "4funbox.com", "mirrorbox.com", "momerybox.com",
    "nephobox.com", "freeterabox.com", "terabox.fun",
    "tibibox.com", "teraboxlink.com",
}

def normalize_terabox_url(text: str) -> str | None:
    text = text.strip()
    url_match = re.search(r"https?://\S+", text)
    if url_match:
        text = url_match.group(0)
    parsed = urlparse(text)
    netloc = parsed.netloc.lstrip("www.")
    if any(netloc == d or netloc.endswith("." + d) for d in _TERABOX_DOMAINS):
        return text
    return None

def _extract_surl(url: str) -> str | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs:
        return qs["surl"][0]
    m = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    if m:
        return m.group(1)
    m2 = re.search(r"surl=([A-Za-z0-9_-]+)", url)
    if m2:
        return m2.group(1)
    return None

def _api_shorturl(surl: str) -> dict | None:
    try:
        r = SESSION.get(
            f"https://www.terabox.com/api/shorturlinfo"
            f"?app_id=250528&shorturl={surl}&root=1",
            timeout=20,
        )
        data = r.json()
        if data.get("errno") != 0:
            return None
        fl = data.get("list", [])
        if not fl:
            return None
        fi = fl[0]
        uk       = data.get("uk", "")
        share_id = data.get("shareid", "")
        sign     = data.get("sign", "")
        ts       = data.get("timestamp", int(time.time()))
        fs_id    = fi.get("fs_id")
        if not fs_id:
            return None
        dl_r = SESSION.get(
            f"https://www.terabox.com/api/download"
            f"?app_id=250528&sign={sign}&timestamp={ts}"
            f"&shareid={share_id}&uk={uk}&product=share"
            f"&nozip=1&fid_list=[{fs_id}]",
            timeout=20,
        )
        dl_data = dl_r.json()
        if dl_data.get("errno") != 0:
            return None
        dlinks = dl_data.get("dlink", [])
        if not dlinks:
            return None
        return {
            "filename":     fi.get("server_filename", "video.mp4"),
            "download_url": dlinks[0].get("dlink", ""),
            "size":         fi.get("size", 0),
        }
    except Exception as e:
        logger.debug(f"API method 1 failed: {e}")
        return None

def _api_proxy(original_url: str) -> dict | None:
    endpoints = [
        f"https://teraboxvideodownloader.nepcoderdevs.workers.dev/?url={quote(original_url)}",
        f"https://terabox-dl.vercel.app/api?url={quote(original_url)}",
    ]
    for ep in endpoints:
        try:
            r = SESSION.get(ep, timeout=20)
            if r.status_code != 200:
                continue
            d = r.json()
            dl_url = (
                d.get("download_url") or d.get("downloadUrl") or
                d.get("dlink") or (d.get("data") or {}).get("download_url")
            )
            fname = (
                d.get("file_name") or d.get("filename") or
                d.get("title") or "video.mp4"
            )
            size = int(
                d.get("size") or d.get("file_size") or
                (d.get("data") or {}).get("size") or 0
            )
            if dl_url:
                return {"filename": fname, "download_url": dl_url, "size": size}
        except Exception as e:
            logger.debug(f"Proxy API failed ({ep}): {e}")
    return None

def _resolve(url: str) -> dict | None:
    surl = _extract_surl(url)
    if surl:
        result = _api_shorturl(surl)
        if result:
            return result
    result = _api_proxy(url)
    if result:
        return result
    return None

def download_video(url: str, user_id: int,
                   progress_callback=None) -> dict | None:
    info = _resolve(url)
    if not info:
        return None

    filename    = _sanitize(info["filename"])
    remote_size = info.get("size", 0)

    if not filename.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".webm")):
        filename += ".mp4"

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if remote_size and remote_size > max_bytes:
        return {"error": "too_large", "size": remote_size}

    user_dir = os.path.join(DOWNLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    raw_path = os.path.join(user_dir, filename)

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            with SESSION.get(
                info["download_url"], stream=True,
                timeout=(10, 120),
                headers={"Accept": "*/*"},
            ) as r:
                r.raise_for_status()
                total    = int(r.headers.get("content-length", 0))
                received = 0
                last_pct = -1
                chunk_sz = CHUNK_SIZE_KB * 1024

                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_sz):
                        if chunk:
                            f.write(chunk)
                            received += len(chunk)
                            if progress_callback and total:
                                pct = min(int(received * 100 / total), 99)
                                if pct != last_pct and pct % 10 == 0:
                                    progress_callback(pct, received, total)
                                    last_pct = pct
            break
        except (requests.RequestException, OSError) as e:
            logger.warning(f"Download attempt {attempt} failed: {e}")
            if os.path.exists(raw_path):
                os.remove(raw_path)
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
            else:
                return None

    original_size = os.path.getsize(raw_path)
    if progress_callback:
        progress_callback(100, original_size, original_size)

    gz_path = raw_path + ".gz"
    _compress(raw_path, gz_path)
    os.remove(raw_path)

    compressed_size = os.path.getsize(gz_path)
    ratio = (1 - compressed_size / original_size) * 100 if original_size else 0
    logger.info(
        f"Saved: {filename} | {format_size(original_size)} → "
        f"{format_size(compressed_size)} ({ratio:.0f}% saved)"
    )

    return {
        "filename":        filename,
        "original_path":   raw_path,
        "compressed_path": gz_path,
        "original_size":   original_size,
        "compressed_size": compressed_size,
    }

def _compress(src: str, dst: str, level: int = 6):
    with open(src, "rb") as fi:
        with gzip.open(dst, "wb", compresslevel=level) as fo:
            shutil.copyfileobj(fi, fo, length=1024 * 512)

def decompress_file(gz_path: str) -> str:
    out = gz_path[:-3] if gz_path.endswith(".gz") else gz_path + ".out"
    with gzip.open(gz_path, "rb") as fi:
        with open(out, "wb") as fo:
            shutil.copyfileobj(fi, fo, length=1024 * 512)
    return out

def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name)
    return name[:200].strip()

def format_size(b: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def cleanup_user_dir(user_id: int):
    d = os.path.join(DOWNLOADS_DIR, str(user_id))
    shutil.rmtree(d, ignore_errors=True)
