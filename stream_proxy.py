from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import os
import urllib.parse
import requests
import re
import time
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
        res = session.get(page_url, timeout=5, verify=False)
        sources = re.findall(r'<source[^>]+src=["\']([^"\']+)', res.text, re.IGNORECASE)
        for s in sources:
            if any(ext in s.lower() for ext in [".css", ".m3u8", "mono"]):
                STREAM_CACHE[ch_id] = (s, now)
                return s
    except Exception:
        pass
    return None

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

        # 2. Serve M3U8 Manifest with direct segment URLs appended with #.ts
        if path.startswith("/live/") and path.endswith(".m3u8"):
            ch_id = path.replace("/live/", "").replace(".m3u8", "")
            if ch_id not in CHANNELS:
                self.send_error(404, "Channel Not Found")
                return

            stream_url = get_stream_url(ch_id)
            if not stream_url:
                self.send_error(502, "Stream URL Not Found")
                return

            try:
                m3u8_res = session.get(stream_url, timeout=5, verify=False)
                if not m3u8_res.ok:
                    stream_url = get_stream_url(ch_id, force_fresh=True)
                    if stream_url:
                        m3u8_res = session.get(stream_url, timeout=5, verify=False)

                if not m3u8_res.ok:
                    self.send_error(502, f"Upstream error {m3u8_res.status_code}")
                    return

                lines = m3u8_res.text.splitlines()
                new_lines = []

                for line in lines:
                    line_str = line.strip()
                    if line_str.startswith("http"):
                        # Direct video segment URL with #.ts anchor so media player treats chunk as MPEG-TS
                        new_lines.append(f"{line_str}#.ts")
                    else:
                        new_lines.append(line)

                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write("\n".join(new_lines).encode("utf-8"))
                return

            except Exception as e:
                self.send_error(500, str(e))
                return

        self.send_error(404)

def main():
    server = ThreadedHTTPServer(("0.0.0.0", PORT), HLSProxyHandler)
    print(f"============================================================")
    print(f" Zero-Bandwidth HLS Stream Proxy running on port {PORT}")
    print(f"============================================================")
    server.serve_forever()

if __name__ == "__main__":
    main()
