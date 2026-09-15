from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urljoin, urlparse
import math
import os
import re
import threading
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PORT = int(os.environ.get("PORT", 5000))
CACHE_TTL = 1800  # 30-minute token cache
# Keep a short live window. A 14-segment DVR (~80s+) makes TVmate periodically
# snap to live edge, which looks like a skip forward/back every 10-30s.
LIVE_WINDOW_SEGMENTS = 5
POLL_INTERVAL = 2.0  # must be well under the ~6s segment duration

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
SLIDING_BUFFERS = {}  # ch_id -> buffer dict
ACTIVE_REQUESTS = {}  # ch_id -> timestamp of last request
LOCK = threading.Lock()

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://1nextbet7.tv/",
    "Origin": "https://1nextbet7.tv",
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
        valid_src = [s for s in sources if any(ext in s.lower() for ext in [".css", ".m3u8", "mono"])]
        if valid_src:
            STREAM_CACHE[ch_id] = (valid_src[0], now)
            return valid_src[0]
    except Exception:
        pass
    return None


def _fetch_text(url):
    res = session.get(url, timeout=5, verify=False)
    if not res.ok:
        return None, None
    return url, res.text


def resolve_media_playlist(url):
    """Follow a master playlist to the highest-bandwidth media playlist."""
    resolved = _fetch_text(url)
    if not resolved[1]:
        return None, None

    current_url, text = resolved
    if "#EXT-X-STREAM-INF" not in text:
        return current_url, text

    variants = []
    bandwidth = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            match = re.search(r"BANDWIDTH=(\d+)", line)
            bandwidth = int(match.group(1)) if match else 0
        elif line and not line.startswith("#"):
            variants.append((bandwidth, urljoin(current_url, line)))

    if not variants:
        return current_url, text

    variants.sort()
    return _fetch_text(variants[-1][1])


def _parse_pdt(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_pdt(dt):
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _fill_pdts(segments):
    last = None
    for seg in segments:
        parsed = _parse_pdt(seg.get("pdt"))
        if parsed is not None:
            last = parsed
            continue
        if last is None:
            continue
        try:
            last = last + timedelta(seconds=float(seg["dur"]))
            seg["pdt"] = _format_pdt(last)
        except (TypeError, ValueError):
            last = None


def parse_media_playlist(text, base_url):
    target_duration = 6
    media_sequence = 0
    segments = []
    cur_inf = "6.000"
    discontinuity = False
    pending_pdt = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = int(float(line.split(":", 1)[1]))
            except ValueError:
                pass
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = line.split(":", 1)[1].strip()
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            discontinuity = True
        elif line.startswith("#EXTINF:"):
            cur_inf = line.split(":", 1)[1].split(",")[0].strip()
        elif not line.startswith("#"):
            segments.append({
                "dur": cur_inf,
                "url": urljoin(base_url, line),
                "disc": discontinuity,
                "pdt": pending_pdt,
            })
            discontinuity = False
            pending_pdt = None

    _fill_pdts(segments)
    return target_duration, media_sequence, segments


def _max_segment_duration(segments):
    max_dur = 0.0
    for seg in segments:
        try:
            max_dur = max(max_dur, float(seg["dur"]))
        except ValueError:
            continue
    return max_dur


def update_channel_manifest(ch_id):
    """Refresh the in-memory live window from upstream. Does not need the public host."""
    stream_url = get_stream_url(ch_id)
    if not stream_url:
        return False

    try:
        resolved_url, text = resolve_media_playlist(stream_url)
        if not text:
            stream_url = get_stream_url(ch_id, force_fresh=True)
            if not stream_url:
                return False
            resolved_url, text = resolve_media_playlist(stream_url)
        if not text:
            return False

        target_duration, upstream_seq, incoming = parse_media_playlist(text, resolved_url)
        if not incoming:
            return False

        incoming_urls = {seg["url"] for seg in incoming}
        needed_target = max(target_duration, int(math.ceil(_max_segment_duration(incoming) or 6)))

        with LOCK:
            buf = SLIDING_BUFFERS.get(ch_id)
            overlap = bool(buf and any(url in buf["seen_urls"] for url in incoming_urls))

            if not buf or not overlap:
                SLIDING_BUFFERS[ch_id] = {
                    "base_seq": upstream_seq,
                    "segments": list(incoming),
                    "seen_urls": set(incoming_urls),
                    "target_duration": needed_target,
                    "last_update": time.time(),
                }
                buf = SLIDING_BUFFERS[ch_id]
            else:
                for seg in incoming:
                    if seg["url"] not in buf["seen_urls"]:
                        buf["segments"].append(seg)
                        buf["seen_urls"].add(seg["url"])
                _fill_pdts(buf["segments"])

            if len(buf["segments"]) > LIVE_WINDOW_SEGMENTS:
                excess = len(buf["segments"]) - LIVE_WINDOW_SEGMENTS
                buf["base_seq"] += excess
                buf["segments"] = buf["segments"][-LIVE_WINDOW_SEGMENTS:]
                buf["seen_urls"] = {seg["url"] for seg in buf["segments"]}

            # HLS forbids TARGETDURATION from shrinking mid-stream.
            buf["target_duration"] = max(buf.get("target_duration", 6), needed_target)
            buf["last_update"] = time.time()
            return True

    except Exception:
        return False


def render_manifest(ch_id, proto, host):
    with LOCK:
        buf = SLIDING_BUFFERS.get(ch_id)
        if not buf or not buf.get("segments"):
            return None

        out = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{buf['target_duration']}",
            f"#EXT-X-MEDIA-SEQUENCE:{buf['base_seq']}",
        ]

        first_pdt = next((seg["pdt"] for seg in buf["segments"] if seg.get("pdt")), None)
        if first_pdt:
            out.append(f"#EXT-X-PROGRAM-DATE-TIME:{first_pdt}")

        for offset, seg in enumerate(buf["segments"]):
            if seg.get("disc") and offset > 0:
                out.append("#EXT-X-DISCONTINUITY")
            out.append(f"#EXTINF:{seg['dur']},")
            # Stable .ts path so TVmate does not treat upstream .jpeg names as images.
            out.append(f"{proto}://{host}/seg/{ch_id}/{buf['base_seq'] + offset}.ts")

        return "\n".join(out) + "\n"


def lookup_segment(ch_id, seq):
    with LOCK:
        buf = SLIDING_BUFFERS.get(ch_id)
        if not buf:
            return None
        offset = seq - buf["base_seq"]
        if offset < 0 or offset >= len(buf["segments"]):
            return None
        return buf["segments"][offset]["url"]


def background_prefetch_worker():
    """Polls upstream manifests for channels that a player has requested recently."""
    while True:
        try:
            now = time.time()
            with LOCK:
                active_ids = [ch for ch, last_ts in ACTIVE_REQUESTS.items() if now - last_ts < 180]

            for ch_id in active_ids:
                update_channel_manifest(ch_id)
        except Exception:
            pass

        time.sleep(POLL_INTERVAL)


def background_keepalive_worker():
    """Pings local server every 8 minutes to prevent Render free instance spin-down."""
    time.sleep(30)
    while True:
        try:
            requests.get(f"http://127.0.0.1:{PORT}/", timeout=5)
        except Exception:
            pass
        time.sleep(480)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class HLSProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _public_origin(self):
        proto = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("Host", f"127.0.0.1:{PORT}")
        return proto, host

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 1. Serve M3U Playlist & Health Check
        if path in ["/playlist.m3u", "/sports.m3u", "/", "/health"]:
            proto, host = self._public_origin()
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()

            m3u = ["#EXTM3U"]
            for ch_id, (ch_name, group) in CHANNELS.items():
                m3u.append(f'#EXTINF:-1 group-title="{group}",{ch_name}')
                m3u.append(f"{proto}://{host}/live/{ch_id}.m3u8")

            self.wfile.write("\n".join(m3u).encode("utf-8"))
            return

        # 2. Redirect .ts segment requests to the origin CDN (no bandwidth through this server)
        if path.startswith("/seg/"):
            parts = path.split("/")
            # /seg/{ch_id}/{seq}.ts -> ['', 'seg', '{ch_id}', '{seq}.ts']
            if len(parts) == 4 and parts[1] == "seg" and parts[3].endswith(".ts"):
                ch_id = parts[2]
                try:
                    seq = int(parts[3][:-3])
                except ValueError:
                    self.send_error(404, "Bad Segment")
                    return
                if ch_id not in CHANNELS:
                    self.send_error(404, "Channel Not Found")
                    return
                origin = lookup_segment(ch_id, seq)
                if not origin:
                    update_channel_manifest(ch_id)
                    origin = lookup_segment(ch_id, seq)
                if not origin:
                    self.send_error(404, "Segment Not Found")
                    return
                self.send_response(302)
                self.send_header("Location", origin)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return

        # 3. Serve M3U8 Manifest from the short live window
        if path.startswith("/live/") and path.endswith(".m3u8"):
            ch_id = path[len("/live/"):-len(".m3u8")]
            if ch_id not in CHANNELS:
                self.send_error(404, "Channel Not Found")
                return

            now = time.time()
            with LOCK:
                ACTIVE_REQUESTS[ch_id] = now
                stale = (
                    ch_id not in SLIDING_BUFFERS
                    or not SLIDING_BUFFERS[ch_id].get("segments")
                    or now - SLIDING_BUFFERS[ch_id].get("last_update", 0) > 8
                )

            if stale:
                update_channel_manifest(ch_id)

            proto, host = self._public_origin()
            manifest_text = render_manifest(ch_id, proto, host)

            if not manifest_text:
                # Keep TVmate from auto-skipping the channel, but never reset
                # MEDIA-SEQUENCE to 1 (that causes a visible jump).
                manifest_text = (
                    "#EXTM3U\n"
                    "#EXT-X-VERSION:3\n"
                    "#EXT-X-TARGETDURATION:6\n"
                    "#EXT-X-MEDIA-SEQUENCE:0\n"
                )

            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(manifest_text.encode("utf-8"))
            return

        self.send_error(404)


def main():
    threading.Thread(target=background_prefetch_worker, daemon=True).start()
    threading.Thread(target=background_keepalive_worker, daemon=True).start()

    server = ThreadedHTTPServer(("0.0.0.0", PORT), HLSProxyHandler)
    print("============================================================")
    print(f" Continuous 24/7 Zero-Drop HLS Proxy running on port {PORT}")
    print("============================================================")
    server.serve_forever()


if __name__ == "__main__":
    main()
