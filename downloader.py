"""
downloader.py — TeraBox Video Downloader
Robust 6-method pipeline with full debug logging.
"""
import os, re, time, gzip, json, shutil, logging, subprocess
import requests
from urllib.parse import urlparse, parse_qs, quote
from config import DOWNLOADS_DIR, MAX_FILE_SIZE_MB, CHUNK_SIZE_KB, DOWNLOAD_RETRIES

logger = logging.getLogger(__name__)

MAX_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
CHUNK     = CHUNK_SIZE_KB * 1024

# ── Shared session ────────────────────────────────────────────────────────────
S = requests.Session()
S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
})
_retry = requests.adapters.Retry(
    total=3, backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET","POST"],
)
S.mount("https://", requests.adapters.HTTPAdapter(max_retries=_retry))
S.mount("http://",  requests.adapters.HTTPAdapter(max_retries=_retry))

# ── Domains ───────────────────────────────────────────────────────────────────
TB_DOMAINS = {
    "terabox.com", "1024terabox.com", "teraboxapp.com",
    "4funbox.com", "mirrorbox.com", "momerybox.com",
    "nephobox.com", "freeterabox.com", "terabox.fun",
    "tibibox.com", "teraboxlink.com", "1024tera.com",
    "terafileshare.com", "terasharelink.com", "teraboxvideo.com",
}

def is_terabox_url(text: str) -> str | None:
    text = text.strip()
    m = re.search(r"https?://[^\s\"'<>\]\)]+", text)
    if m:
        text = m.group(0)
    parsed = urlparse(text)
    host = parsed.netloc.lower().lstrip("www.")
    if any(host == d or host.endswith("."+d) for d in TB_DOMAINS):
        return text
    return None

# ── Extract surl from any TeraBox URL ─────────────────────────────────────────
def _surl(url: str) -> str | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs:
        return qs["surl"][0]
    m = re.search(r"/s/([A-Za-z0-9_\-]+)", parsed.path)
    return m.group(1) if m else None

# ─────────────────────────────────────────────────────────────────────────────
#  DEBUG INFO — returned with every attempt for admin /debug command
# ─────────────────────────────────────────────────────────────────────────────
class DebugLog:
    def __init__(self):
        self.steps: list[str] = []
    def add(self, msg: str):
        logger.info(msg)
        self.steps.append(msg)
    def err(self, msg: str):
        logger.warning(msg)
        self.steps.append(f"✗ {msg}")
    def ok(self, msg: str):
        logger.info(msg)
        self.steps.append(f"✓ {msg}")
    def summary(self) -> str:
        return "\n".join(self.steps[-30:])  # last 30 lines

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 1: yt-dlp  (direct download — most reliable when installed)
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_bin() -> str | None:
    for cmd in ("yt-dlp", f"{os.environ.get('HOME','')}/bin/yt-dlp",
                "/data/data/com.termux/files/usr/bin/yt-dlp"):
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return cmd
        except Exception:
            pass
    return None

def _m1_ytdlp_direct(url: str, out: str, dbg: DebugLog, prog=None) -> bool:
    bin_ = _ytdlp_bin()
    if not bin_:
        dbg.err("M1: yt-dlp not found in PATH")
        return False
    ver = subprocess.run([bin_,"--version"],capture_output=True,text=True).stdout.strip()
    dbg.add(f"M1: yt-dlp {ver}")
    base = out.rsplit(".",1)[0] if "." in os.path.basename(out) else out
    tmpl = base + ".%(ext)s"
    cmd  = [
        bin_,
        "--no-playlist", "--no-warnings",
        "--merge-output-format", "mp4",
        "--output", tmpl,
        "--max-filesize", f"{MAX_FILE_SIZE_MB}m",
        "--retries", "5",
        "--fragment-retries", "5",
        "--socket-timeout", "30",
        "--extractor-retries", "3",
        "--user-agent", S.headers["User-Agent"],
        url,
    ]
    dbg.add(f"M1: running yt-dlp…")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        dbg.add(f"M1: exit={proc.returncode}")
        if proc.stderr:
            dbg.add(f"M1 stderr: {proc.stderr[:300]}")
        if proc.returncode == 0:
            for ext in ("mp4","mkv","webm","m4v","avi"):
                c = base + "." + ext
                if os.path.exists(c) and os.path.getsize(c) > 1000:
                    if c != out:
                        shutil.move(c, out)
                    sz = os.path.getsize(out)
                    dbg.ok(f"M1: downloaded {format_size(sz)}")
                    if prog: prog(100, sz, sz)
                    return True
        dbg.err(f"M1: no output file found after yt-dlp")
    except subprocess.TimeoutExpired:
        dbg.err("M1: yt-dlp timed out (300s)")
    except Exception as e:
        dbg.err(f"M1 exception: {e}")
    return False

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 2: Page scrape — extract locals.mset / window.__data
# ─────────────────────────────────────────────────────────────────────────────
def _m2_page_scrape(url: str, dbg: DebugLog) -> dict | None:
    """
    TeraBox embeds file metadata in page JS as:
      locals.mset({...})  OR  window.__initialState = {...}  OR  renderContent({...})
    Extract that and build download URL directly.
    """
    dbg.add(f"M2: fetching share page: {url}")
    try:
        r = S.get(url, timeout=25, allow_redirects=True)
        dbg.add(f"M2: page status={r.status_code} final_url={r.url[:80]}")
        if r.status_code != 200:
            dbg.err(f"M2: bad status {r.status_code}")
            return None
    except Exception as e:
        dbg.err(f"M2: page fetch failed: {e}")
        return None

    html   = r.text
    S.cookies.update(r.cookies)  # save cookies for API calls

    # Pattern 1: locals.mset({...}) — classic TeraBox
    m = re.search(r'locals\.mset\s*\(\s*(\{.+?\})\s*\)', html, re.DOTALL)
    if m:
        try:
            data  = json.loads(m.group(1))
            result = _parse_locals_mset(data, dbg)
            if result:
                return result
        except Exception as e:
            dbg.err(f"M2: locals.mset parse error: {e}")

    # Pattern 2: __initialState
    m = re.search(r'window\.__initialState\s*=\s*(\{.+?\});', html, re.DOTALL)
    if m:
        try:
            data   = json.loads(m.group(1))
            result = _parse_initial_state(data, dbg)
            if result:
                return result
        except Exception as e:
            dbg.err(f"M2: __initialState parse error: {e}")

    # Pattern 3: renderContent or pageData
    for pat in [
        r'"list"\s*:\s*(\[.+?\])',
        r'"fileList"\s*:\s*(\[.+?\])',
    ]:
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                fl = json.loads(m.group(1))
                if fl:
                    fi = fl[0]
                    uk     = re.search(r'"uk"\s*:\s*"?(\d+)"?', html)
                    sid    = re.search(r'"shareid"\s*:\s*"?(\d+)"?', html)
                    sign   = re.search(r'"sign"\s*:\s*"([^"]+)"', html)
                    ts     = re.search(r'"timestamp"\s*:\s*"?(\d+)"?', html)
                    fs_id  = fi.get("fs_id") or fi.get("fsid")
                    fname  = fi.get("server_filename","video.mp4")
                    fsize  = fi.get("size", 0)
                    if fs_id and uk and sid and sign:
                        dbg.ok(f"M2: found via pattern fileList: {fname}")
                        return {
                            "fs_id": str(fs_id),
                            "filename": fname, "size": fsize,
                            "uk": uk.group(1), "shareid": sid.group(1),
                            "sign": sign.group(1),
                            "timestamp": ts.group(1) if ts else str(int(time.time())),
                            "page_url": r.url,
                        }
            except Exception as e:
                dbg.err(f"M2: fileList pattern error: {e}")

    # Pattern 4: Extract from meta/og tags for filename at least
    title_m = re.search(r'<title>([^<]+)</title>', html)
    if title_m:
        dbg.add(f"M2: page title: {title_m.group(1)[:80]}")

    dbg.err(f"M2: could not extract file data from page HTML (len={len(html)})")
    # Save snippet for debug
    dbg.add(f"M2 HTML snippet: {html[200:600]}")
    return None

def _parse_locals_mset(data: dict, dbg: DebugLog) -> dict | None:
    share = data.get("share") or data
    fl    = share.get("list") or share.get("fileList") or []
    if not fl:
        dbg.err("M2: locals.mset: empty list")
        return None
    fi     = fl[0]
    fs_id  = str(fi.get("fs_id") or fi.get("fsid",""))
    fname  = fi.get("server_filename","video.mp4")
    fsize  = fi.get("size",0)
    uk     = str(share.get("uk",""))
    sid    = str(share.get("shareid",""))
    sign   = share.get("sign","")
    ts     = str(share.get("timestamp",int(time.time())))
    if not fs_id or not uk:
        dbg.err(f"M2: missing fs_id={fs_id} uk={uk}")
        return None
    dbg.ok(f"M2: locals.mset → {fname} ({format_size(fsize)})")
    return {"fs_id":fs_id,"filename":fname,"size":fsize,
            "uk":uk,"shareid":sid,"sign":sign,"timestamp":ts}

def _parse_initial_state(data: dict, dbg: DebugLog) -> dict | None:
    try:
        share = (data.get("share") or data.get("shareInfo") or
                 data.get("props",{}).get("pageProps",{}).get("share",{}))
        fl    = share.get("list") or share.get("fileList") or []
        if not fl:
            dbg.err("M2: __initialState: empty list")
            return None
        fi    = fl[0]
        fs_id = str(fi.get("fs_id") or fi.get("fsid",""))
        fname = fi.get("server_filename","video.mp4")
        fsize = fi.get("size",0)
        uk    = str(share.get("uk",""))
        sid   = str(share.get("shareid",""))
        sign  = share.get("sign","")
        ts    = str(share.get("timestamp",int(time.time())))
        if not fs_id:
            return None
        dbg.ok(f"M2: __initialState → {fname}")
        return {"fs_id":fs_id,"filename":fname,"size":fsize,
                "uk":uk,"shareid":sid,"sign":sign,"timestamp":ts}
    except Exception as e:
        dbg.err(f"M2: __initialState parse: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 3: TeraBox official API (with cookies from M2)
# ─────────────────────────────────────────────────────────────────────────────
def _m3_official_api(url: str, dbg: DebugLog) -> dict | None:
    surl = _surl(url)
    if not surl:
        dbg.err("M3: no surl"); return None
    dbg.add(f"M3: official API, surl={surl}")

    # First visit page to get cookies
    for domain in ("www.terabox.com","www.1024terabox.com","www.1024tera.com"):
        share_page = f"https://{domain}/sharing/link?surl={surl}"
        try:
            p = S.get(share_page, timeout=20, allow_redirects=True)
            dbg.add(f"M3: page {domain} → {p.status_code}")
            S.cookies.update(p.cookies)
        except Exception as e:
            dbg.add(f"M3: page fetch {domain} failed: {e}")

    for api_base in ("https://www.terabox.com","https://www.1024terabox.com","https://www.1024tera.com"):
        try:
            dbg.add(f"M3: shorturlinfo @ {api_base}")
            r1 = S.get(
                f"{api_base}/api/shorturlinfo",
                params={"app_id":"250528","shorturl":surl,"root":"1"},
                headers={"Referer": f"{api_base}/sharing/link?surl={surl}"},
                timeout=20,
            )
            dbg.add(f"M3: status={r1.status_code}")
            d1 = r1.json()
            dbg.add(f"M3: errno={d1.get('errno')} errmsg={d1.get('errmsg','')}")

            if d1.get("errno") != 0:
                continue

            fl = d1.get("list",[])
            if not fl: continue

            fi    = fl[0]
            fs_id = str(fi.get("fs_id",""))
            fname = fi.get("server_filename","video.mp4")
            fsize = fi.get("size",0)
            uk    = str(d1.get("uk",""))
            sid   = str(d1.get("shareid",""))
            sign  = d1.get("sign","")
            ts    = str(d1.get("timestamp",int(time.time())))

            if not fs_id:
                dbg.err(f"M3: no fs_id"); continue

            # Get download link
            r2 = S.get(
                f"{api_base}/api/download",
                params={
                    "app_id":"250528","sign":sign,"timestamp":ts,
                    "shareid":sid,"uk":uk,"product":"share",
                    "nozip":"1","fid_list":f"[{fs_id}]",
                },
                headers={"Referer": f"{api_base}/sharing/link?surl={surl}"},
                timeout=20,
            )
            d2 = r2.json()
            dbg.add(f"M3: download errno={d2.get('errno')}")

            if d2.get("errno") != 0:
                dbg.err(f"M3: download API errno={d2.get('errno')} errmsg={d2.get('errmsg','')}")
                continue

            dlinks = d2.get("dlink",[])
            if not dlinks:
                dbg.err("M3: empty dlink"); continue

            dlink = dlinks[0].get("dlink","")
            if not dlink:
                dbg.err("M3: empty dlink url"); continue

            dbg.ok(f"M3: got download link for {fname} ({format_size(fsize)})")
            return {"filename":fname,"url":dlink,"size":fsize,
                    "referer": f"{api_base}/sharing/link?surl={surl}"}

        except Exception as e:
            dbg.err(f"M3: {api_base} exception: {e}")

    return None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 4: M2 page data → build download URL
# ─────────────────────────────────────────────────────────────────────────────
def _m4_page_then_api(url: str, dbg: DebugLog) -> dict | None:
    """Use page-scraped metadata to hit the download API."""
    page_data = _m2_page_scrape(url, dbg)
    if not page_data:
        return None
    if "url" in page_data:
        return page_data  # already has direct URL

    fs_id   = page_data.get("fs_id","")
    uk      = page_data.get("uk","")
    sid     = page_data.get("shareid","")
    sign    = page_data.get("sign","")
    ts      = page_data.get("timestamp",str(int(time.time())))
    fname   = page_data.get("filename","video.mp4")
    fsize   = page_data.get("size",0)
    page_url= page_data.get("page_url", url)

    if not fs_id or not sign:
        dbg.err(f"M4: missing data fs_id={fs_id} sign={bool(sign)}")
        return None

    for api_base in ("https://www.terabox.com","https://www.1024terabox.com","https://www.1024tera.com"):
        try:
            r = S.get(
                f"{api_base}/api/download",
                params={
                    "app_id":"250528","sign":sign,"timestamp":ts,
                    "shareid":sid,"uk":uk,"product":"share",
                    "nozip":"1","fid_list":f"[{fs_id}]",
                },
                headers={"Referer": page_url},
                timeout=20,
            )
            d = r.json()
            dbg.add(f"M4: download api {api_base} errno={d.get('errno')}")
            if d.get("errno") == 0:
                dlinks = d.get("dlink",[])
                if dlinks:
                    dlink = dlinks[0].get("dlink","")
                    if dlink:
                        dbg.ok(f"M4: got direct link for {fname}")
                        return {"filename":fname,"url":dlink,"size":fsize,
                                "referer": page_url}
        except Exception as e:
            dbg.err(f"M4: {api_base}: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 5: Third-party proxy APIs
# ─────────────────────────────────────────────────────────────────────────────
def _m5_proxies(url: str, dbg: DebugLog) -> dict | None:
    encoded = quote(url, safe="")
    endpoints = [
        f"https://teraboxvideodownloader.nepcoderdevs.workers.dev/?url={encoded}",
        f"https://terabox.udayscript.com/api?url={encoded}",
        f"https://ytdl.udayscript.com/terabox?url={encoded}",
        f"https://terabox-dl-api.vercel.app/api?url={encoded}",
        f"https://terabox-video-downloader.vercel.app/api?url={encoded}",
    ]
    for ep in endpoints:
        short = ep[:60]
        try:
            dbg.add(f"M5: trying {short}")
            r = S.get(ep, timeout=25)
            dbg.add(f"M5: {r.status_code}")
            if r.status_code != 200:
                continue
            d = r.json()
            dl = (d.get("download_url") or d.get("downloadUrl") or
                  d.get("dlink") or d.get("url") or
                  (d.get("data") or {}).get("download_url") or
                  (d.get("data") or {}).get("dlink") or
                  (d.get("data") or {}).get("url"))
            fn = (d.get("file_name") or d.get("filename") or
                  d.get("title") or d.get("name") or "video.mp4")
            sz = int(d.get("size") or d.get("file_size") or 0)
            if dl:
                dbg.ok(f"M5: got link from {short}")
                return {"filename":fn,"url":dl,"size":sz,"referer":url}
            dbg.err(f"M5: no dl url in response: {str(d)[:150]}")
        except Exception as e:
            dbg.err(f"M5: {short}: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  METHOD 6: yt-dlp get-url → stream
# ─────────────────────────────────────────────────────────────────────────────
def _m6_ytdlp_url(url: str, dbg: DebugLog) -> dict | None:
    bin_ = _ytdlp_bin()
    if not bin_:
        dbg.err("M6: yt-dlp not found"); return None
    dbg.add("M6: yt-dlp --get-url")
    try:
        r = subprocess.run(
            [bin_,"--get-url","--no-playlist","--quiet",
             "--user-agent", S.headers["User-Agent"], url],
            capture_output=True, text=True, timeout=30,
        )
        dbg.add(f"M6: exit={r.returncode}")
        if r.returncode == 0 and r.stdout.strip():
            dl_url = r.stdout.strip().split("\n")[0]
            # Get title
            rt = subprocess.run(
                [bin_,"--get-title","--no-playlist","--quiet",url],
                capture_output=True, text=True, timeout=15,
            )
            title = rt.stdout.strip() if rt.returncode==0 else "video"
            fname = _safe(title)+".mp4"
            dbg.ok(f"M6: got URL for {fname}")
            return {"filename":fname,"url":dl_url,"size":0,"referer":url}
        dbg.err(f"M6: stderr={r.stderr[:200]}")
    except Exception as e:
        dbg.err(f"M6: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  HTTP STREAM DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────
def _stream(info: dict, dest: str, dbg: DebugLog, prog=None) -> bool:
    url  = info["url"]
    ref  = info.get("referer","https://www.terabox.com/")
    hdrs = {
        "Referer": ref,
        "Accept": "*/*",
        "User-Agent": S.headers["User-Agent"],
    }
    dbg.add(f"Stream: {url[:80]}")
    for attempt in range(1, DOWNLOAD_RETRIES+1):
        try:
            with S.get(url, stream=True, timeout=(20,180), headers=hdrs,
                       allow_redirects=True) as r:
                dbg.add(f"Stream: HTTP {r.status_code} (attempt {attempt})")
                if r.status_code not in (200,206):
                    if attempt < DOWNLOAD_RETRIES:
                        time.sleep(3*attempt)
       
