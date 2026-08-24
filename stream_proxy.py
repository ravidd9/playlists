from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import os
import urllib.parse
import requests
import re
import time
import threading
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PORT = int(os.environ.get("PORT", 5000))
CACHE_TTL = 1800  # Cache stream manifest token URL for 30 minutes

CHANNELS = {
    "yes-1": ("Yes 1", "Israel"),
    "yes-2": ("Yes 2", "Israel"),
    "yes-3": ("Yes 3", "Israel"),
    "yes-4": ("Yes 4", "Israel"),
    "yes-5": ("Yes 5", "Israel"),
    "kan-11": ("Kan 11", "Israel"),
    "One-1": ("ONE HD", "Sports"),
    "one-2": ("ONE 2 HD", "Sports"),
}

STREAM_CACHE = {}  # ch_id -> (stream_url, timestamp)
PREFETCHED_MANIFESTS = {}  # ch_id -> (manifest_text, timestamp)
ACTIVE_REQUESTS = {}  # ch_id -> timestamp of last request
LOCK = threading.Lock()

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://1nextbet7.tv/",
    "Origin": "https://1nextbet7.tv"
})

def get_stream_url(ch_id, force_fresh=False):
    now = time.time()
    if not force_fresh and ch_id in STREAM_CACHE:
        cached_url, ts = STREAM_CACHE[ch_id]
        if now - ts < CACHE_TTL:
            return cached_url

    page_url = f"https://1nextbet7.tv/kanal-izle/{ch_id}"
    try:
        res = session.get(page_url, timeout=4, verify=False)
        sources = re.findall(r'<source[^>]+src=["\']([^"\']+)', res.text, re.IGNORECASE)
        for s in sources:
            if any(ext in s.lower() for ext in [".css", ".m3u8", "mono"]):
                STREAM_CACHE[ch_id] = (s, now)
                return s
    except Exception:
        pass
    return None

def fetch_manifest_for_channel(ch_id):
    """Fetch and parse upstream M3U8 manifest, returning formatted text."""
    stream_url = get_stream_url(ch_id)
    if not stream_url:
        return None

    try:
        m3u8_res = session.get(stream_url, timeout=4, verify=False)
        if not m3u8_res.ok:
            stream_url = get_stream_url(ch_id, force_fresh=True)
            if stream_url:
                m3u8_res = session.get(stream_url, timeout=4, verify=False)

        if not m3u8_res.ok:
            return None

        lines = m3u8_res.text.splitlines()
        new_lines = []
        for line in lines:
            line_str = line.strip()
            if line_str.startswith("http"):
                new_lines.append(f"{line_str}#.ts")
            else:
                new_lines.append(line)

        return "\n".join(new_lines)
    except Exception:
        return None

def background_prefetch_worker():
    """Background thread that continuously pre-fetches manifests every 3s for active channels."""
    while True:
        try:
            now = time.time()
            with LOCK:
                # Active if player requested it within the last 60 seconds
                active_ids = [ch for ch, last_ts in ACTIVE_REQUESTS.items() if now - last_ts < 60]

            for ch_id in active_ids:
                manifest = fetch_manifest_for_channel(ch_id)
                if manifest:
                    with LOCK:
                        PREFETCHED_MANIFESTS[ch_id] = (manifest, now)

        except Exception:
            pass

        time.sleep(3)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class HLSProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default log clutter

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        proto = self.headers.get("X-Forwarded-Proto", "http")

        # 1. Serve M3U Playlist
        if path in ["/playlist.m3u", "/sports.m3u", "/"]:
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            host = self.headers.get("Host", f"127.0.0.1:{PORT}")
            m3u = ["#EXTM3U"]
            for ch_id, (ch_name, group) in CHANNELS.items():
                m3u.append(f'#EXTINF:-1 group-title="{group}",{ch_name}')
                m3u.append(f'{proto}://{host}/live/{ch_id}.m3u8')
            
            self.wfile.write("\n".join(m3u).encode("utf-8"))
            return

        # 2. Serve M3U8 Manifest instantly from RAM (0ms latency)
        if path.startswith("/live/") and path.endswith(".m3u8"):
            ch_id = path.replace("/live/", "").replace(".m3u8", "")
            if ch_id not in CHANNELS:
                self.send_error(404, "Channel Not Found")
                return

            now = time.time()
            with LOCK:
                ACTIVE_REQUESTS[ch_id] = now
                cached_manifest, ts = PREFETCHED_MANIFESTS.get(ch_id, (None, 0))

            # If pre-fetched manifest is fresh (< 6 seconds old), serve instantly from RAM
            if cached_manifest and (now - ts < 6):
                manifest_text = cached_manifest
            else:
                # Fetch synchronously if not in RAM yet
                manifest_text = fetch_manifest_for_channel(ch_id)
                if manifest_text:
                    with LOCK:
                        PREFETCHED_MANIFESTS[ch_id] = (manifest_text, now)

            if not manifest_text:
                self.send_error(502, "Stream Manifest Unavailable")
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(manifest_text.encode("utf-8"))
            return

        self.send_error(404)

def main():
    # Start background pre-fetcher thread
    worker = threading.Thread(target=background_prefetch_worker, daemon=True)
    worker.start()

    server = ThreadedHTTPServer(("0.0.0.0", PORT), HLSProxyHandler)
    print(f"============================================================")
    print(f" Zero-Latency HLS Stream Proxy running on port {PORT}")
    print(f"============================================================")
    server.serve_forever()

if __name__ == "__main__":
    main()
