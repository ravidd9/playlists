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
MAX_BUFFER_SEGMENTS = 12  # Keep 12 segments (120s of video buffer) in sliding window

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
SLIDING_BUFFERS = {}  # ch_id -> {"first_seq": int, "segments": [(inf_str, url, is_discontinuity)], "manifest": str, "last_update": float}
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
        valid_src = [s for s in sources if any(ext in s.lower() for ext in [".css", ".m3u8", "mono"])]
        if valid_src:
            STREAM_CACHE[ch_id] = (valid_src[0], now)
            return valid_src[0]
    except Exception:
        pass
    return None

def update_channel_manifest(ch_id):
    """Fetch upstream manifest and update sliding window buffer."""
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

        # Parse upstream media sequence
        seq_match = [l for l in lines if "#EXT-X-MEDIA-SEQUENCE" in l]
        upstream_seq = int(seq_match[0].split(":")[-1]) if seq_match else 0

        incoming_segments = []
        cur_inf = "10.000"
        has_discontinuity = False

        for line in lines:
            line_str = line.strip()
            if line_str.startswith("#EXT-X-DISCONTINUITY"):
                has_discontinuity = True
            elif line_str.startswith("#EXTINF"):
                cur_inf = line_str.split(":")[-1].split(",")[0]
            elif line_str.startswith("http"):
                seg_url = f"{line_str}#.ts"
                incoming_segments.append((cur_inf, seg_url, has_discontinuity))
                has_discontinuity = False

        with LOCK:
            if ch_id not in SLIDING_BUFFERS:
                SLIDING_BUFFERS[ch_id] = {
                    "first_seq": upstream_seq,
                    "segments": [],
                    "manifest": "",
                    "last_update": time.time()
                }

            buf = SLIDING_BUFFERS[ch_id]
            existing_urls = {url for dur, url, disc in buf["segments"]}

            for dur, url, disc in incoming_segments:
                if url not in existing_urls:
                    buf["segments"].append((dur, url, disc))

            # Maintain sliding window of MAX_BUFFER_SEGMENTS
            if len(buf["segments"]) > MAX_BUFFER_SEGMENTS:
                removed = len(buf["segments"]) - MAX_BUFFER_SEGMENTS
                buf["segments"] = buf["segments"][-MAX_BUFFER_SEGMENTS:]
                buf["first_seq"] += removed
            elif buf["first_seq"] == 0:
                buf["first_seq"] = upstream_seq

            # Generate robust M3U8 manifest with deep buffer
            out = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:10",
                "#EXT-X-ALLOW-CACHE:NO",
                f"#EXT-X-MEDIA-SEQUENCE:{buf['first_seq']}"
            ]
            for dur, url, disc in buf["segments"]:
                if disc:
                    out.append("#EXT-X-DISCONTINUITY")
                out.append(f"#EXTINF:{dur},")
                out.append(url)

            buf["manifest"] = "\n".join(out)
            buf["last_update"] = time.time()
            return buf["manifest"]

    except Exception:
        return None

def background_prefetch_worker():
    """Continuously poll upstream manifests every 2.5s to maintain 100-second deep buffer."""
    while True:
        try:
            now = time.time()
            with LOCK:
                active_ids = [ch for ch, last_ts in ACTIVE_REQUESTS.items() if now - last_ts < 120]

            for ch_id in active_ids:
                update_channel_manifest(ch_id)

        except Exception:
            pass

        time.sleep(2.5)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class HLSProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

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

        # 2. Serve M3U8 Manifest with deep buffer instantly from RAM
        if path.startswith("/live/") and path.endswith(".m3u8"):
            ch_id = path.replace("/live/", "").replace(".m3u8", "")
            if ch_id not in CHANNELS:
                self.send_error(404, "Channel Not Found")
                return

            now = time.time()
            with LOCK:
                ACTIVE_REQUESTS[ch_id] = now
                buf = SLIDING_BUFFERS.get(ch_id)
                manifest_text = buf["manifest"] if buf and buf["manifest"] and (now - buf["last_update"] < 10) else None

            if not manifest_text:
                manifest_text = update_channel_manifest(ch_id)

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
    worker = threading.Thread(target=background_prefetch_worker, daemon=True)
    worker.start()

    server = ThreadedHTTPServer(("0.0.0.0", PORT), HLSProxyHandler)
    print(f"============================================================")
    print(f" Deep-Buffer Zero-Lag HLS Proxy running on port {PORT}")
    print(f"============================================================")
    server.serve_forever()

if __name__ == "__main__":
    main()
