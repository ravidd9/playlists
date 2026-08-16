from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import urllib.parse
import requests
import re
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PORT = int(os.environ.get("PORT", 5000))

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://1nextbet7.tv/",
    "Origin": "https://1nextbet7.tv"
}

class HLSProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default log output

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

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

        # 2. Serve M3U8 Manifest with rewritten segment URLs
        if path.startswith("/live/") and path.endswith(".m3u8"):
            ch_id = path.replace("/live/", "").replace(".m3u8", "")
            if ch_id not in CHANNELS:
                self.send_error(404, "Channel Not Found")
                return

            page_url = f"https://1nextbet7.tv/kanal-izle/{ch_id}"
            try:
                res = requests.get(page_url, headers=HEADERS, timeout=5, verify=False)
                sources = re.findall(r'<source[^>]+src=["\']([^"\']+)', res.text, re.IGNORECASE)
                stream_url = None
                for s in sources:
                    if any(ext in s.lower() for ext in [".css", ".m3u8", "mono"]):
                        stream_url = s
                        break

                if not stream_url:
                    self.send_error(502, "Stream URL Not Found")
                    return

                m3u8_res = requests.get(stream_url, headers=HEADERS, timeout=5, verify=False)
                if not m3u8_res.ok:
                    self.send_error(502, f"Upstream error {m3u8_res.status_code}")
                    return

                host = self.headers.get("Host", f"127.0.0.1:{PORT}")
                lines = m3u8_res.text.splitlines()
                new_lines = []

                for line in lines:
                    line_str = line.strip()
                    if line_str.startswith("http"):
                        # Rewrite segment URLs to route through proxy with video/mp2t (.ts) content
                        encoded_url = urllib.parse.quote(line_str)
                        new_lines.append(f"{proto}://{host}/segment?url={encoded_url}&ext=.ts")
                    else:
                        new_lines.append(line)

                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write("\n".join(new_lines).encode("utf-8"))
                return

            except Exception as e:
                self.send_error(500, str(e))
                return

        # 3. Serve Video TS Segment Stream
        if path == "/segment":
            target_url = query.get("url", [None])[0]
            if not target_url:
                self.send_error(400, "Missing url parameter")
                return

            try:
                with requests.get(target_url, headers=HEADERS, timeout=10, stream=True, verify=False) as seg_res:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    for chunk in seg_res.iter_content(chunk_size=65536):
                        if chunk:
                            self.wfile.write(chunk)
                return
            except Exception as e:
                self.send_error(500, str(e))
                return

        self.send_error(404)

def main():
    server = HTTPServer(("0.0.0.0", PORT), HLSProxyHandler)
    print(f"============================================================")
    print(f" HLS Stream Proxy running on port {PORT}")
    print(f"============================================================")
    server.serve_forever()

if __name__ == "__main__":
    main()
