import concurrent.futures
import re
import requests
import urllib3

# Suppress insecure HTTPS request warnings when checking legacy IPTV streams
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PLAYLIST_URL = "https://iptv-org.github.io/iptv/categories/sports.m3u"
OUTPUT_FILE = "sports_active.m3u"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VLC/3.0.18"}

NEXTBET_CHANNELS = [
    {"name": "Yes 1", "id": "yes-1", "tvg_id": "Yes1.il", "group": "Israel"},
    {"name": "Yes 2", "id": "yes-2", "tvg_id": "Yes2.il", "group": "Israel"},
    {"name": "Yes 3", "id": "yes-3", "tvg_id": "Yes3.il", "group": "Israel"},
    {"name": "Yes 4", "id": "yes-4", "tvg_id": "Yes4.il", "group": "Israel"},
    {"name": "Yes 5", "id": "yes-5", "tvg_id": "Yes5.il", "group": "Israel"},
    {"name": "Kan 11", "id": "kan-11", "tvg_id": "Kan11.il", "group": "Israel"},
    {"name": "ONE HD", "id": "One-1", "tvg_id": "One.il", "group": "Sports"},
    {"name": "ONE 2 HD", "id": "one-2", "tvg_id": "One2.il", "group": "Sports"},
]


def fetch_nextbet_streams():
    print("Scraping live streams from 1nextbet7.tv...")
    scraped_entries = []
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://1nextbet7.tv/",
    }

    for item in NEXTBET_CHANNELS:
        ch_id = item["id"]
        ch_name = item["name"]
        tvg_id = item["tvg_id"]
        group = item["group"]

        url = f"https://1nextbet7.tv/kanal-izle/{ch_id}"
        try:
            res = requests.get(url, headers=base_headers, timeout=5, verify=False)
            if not res.ok:
                continue

            sources = re.findall(r'<source[^>]+src=["\']([^"\']+)', res.text, re.IGNORECASE)
            stream_url = None
            for s in sources:
                if any(ext in s.lower() for ext in [".css", ".m3u8", "mono"]):
                    stream_url = s
                    break

            if stream_url:
                with requests.get(stream_url, headers=base_headers, timeout=5, stream=True, verify=False) as chk:
                    if chk.ok or chk.status_code == 206:
                        header_block = (
                            f'#EXTINF:-1 tvg-id="{tvg_id}" group-title="{group}",{ch_name}\n'
                            f"#EXTVLCOPT:http-referrer=https://1nextbet7.tv/\n"
                            f"#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                        )
                        scraped_entries.append((header_block, stream_url))
                        print(f"  [OK] {ch_name}")
        except Exception:
            pass

    print(f"Added {len(scraped_entries)} channels from 1nextbet7.tv")
    return scraped_entries


def check_stream(item):
    header, url = item
    try:
        # Use context manager so open sockets are closed immediately after checking
        with requests.get(url, headers=HEADERS, timeout=5, stream=True, verify=False) as response:
            if not (response.ok or response.status_code == 206):
                print(f"[FAILED {response.status_code}] {url}")
                return None

            # 1. Reject responses with HTML Content-Type (maintenance, captcha, 404 landing pages)
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "application/xhtml+xml" in content_type:
                print(f"[HTML PAGE] {url}")
                return None

            # 2. Peek first 1024 bytes of response body to verify content
            chunk = next(response.iter_content(chunk_size=1024), b"")
            chunk_str = chunk.decode("utf-8", errors="ignore").strip()
            chunk_lower = chunk_str.lower()

            # Reject HTML body tags (e.g. <!DOCTYPE html>, <html>, <head>, etc.)
            if any(tag in chunk_lower for tag in ["<!doctype html", "<html", "<head>", "<body>", "<title>"]):
                print(f"[HTML BODY] {url}")
                return None

            # 3. If stream URL is an M3U/M3U8 playlist, ensure it contains valid M3U tags
            url_path = url.split("?")[0].lower()
            if url_path.endswith(".m3u") or url_path.endswith(".m3u8"):
                if not ("#EXTM3U" in chunk_str or "#EXT-X-" in chunk_str or "#EXTINF" in chunk_str):
                    print(f"[INVALID M3U] {url}")
                    return None

            print(f"[OK STREAM] {url}")
            return header, url
    except Exception:
        print(f"[OFFLINE] {url}")
    return None


def main():
    # 1. Fetch live channels from 1nextbet7.tv
    nextbet_entries = fetch_nextbet_streams()

    # 2. Download and check sports streams from iptv-org
    print("\nDownloading iptv-org playlist...")
    try:
        resp = requests.get(PLAYLIST_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"Failed to download playlist: {e}")
        return

    lines = resp.text.splitlines()

    entries = []
    current_headers = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # Don't overwrite header if there are metadata comments like #EXTGRP or #EXTVLCOPT
            if not line.startswith("#EXTM3U"):
                current_headers.append(line)
        else:
            # Any non-comment, non-empty line is treated as the stream URL
            if current_headers:
                header_block = "\n".join(current_headers)
                entries.append((header_block, line))
                current_headers = []

    print(f"Testing {len(entries)} iptv-org streams concurrently...")

    working_entries = []
    # Check 20 streams simultaneously to speed up processing
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = executor.map(check_stream, entries)
        for result in results:
            if result:
                working_entries.append(result)

    all_entries = nextbet_entries + working_entries

    print(f"\nSaving {len(all_entries)} active streams to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for header, url in all_entries:
            f.write(f"{header}\n{url}\n")

    print("Done!")


if __name__ == "__main__":
    main()
