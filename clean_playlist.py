import concurrent.futures
import requests
import urllib3

# Suppress insecure HTTPS request warnings when checking legacy IPTV streams
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PLAYLIST_URL = "https://iptv-org.github.io/iptv/categories/sports.m3u"
OUTPUT_FILE = "sports_active.m3u"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VLC/3.0.18"}


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
    print("Downloading playlist...")
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

    print(f"Testing {len(entries)} streams concurrently...")

    working_entries = []
    # Check 20 streams simultaneously to speed up processing
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = executor.map(check_stream, entries)
        for result in results:
            if result:
                working_entries.append(result)

    print(f"\nSaving {len(working_entries)} active streams to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for header, url in working_entries:
            f.write(f"{header}\n{url}\n")

    print("Done!")


if __name__ == "__main__":
    main()