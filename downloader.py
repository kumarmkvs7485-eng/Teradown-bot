"""
downloader.py — TeraBox Video Downloader
5 fallback methods, session-based scraping, works on Termux ARM
"""
import os, re, time, gzip, json, shutil, logging, subprocess, hashlib
import requests
from urllib.parse import urlparse, parse_qs, quote, urlencode
from config import DOWNLOADS_DIR, MAX_FILE_SIZE_MB, CHUNK_SIZE_KB, DOWNLOAD_RETRIES

logger = logging.getLogger(__name__)

MAX_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
CHUNK     = CHUNK_SIZE_KB * 1024

# ── Shared HTTP session ───────────────────────────────────────────────────────
S = requests.Session()
S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 9; Vivo V9) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})
S.mount("https://", requests.adapters.HTTPAdapter(
    max_retries=requests.adapters.Retry(total=3, backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504])
))
S.mount("http://", requests.adapters.HTTPAdapter(
    max_retries=requests.adapters.Retry(total=3, backoff_factor=1)
))

# ── TeraBox domains ───────────────────────────────────────────────────────────
TB_DOMAINS = {
    "terabox.com", "1024terabox.com", "teraboxapp.com",
    "4funbox.com", "mirrorbox.com", "momerybox.com",
    "nephobox.com", "freeterabox.com", "terabox.fun",
    "tibibox.com", "teraboxlink.com", "1024tera.com",
    "terafileshare.com", "terasharelink.com",
}

def is_terabox_url(text: str) -> str | None:
    text = text.strip()
    m = re.search(r"https?://[^\s\"'<>\]]+", text)
    if m:
        text = m.group(0).rstrip(")")
    parsed = urlparse(text)
    host = parsed.netloc.lower().lstrip("www.")
    if any(host == d or host.endswith("." + d) for d in TB_DOMAINS):
        return text
    return None

def _surl(url: str) -> str | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs:
        return qs["surl"][0]
    m = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    return m.group(1) if m else None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 1: yt-dlp  (most reliable)
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_ok() -> bool:
    try:
        r = subprocess.run(["yt-dlp", "--version"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def _dl_ytdlp(url: str, out: str, prog=None) -> bool:
    if not _ytdlp_ok():
        return False
    base = out.rsplit(".", 1)[0] if "." in os.path.basename(out) else out
    tmpl = base + ".%(ext)s"
    cmd  = [
        "yt-dlp",
        "--no-playlist", "--no-warnings", "--quiet",
        "--merge-output-format", "mp4",
        "--output", tmpl,
        "--max-filesize", f"{MAX_FILE_SIZE_MB}m",
        "--retries", "4",
        "--fragment-retries", "4",
        "--socket-timeout", "30",
        "--user-agent",
        "Mozilla/5.0 (Android 9; Mobile) Chrome/124.0",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            for ext in ("mp4", "mkv", "webm", "m4v", "avi"):
                c = base + "." + ext
                if os.path.exists(c) and os.path.getsize(c) > 1000:
                    if c != out:
                        shutil.move(c, out)
                    if prog:
                        prog(100, os.path.getsize(out), os.path.getsize(out))
                    return True
        logger.debug(f"yt-dlp stderr: {proc.stderr[:300]}")
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out")
    except Exception as e:
        logger.debug(f"yt-dlp exception: {e}")
    return False

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 2: TeraBox session scraper  (proper cookie-based API)
# ─────────────────────────────────────────────────────────────────────────────
def _scrape_terabox(url: str) -> dict | None:
    """
    Proper TeraBox share-link scraper:
    1. Visit share page → get cookies + tokens
    2. Call shorturlinfo API → get file metadata
    3. Call download API → get direct link
    """
    surl = _surl(url)
    if not surl:
        logger.debug("No surl extracted")
        return None

    # Step 1: Visit the share page to get session cookies
    share_url = f"https://www.terabox.com/sharing/link?surl={surl}"
    try:
        r0 = S.get(share_url, timeout=20, allow_redirects=True)
        # Extract jsToken from page HTML
        js_token = ""
        m = re.search(r'window\.jsToken\s*=\s*["\']([^"\']+)', r0.text)
        if m:
            js_token = m.group(1)
        # Also try to get logid
        logid = ""
        m2 = re.search(r'fn\("([a-f0-9]+)"\)', r0.text)
        if m2:
            logid = m2.group(1)
    except Exception as e:
        logger.debug(f"Share page fetch failed: {e}")
        js_token = ""

    # Step 2: Try multiple API domains
    apis = [
        "https://www.terabox.com",
        "https://www.1024terabox.com",
        "https://terabox.com",
    ]

    for base_api in apis:
        try:
            params = {
                "app_id": "250528",
                "shorturl": surl,
                "root": "1",
            }
            if js_token:
                params["jsToken"] = js_token

            r1 = S.get(
                f"{base_api}/api/shorturlinfo",
                params=params,
                headers={
                    "Referer": share_url,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=20,
            )
            d1 = r1.json()
            logger.debug(f"shorturlinfo ({base_api}): errno={d1.get('errno')}")

            if d1.get("errno") != 0:
                continue

            file_list = d1.get("list", [])
            if not file_list:
                continue

            fi    = file_list[0]
            fs_id = str(fi.get("fs_id", ""))
            fname = fi.get("server_filename", "video.mp4")
            fsize = fi.get("size", 0)
            uk    = str(d1.get("uk", ""))
            sid   = str(d1.get("shareid", ""))
            sign  = d1.get("sign", "")
            ts    = str(d1.get("timestamp", int(time.time())))

            if not fs_id:
                continue

            # Step 3: Get download link
            dl_params = {
                "app_id": "250528",
                "sign": sign,
                "timestamp": ts,
                "shareid": sid,
                "uk": uk,
                "product": "share",
                "nozip": "1",
                "fid_list": f"[{fs_id}]",
            }
            r2 = S.get(
                f"{base_api}/api/download",
                params=dl_params,
                headers={"Referer": share_url},
                timeout=20,
            )
            d2 = r2.json()
            logger.debug(f"download api: errno={d2.get('errno')}")

            if d2.get("errno") != 0:
                continue

            dlinks = d2.get("dlink", [])
            if not dlinks:
                continue

            dlink = dlinks[0].get("dlink", "")
            if not dlink:
                continue

            logger.info(f"TeraBox API resolved: {fname} ({format_size(fsize)})")
            return {"filename": fname, "url": dlink, "size": fsize,
                    "referer": share_url}

        except Exception as e:
            logger.debug(f"TeraBox API {base_api} failed: {e}")
            continue

    return None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 3: 1024tera.com API (alternate endpoint)
# ─────────────────────────────────────────────────────────────────────────────
def _api_1024tera(url: str) -> dict | None:
    surl = _surl(url)
    if not surl:
        return None
    try:
        # 1024tera uses a slightly different API path
        r = S.get(
            "https://www.1024tera.com/api/shorturlinfo",
            params={"app_id": "250528", "shorturl": surl, "root": "1"},
            timeout=20,
        )
        d = r.json()
        if d.get("errno") != 0:
            return None
        fl = d.get("list", [])
        if not fl:
            return None
        fi    = fl[0]
        fs_id = str(fi.get("fs_id", ""))
        fname = fi.get("server_filename", "video.mp4")
        fsize = fi.get("size", 0)
        uk    = str(d.get("uk", ""))
        sid   = str(d.get("shareid", ""))
        sign  = d.get("sign", "")
        ts    = str(d.get("timestamp", int(time.time())))

        r2 = S.get(
            "https://www.1024tera.com/api/download",
            params={
                "app_id": "250528", "sign": sign, "timestamp": ts,
                "shareid": sid, "uk": uk, "product": "share",
                "nozip": "1", "fid_list": f"[{fs_id}]",
            },
            timeout=20,
        )
        d2 = r2.json()
        dlinks = d2.get("dlink", [])
        if dlinks and d2.get("errno") == 0:
            return {"filename": fname, "url": dlinks[0].get("dlink",""),
                    "size": fsize, "referer": url}
    except Exception as e:
        logger.debug(f"1024tera API failed: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 4: Third-party proxy APIs
# ─────────────────────────────────────────────────────────────────────────────
def _api_proxies(url: str) -> dict | None:
    encoded = quote(url, safe="")
    endpoints = [
        f"https://teraboxvideodownloader.nepcoderdevs.workers.dev/?url={encoded}",
        f"https://terabox.udayscript.com/api?url={encoded}",
        f"https://ytdl.udayscript.com/terabox?url={encoded}",
        f"https://terabox-dl-api.vercel.app/api?url={encoded}",
    ]
    for ep in endpoints:
        try:
            r = S.get(ep, timeout=25)
            if r.status_code != 200:
                continue
            d = r.json()
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
            sz = int(d.get("size") or d.get("file_size") or
                     (d.get("data") or {}).get("size") or 0)
            if dl:
                logger.info(f"Proxy resolved via: {ep[:50]}")
                return {"filename": fn, "url": dl, "size": sz, "referer": url}
        except Exception as e:
            logger.debug(f"Proxy {ep[:50]} failed: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 5: yt-dlp get-url only, then stream manually
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_get_url(url: str) -> dict | None:
    if not _ytdlp_ok():
        return None
    try:
        r = subprocess.run(
            ["yt-dlp", "--get-url", "--no-playlist", "--quiet",
             "--user-agent", "Mozilla/5.0 (Android 9; Mobile) Chrome/124.0", url],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            dl_url = r.stdout.strip().split("\n")[0]
            # Also get title
            r2 = subprocess.run(
                ["yt-dlp", "--get-title", "--no-playlist", "--quiet", url],
                capture_output=True, text=True, timeout=15,
            )
            title = r2.stdout.strip() if r2.returncode == 0 else "video"
            fname = _safe(title) + ".mp4"
            return {"filename": fname, "url": dl_url, "size": 0, "referer": url}
    except Exception as e:
        logger.debug(f"yt-dlp get-url failed: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  HTTP STREAM DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────
def _stream(info: dict, dest: str, prog=None) -> bool:
    url  = info["url"]
    ref  = info.get("referer", "https://www.terabox.com/")
    hdrs = {
        "Referer": ref,
        "Accept": "*/*",
        "Range": "bytes=0-",
    }
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            with S.get(url, stream=True, timeout=(15, 180), headers=hdrs) as r:
                # Follow redirects (TeraBox CDN redirects)
                if r.status_code in (301, 302, 303, 307, 308):
                    url = r.headers.get("Location", url)
                    continue
                if r.status_code not in (200, 206):
                    logger.warning(f"HTTP {r.status_code} on attempt {attempt}")
                    time.sleep(2 * attempt)
                    continue

                total = int(r.headers.get("content-length", 0))
                recv  = 0
                last  = -1

                if total and total > MAX_BYTES:
                    logger.warning(f"File too large: {total} bytes")
                    return False

                with open(dest, "wb") as f:
                    for blk in r.iter_content(chunk_size=CHUNK):
                        if blk:
                            f.write(blk)
                            recv += len(blk)
                            if recv > MAX_BYTES:
                                logger.warning("Size limit exceeded mid-download")
                                return False
                            if prog and total:
                                pct = min(int(recv * 100 / total), 99)
                                if pct != last and pct % 10 == 0:
                                    prog(pct, recv, total)
                                    last = pct

            sz = os.path.getsize(dest)
            if sz < 1000:
                logger.warning(f"Downloaded file too small: {sz} bytes")
                os.remove(dest)
                return False

            if prog:
                prog(100, sz, sz)
            logger.info(f"Streamed {format_size(sz)} to {os.path.basename(dest)}")
            return True

        except Exception as e:
            logger.warning(f"Stream attempt {attempt} failed: {e}")
            if os.path.exists(dest):
                os.remove(dest)
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(3 * attempt)

    return False

# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC INTERFACE
# ─────────────────────────────────────────────────────────────────────────────
def download_video(url: str, user_id: int, prog=None) -> dict | None:
    user_dir = os.path.join(DOWNLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    # Clean old partial files
    for f in os.listdir(user_dir):
        fp = os.path.join(user_dir, f)
        if os.path.isfile(fp) and not f.endswith(".gz"):
            try: os.remove(fp)
            except OSError: pass

    out_path = os.path.join(user_dir, "video.mp4")

    # ── Method 1: yt-dlp direct download ─────────────────────────
    logger.info(f"[M1] yt-dlp direct: {url}")
    if _dl_ytdlp(url, out_path, prog):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            orig = os.path.getsize(out_path)
            # Try to get real filename from yt-dlp
            fname = _get_ytdlp_title(url) + ".mp4"
            final = os.path.join(user_dir, _safe(fname))
            shutil.move(out_path, final)
            return _finish(final, _safe(fname), orig)

    # ── Method 2: TeraBox session API ────────────────────────────
    logger.info(f"[M2] TeraBox session API: {url}")
    info = _scrape_terabox(url)
    if info and info.get("url"):
        fname = _safe(info["filename"])
        if not fname.lower().endswith((".mp4",".mkv",".webm",".mov",".avi")):
            fname += ".mp4"
        dest = os.path.join(user_dir, fname)
        if info.get("size") and info["size"] > MAX_BYTES:
            return {"error": "too_large", "size": info["size"]}
        if _stream(info, dest, prog):
            return _finish(dest, fname, os.path.getsize(dest))

    # ── Method 3: 1024tera API ────────────────────────────────────
    logger.info(f"[M3] 1024tera API: {url}")
    info = _api_1024tera(url)
    if info and info.get("url"):
        fname = _safe(info["filename"])
        if not fname.lower().endswith((".mp4",".mkv",".webm",".mov",".avi")):
            fname += ".mp4"
        dest = os.path.join(user_dir, fname)
        if info.get("size") and info["size"] > MAX_BYTES:
            return {"error": "too_large", "size": info["size"]}
        if _stream(info, dest, prog):
            return _finish(dest, fname, os.path.getsize(dest))

    # ── Method 4: Proxy APIs ──────────────────────────────────────
    logger.info(f"[M4] Proxy APIs: {url}")
    info = _api_proxies(url)
    if info and info.get("url"):
        fname = _safe(info["filename"])
        if not fname.lower().endswith((".mp4",".mkv",".webm",".mov",".avi")):
            fname += ".mp4"
        dest = os.path.join(user_dir, fname)
        if _stream(info, dest, prog):
            return _finish(dest, fname, os.path.getsize(dest))

    # ── Method 5: yt-dlp get-url then stream ─────────────────────
    logger.info(f"[M5] yt-dlp get-url + stream: {url}")
    info = _ytdlp_get_url(url)
    if info and info.get("url"):
        fname = _safe(info["filename"])
        dest  = os.path.join(user_dir, fname)
        if _stream(info, dest, prog):
            return _finish(dest, fname, os.path.getsize(dest))

    logger.error(f"All 5 methods failed for: {url}")
    return None

def _finish(raw_path: str, filename: str, orig_size: int) -> dict:
    gz_path = raw_path + ".gz"
    try:
        with open(raw_path, "rb") as fi, \
             gzip.open(gz_path, "wb", compresslevel=1) as fo:
            shutil.copyfileobj(fi, fo, length=2 * 1024 * 1024)
        os.remove(raw_path)
        comp_size = os.path.getsize(gz_path)
        saving = (1 - comp_size/orig_size)*100 if orig_size else 0
        logger.info(f"Compressed: {format_size(orig_size)} → {format_size(comp_size)} ({saving:.0f}% saved)")
    except Exception as e:
        logger.warning(f"Compression failed ({e}), using raw")
        gz_path   = raw_path
        comp_size = orig_size
    return {
        "filename":        filename,
        "compressed_path": gz_path,
        "original_size":   orig_size,
        "compressed_size": comp_size,
    }

def decompress_file(gz: str) -> str:
    if not gz.endswith(".gz"):
        return gz
    out = gz[:-3]
    with gzip.open(gz, "rb") as fi, open(out, "wb") as fo:
        shutil.copyfileobj(fi, fo, length=2 * 1024 * 1024)
    return out

def cleanup_user_dir(user_id: int):
    shutil.rmtree(os.path.join(DOWNLOADS_DIR, str(user_id)), ignore_errors=True)

def _safe(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name).strip(". ")
    return name[:180] or "video.mp4"

def format_size(b: int) -> str:
    if not b or b <= 0:
        return "? MB"
    for u in ("B","KB","MB","GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def _get_ytdlp_title(url: str) -> str:
    try:
        r = subprocess.run(
            ["yt-dlp", "--get-title", "--no-playlist", "--quiet", url],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()[:80]
    except Exception:
        pass
    return "video"

def install_ytdlp() -> bool:
    if _ytdlp_ok():
        logger.info("yt-dlp ready ✓")
        return True
    logger.info(
