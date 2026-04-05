"""
downloader.py  —  TeraBox video downloader
  Method 1: yt-dlp  (most reliable)
  Method 2: TeraBox open API  (fallback)
  Method 3: Scrape + direct link  (last resort)
"""
import os, re, time, gzip, json, shutil, logging, subprocess, tempfile
import requests
from urllib.parse import urlparse, parse_qs, urlencode, quote
from config import (
    DOWNLOADS_DIR, MAX_FILE_SIZE_MB, CHUNK_SIZE_KB,
    DOWNLOAD_RETRIES, RETRY_DELAY_SEC,
)

logger = logging.getLogger(__name__)

# ── HTTP session ──────────────────────────────────────────────────────────────
S = requests.Session()
S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 9; Vivo V9) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})
_retry = requests.adapters.Retry(total=3, backoff_factor=1,
                                  status_forcelist=[500, 502, 503, 504])
S.mount("https://", requests.adapters.HTTPAdapter(max_retries=_retry))
S.mount("http://",  requests.adapters.HTTPAdapter(max_retries=_retry))

# ── Supported TeraBox domains ─────────────────────────────────────────────────
TERABOX_DOMAINS = {
    "terabox.com", "www.terabox.com",
    "1024terabox.com", "www.1024terabox.com",
    "teraboxapp.com", "www.teraboxapp.com",
    "4funbox.com", "mirrorbox.com", "momerybox.com",
    "nephobox.com", "freeterabox.com", "terabox.fun",
    "tibibox.com", "teraboxlink.com", "terafileshare.com",
    "1024tera.com", "www.1024tera.com",
}

def is_terabox_url(text: str) -> str | None:
    """Return cleaned URL if it's a TeraBox link, else None."""
    text = text.strip()
    m = re.search(r"https?://[^\s\]\"'<>]+", text)
    if m:
        text = m.group(0)
    parsed = urlparse(text)
    host = parsed.netloc.lower().lstrip("www.")
    if any(host == d or host.endswith("." + d) for d in TERABOX_DOMAINS):
        return text
    return None

# ── yt-dlp availability check ─────────────────────────────────────────────────
def _ytdlp_available() -> bool:
    try:
        r = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 1: yt-dlp
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_ytdlp(url: str) -> dict | None:
    """Use yt-dlp to extract direct video info."""
    if not _ytdlp_available():
        logger.warning("yt-dlp not installed — skipping")
        return None
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--no-playlist",
                "--no-warnings",
                "--quiet",
                "--user-agent",
                "Mozilla/5.0 (Android 9; Mobile) AppleWebKit/537.36 Chrome/124.0",
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug(f"yt-dlp error: {result.stderr[:200]}")
            return None
        info = json.loads(result.stdout)
        # Pick best format ≤ 50MB
        formats = info.get("formats", [])
        best = None
        for fmt in reversed(formats):
            size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
            if size and size > MAX_FILE_SIZE_MB * 1024 * 1024:
                continue
            ext = fmt.get("ext", "").lower()
            if ext in ("mp4", "webm", "mkv", "m4v") or fmt.get("vcodec") not in (None, "none"):
                best = fmt
                break
        if not best and formats:
            best = formats[-1]  # take whatever we have
        if not best:
            return None
        filename = _safe_name(info.get("title") or info.get("id") or "video")
        if not filename.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
            filename += "." + (best.get("ext") or "mp4")
        return {
            "filename":     filename,
            "download_url": best.get("url", ""),
            "size":         best.get("filesize") or best.get("filesize_approx") or 0,
            "ext":          best.get("ext", "mp4"),
            "headers":      best.get("http_headers", {}),
        }
    except Exception as e:
        logger.debug(f"yt-dlp resolve failed: {e}")
        return None

def _download_ytdlp(url: str, out_path: str,
                    progress_cb=None) -> bool:
    """Download via yt-dlp directly to file."""
    if not _ytdlp_available():
        return False
    try:
        out_template = out_path.rsplit(".", 1)[0] + ".%(ext)s" if "." in os.path.basename(out_path) else out_path + ".%(ext)s"
        cmd = [
            "yt-dlp",
            "--no-playlist", "--no-warnings",
            "--merge-output-format", "mp4",
            "--output", out_template,
            "--max-filesize", f"{MAX_FILE_SIZE_MB}m",
            "--retries", str(DOWNLOAD_RETRIES),
            "--fragment-retries", "5",
            "--user-agent",
            "Mozilla/5.0 (Android 9; Mobile) AppleWebKit/537.36 Chrome/124.0",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            # Find the actual output file
            base = out_path.rsplit(".", 1)[0] if "." in os.path.basename(out_path) else out_path
            for ext in ("mp4", "mkv", "webm", "m4v", "avi"):
                candidate = base + "." + ext
                if os.path.exists(candidate):
                    if candidate != out_path:
                        shutil.move(candidate, out_path)
                    return True
            # Try exact path
            if os.path.exists(out_path):
                return True
        logger.debug(f"yt-dlp download failed: {proc.stderr[:300]}")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out")
        return False
    except Exception as e:
        logger.debug(f"yt-dlp exception: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 2: TeraBox Open APIs
# ─────────────────────────────────────────────────────────────────────────────
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

def _api_terabox(url: str) -> dict | None:
    """
    TeraBox official short-URL API → download-link API chain.
    Works for public share links.
    """
    surl = _extract_surl(url)
    if not surl:
        return None
    try:
        # Step 1: get file list
        r1 = S.get(
            "https://www.1024terabox.com/api/shorturlinfo",
            params={"app_id": "250528", "shorturl": surl, "root": "1"},
            timeout=20,
        )
        d1 = r1.json()
        if d1.get("errno") != 0:
            # Try alternate domain
            r1 = S.get(
                "https://www.terabox.com/api/shorturlinfo",
                params={"app_id": "250528", "shorturl": surl, "root": "1"},
                timeout=20,
            )
            d1 = r1.json()
        if d1.get("errno") != 0:
            logger.debug(f"TeraBox API errno={d1.get('errno')} errmsg={d1.get('errmsg')}")
            return None

        fl = d1.get("list", [])
        if not fl:
            return None
        fi   = fl[0]
        fs_id  = fi.get("fs_id") or fi.get("fs_id")
        fname  = fi.get("server_filename", "video.mp4")
        fsize  = fi.get("size", 0)
        uk     = d1.get("uk", "")
        sid    = d1.get("shareid", "")
        sign   = d1.get("sign", "")
        ts     = d1.get("timestamp", int(time.time()))

        if not fs_id:
            return None

        # Step 2: get download link
        for base in ("https://www.1024terabox.com", "https://www.terabox.com"):
            try:
                r2 = S.get(
                    f"{base}/api/download",
                    params={
                        "app_id": "250528", "sign": sign,
                        "timestamp": ts, "shareid": sid,
                        "uk": uk, "product": "share",
                        "nozip": "1", "fid_list": f"[{fs_id}]",
                    },
                    timeout=20,
                )
                d2 = r2.json()
                if d2.get("errno") == 0:
                    dlinks = d2.get("dlink", [])
                    if dlinks:
                        return {
                            "filename":     fname,
                            "download_url": dlinks[0].get("dlink", ""),
                            "size":         fsize,
                            "headers":      {},
                        }
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"TeraBox API failed: {e}")
    return None

def _api_proxy(url: str) -> dict | None:
    """Open proxy APIs as last resort."""
    endpoints = [
        f"https://teraboxvideodownloader.nepcoderdevs.workers.dev/?url={quote(url)}",
        f"https://terabox.udayscript.com/api?url={quote(url)}",
        f"https://ytdl.udayscript.com/terabox?url={quote(url)}",
    ]
    for ep in endpoints:
        try:
            r = S.get(ep, timeout=25)
            if r.status_code != 200:
                continue
            d = r.json()
            # Normalise different response shapes
            dl = (
                d.get("download_url") or d.get("downloadUrl") or
                d.get("dlink") or d.get("url") or
                (d.get("data") or {}).get("download_url") or
                (d.get("data") or {}).get("dlink")
            )
            fn = (
                d.get("file_name") or d.get("filename") or
                d.get("title") or d.get("name") or "video.mp4"
            )
            sz = int(
                d.get("size") or d.get("file_size") or
                (d.get("data") or {}).get("size") or 0
            )
            if dl:
                logger.info(f"Proxy resolved: {ep}")
                return {"filename": fn, "download_url": dl, "size": sz, "headers": {}}
        except Exception as e:
            logger.debug(f"Proxy {ep} failed: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  STREAM DOWNLOAD (for methods 2 & 3)
# ─────────────────────────────────────────────────────────────────────────────
def _stream_download(info: dict, dest: str, progress_cb=None) -> bool:
    dl_url  = info["download_url"]
    extra_h = info.get("headers", {})
    max_b   = MAX_FILE_SIZE_MB * 1024 * 1024
    chunk   = CHUNK_SIZE_KB * 1024

    hdrs = {"Referer": "https://www.terabox.com/", "Accept": "*/*"}
    hdrs.update(extra_h)

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            with S.get(dl_url, stream=True, timeout=(15, 180), headers=hdrs) as r:
                r.raise_for_status()
                total    = int(r.headers.get("content-length", 0))
                received = 0
                last_pct = -1

                if total and total > max_b:
                    logger.warning(f"Remote reports {total} bytes — too large")
                    return False

                with open(dest, "wb") as f:
                    for blk in r.iter_content(chunk_size=chunk):
                        if blk:
                            f.write(blk)
                            received += len(blk)
                            if received > max_b:
                                logger.warning("Download exceeded size limit mid-stream")
                                return False
                            if progress_cb and total:
                                pct = min(int(received * 100 / total), 99)
                                if pct != last_pct and pct % 10 == 0:
                                    progress_cb(pct, received, total)
                                    last_pct = pct
            if progress_cb:
                progress_cb(100, received, received)
            return True
        except Exception as e:
            logger.warning(f"Stream attempt {attempt} failed: {e}")
            if os.path.exists(dest):
                os.remove(dest)
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
    return False

# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC INTERFACE
# ─────────────────────────────────────────────────────────────────────────────
def download_video(url: str, user_id: int, progress_cb=None) -> dict | None:
    """
    Download a TeraBox video. Returns:
      {filename, compressed_path, original_size, compressed_size}
    or {"error": "too_large"|"failed", ...}
    """
    user_dir = os.path.join(DOWNLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    # ── Try yt-dlp direct download first (most reliable) ─────────
    ytdlp_path = os.path.join(user_dir, "ytdlp_out.mp4")
    logger.info(f"Trying yt-dlp for {url}")
    if _download_ytdlp(url, ytdlp_path, progress_cb):
        if os.path.exists(ytdlp_path) and os.path.getsize(ytdlp_path) > 1000:
            orig_size = os.path.getsize(ytdlp_path)
            # Get a proper filename
            info = _resolve_ytdlp(url)
            filename = info["filename"] if info else os.path.basename(ytdlp_path)
            filename = _safe_name(filename)
            final_path = os.path.join(user_dir, filename)
            if ytdlp_path != final_path:
                shutil.move(ytdlp_path, final_path)
            return _compress_and_return(final_path, filename, orig_size)

    # Cleanup any partial yt-dlp file
    for f in os.listdir(user_dir):
        fp = os.path.join(user_dir, f)
        if os.path.isfile(fp) and not fp.endswith(".gz"):
            os.remove(fp)

    # ── Try API methods ───────────────────────────────────────────
    info = _api_terabox(url) or _api_proxy(url)
    if not info:
        logger.error("All resolution methods failed")
        return None

    if not info.get("download_url"):
        return None

    size = info.get("size", 0)
    if size and size > MAX_FILE_SIZE_MB * 1024 * 1024:
        return {"error": "too_large", "size": size}

    filename  = _safe_name(info.get("filename") or "video.mp4")
    if not filename.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".avi")):
        filename += ".mp4"

    raw_path = os.path.join(user_dir, filename)
    logger.info(f"Downloading via HTTP stream: {filename}")

    if not _stream_download(info, raw_path, progress_cb):
        logger.error("HTTP stream download failed")
        return None

    orig_size = os.path.getsize(raw_path)
    return _compress_and_return(raw_path, filename, orig_size)

def _compress_and_return(raw_path: str, filename: str, orig_size: int) -> dict:
    gz_path = raw_path + ".gz"
    try:
        with open(raw_path, "rb") as fi, gzip.open(gz_path, "wb", compresslevel=1) as fo:
            # compresslevel=1 = fast, low CPU — important on Termux
            shutil.copyfileobj(fi, fo, length=1024 * 1024)
        os.remove(raw_path)
        comp_size = os.path.getsize(gz_path)
    except Exception as e:
        logger.warning(f"Compression failed ({e}), using raw file")
        gz_path   = raw_path
        comp_size = orig_size

    logger.info(f"Ready: {filename}  {format_size(orig_size)} → {format_size(comp_size)}")
    return {
        "filename":        filename,
        "compressed_path": gz_path,
        "original_size":   orig_size,
        "compressed_size": comp_size,
    }

def decompress_file(gz_path: str) -> str:
    if not gz_path.endswith(".gz"):
        return gz_path  # not compressed
    out = gz_path[:-3]
    with gzip.open(gz_path, "rb") as fi, open(out, "wb") as fo:
        shutil.copyfileobj(fi, fo, length=1024 * 1024)
    return out

def cleanup_user_dir(user_id: int):
    d = os.path.join(DOWNLOADS_DIR, str(user_id))
    shutil.rmtree(d, ignore_errors=True)

def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name[:180] or "video.mp4"

def format_size(b: int) -> str:
    if b <= 0:
        return "? MB"
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def install_ytdlp():
    """Install yt-dlp if not present. Call once at startup."""
    if _ytdlp_available():
        logger.info("yt-dlp already installed.")
        return True
    logger.info("Installing yt-dlp…")
    try:
        r = subprocess.run(
            ["pip", "install", "yt-dlp", "--quiet"],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0:
            logger.info("yt-dlp installed successfully.")
            return True
        logger.error(f"yt-dlp install failed: {r.stderr.decode()[:200]}")
        return False
    except Exception as e:
        logger.error(f"yt-dlp install exception: {e}")
        return False
