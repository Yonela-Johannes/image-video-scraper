#!/usr/bin/env python3
"""
scrape_videos.py - find and download videos from a website.

Finds:
  * direct video files (.mp4, .webm, .mov ...) from <video> tags and network traffic
  * HLS (.m3u8) and DASH (.mpd) streams (downloaded with ffmpeg, if installed)
Lists (but does not download):
  * embedded players like YouTube / Vimeo (use yt-dlp for those, see notes below)

Does NOT: bypass DRM / encrypted streams (Netflix-style). Only download content
you own or have permission to save.

Setup:
    python3 -m venv .venv && source .venv/bin/activate
    pip install playwright requests
    playwright install chromium
    sudo apt install ffmpeg        # only needed for .m3u8 / .mpd streams

Usage:
    python scrape_videos.py https://example.com
    python scrape_videos.py https://example.com -o clips --depth 1
    python scrape_videos.py https://example.com --list-only
"""

import argparse
import hashlib
import mimetypes
import re
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".ogv"}
EMBED_RE = re.compile(
    r"(youtube\.com|youtube-nocookie\.com|youtu\.be|vimeo\.com|dailymotion\.com|"
    r"wistia\.(com|net)|loom\.com|twitch\.tv|facebook\.com/plugins/video)", re.I
)

# Runs inside the page: gather video URLs from the rendered DOM.
DOM_VIDEO_JS = r"""
() => {
  const out = [];
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch { return null; } };
  const add = (u, fromTag) => {
    const a = u && abs(u);
    if (a && !a.startsWith('blob:') && !a.startsWith('data:')) out.push({url: a, fromTag});
  };

  document.querySelectorAll('video').forEach(v => {
    add(v.currentSrc || v.src, true);
    add(v.getAttribute('data-src'), true);
    v.querySelectorAll('source').forEach(s => add(s.src, true));
  });
  document.querySelectorAll('meta[property="og:video"], meta[property="og:video:url"], meta[property="og:video:secure_url"]')
    .forEach(m => add(m.content, false));
  document.querySelectorAll('a[href]').forEach(a => add(a.href, false));

  const embeds = [...document.querySelectorAll('iframe[src]')].map(f => abs(f.src)).filter(Boolean);
  return {videos: out, embeds};
}
"""

PLAY_JS = """
async (waitMs) => {
  const vids = [...document.querySelectorAll('video')];
  for (const v of vids) {
    try { v.muted = true; v.scrollIntoView({block: 'center'}); await v.play(); } catch (e) {}
  }
  await new Promise(r => setTimeout(r, waitMs));
  vids.forEach(v => { try { v.pause(); } catch (e) {} });
}
"""

SCROLL_JS = """
async ({pause, maxSteps}) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let last = -1;
  for (let i = 0; i < maxSteps; i++) {
    window.scrollBy(0, window.innerHeight * 0.8);
    await sleep(pause);
    const h = document.documentElement.scrollHeight;
    if (window.innerHeight + window.scrollY >= h - 2 && h === last) break;
    last = h;
  }
  window.scrollTo(0, 0);
}
"""


def classify(url: str, ctype: str | None = None, from_tag: bool = False) -> str | None:
    """Return 'hls', 'dash', 'file', or None (not a video worth downloading)."""
    path = urlparse(url).path.lower()
    ctype = (ctype or "").split(";")[0].strip().lower()

    if path.endswith(".m3u8") or "mpegurl" in ctype:
        return "hls"
    if path.endswith(".mpd") or ctype == "application/dash+xml":
        return "dash"
    # stream segments are not standalone videos
    if path.endswith((".ts", ".m4s", ".m4f")) or ctype in ("video/mp2t", "video/iso.segment"):
        return None
    if ctype.startswith("video/"):
        return "file"
    if Path(path).suffix in VIDEO_EXTS:
        return "file"
    if from_tag and not ctype:
        return "file"  # e.g. <video src="/stream/123"> with no extension
    return None


def safe_name(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).stem or "video"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


def target_path(url: str, out_dir: Path, ext: str) -> Path:
    tag = hashlib.sha1(url.encode()).hexdigest()[:8]
    return out_dir / f"{safe_name(url)}_{tag}{ext}"


def video_extension(url: str, ctype: str) -> str:
    ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) if ctype else None
    if ext in VIDEO_EXTS or ext == ".qt":
        return ".mov" if ext == ".qt" else ext
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in VIDEO_EXTS else ".mp4"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def download_file(session: requests.Session, url: str, out_dir: Path, referer: str, min_bytes: int) -> bool:
    """Stream a direct video file to disk."""
    try:
        with session.get(url, stream=True, timeout=30, headers={"Referer": referer}) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if ctype.startswith("text/") or "html" in ctype or "json" in ctype:
                return False  # not a video after all

            dest = target_path(url, out_dir, video_extension(url, ctype))
            if dest.exists():
                print(f"  [exists] {dest.name}")
                return False

            total = int(resp.headers.get("Content-Length", 0))
            if total and total < min_bytes:
                return False

            part = dest.with_suffix(dest.suffix + ".part")
            done, last_print = 0, 0.0
            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_print > 1:
                        pct = f" {done / total * 100:.0f}%" if total else ""
                        print(f"  [downloading] {dest.name} {human(done)}{pct}", end="\r")
                        last_print = now

            if done < min_bytes:
                part.unlink(missing_ok=True)
                return False
            part.rename(dest)
            print(f"  [saved] {dest.name} ({human(done)})            ")
            return True
    except requests.RequestException as e:
        print(f"  [fail] {url} ({e})")
        return False


def download_stream(url: str, out_dir: Path, referer: str) -> bool:
    """Download an HLS/DASH stream into an .mp4 using ffmpeg."""
    dest = target_path(url, out_dir, ".mp4")
    if dest.exists():
        print(f"  [exists] {dest.name}")
        return False
    print(f"  [ffmpeg] {url}")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-stats",
        "-user_agent", USER_AGENT,
        "-headers", f"Referer: {referer}\r\n",
        "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(dest),
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        print("  [fail] ffmpeg could not download this stream (it may be encrypted/DRM-protected)")
        return False
    print(f"  [saved] {dest.name} ({human(dest.stat().st_size)})")
    return True


def collect_page(page, context, url, args) -> tuple[dict[str, str], set[str]]:
    """Load a page and return ({video_url: kind}, {embed_urls})."""
    found: dict[str, str] = {}

    def on_response(resp):
        try:
            kind = classify(resp.url, resp.headers.get("content-type"))
            if kind and resp.status in (200, 206):
                found.setdefault(resp.url, kind)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            print("  [warn] page load timed out, using what loaded")

        if not args.no_scroll:
            page.evaluate(SCROLL_JS, {"pause": 400, "maxSteps": args.max_scrolls})

        if not args.no_play:
            # Playing each <video> (muted) makes the site request the real media files/manifests
            page.evaluate(PLAY_JS, int(args.play_wait * 1000))

        dom = page.evaluate(DOM_VIDEO_JS)
    finally:
        page.remove_listener("response", on_response)

    for item in dom["videos"]:
        kind = classify(item["url"], None, from_tag=item["fromTag"])
        if kind:
            found.setdefault(item["url"], kind)

    embeds = {e for e in dom["embeds"] if EMBED_RE.search(e)}
    return found, embeds


def main():
    p = argparse.ArgumentParser(description="Find and download videos from a website.")
    p.add_argument("url", help="Starting URL (include https://)")
    p.add_argument("-o", "--output", default="downloaded_videos", help="Output folder")
    p.add_argument("-d", "--depth", type=int, default=0, help="Link levels to follow on the same domain")
    p.add_argument("--max-pages", type=int, default=10, help="Safety cap on pages crawled")
    p.add_argument("--min-size", type=int, default=100_000, help="Skip files smaller than N bytes (ads, previews)")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between pages")
    p.add_argument("--timeout", type=int, default=30, help="Page load timeout in seconds")
    p.add_argument("--play-wait", type=float, default=5.0, help="Seconds to let videos play to trigger requests")
    p.add_argument("--max-scrolls", type=int, default=30, help="Max scroll steps per page")
    p.add_argument("--no-play", action="store_true", help="Don't auto-play <video> elements")
    p.add_argument("--no-scroll", action="store_true", help="Don't auto-scroll")
    p.add_argument("--list-only", action="store_true", help="Only list video URLs, don't download")
    p.add_argument("--headed", action="store_true", help="Show the browser window")
    p.add_argument("--ignore-robots", action="store_true", help="Don't check robots.txt")
    args = p.parse_args()

    out_dir = Path(args.output)
    if not args.list_only:
        out_dir.mkdir(parents=True, exist_ok=True)

    has_ffmpeg = shutil.which("ffmpeg") is not None
    start = urlparse(args.url)
    base_netloc = start.netloc

    robots = None
    if not args.ignore_robots:
        try:
            robots = RobotFileParser()
            robots.set_url(f"{start.scheme}://{base_netloc}/robots.txt")
            robots.read()
        except Exception:
            robots = None

    queue = deque([(args.url, 0)])
    visited: set[str] = set()
    seen_videos: set[str] = set()
    all_embeds: set[str] = set()
    saved = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        page = context.new_page()

        while queue and len(visited) < args.max_pages:
            url, level = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            if robots and not robots.can_fetch(USER_AGENT, url):
                print(f"[skip] blocked by robots.txt: {url}")
                continue

            print(f"\n[page] {url} (depth {level})")
            try:
                videos, embeds = collect_page(page, context, url, args)
            except Exception as e:
                print(f"  [fail] {e}")
                continue

            all_embeds |= embeds

            # Reuse the browser's cookies so logged-in / session-protected media works
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            for c in context.cookies():
                session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

            for vid_url, kind in videos.items():
                if vid_url in seen_videos:
                    continue
                seen_videos.add(vid_url)
                print(f"  [found:{kind}] {vid_url}")
                if args.list_only:
                    continue
                if kind == "file":
                    saved += download_file(session, vid_url, out_dir, url, args.min_size)
                elif has_ffmpeg:
                    saved += download_stream(vid_url, out_dir, url)
                else:
                    print("  [skip] stream found but ffmpeg isn't installed (sudo apt install ffmpeg)")

            if level < args.depth:
                try:
                    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
                except Exception:
                    hrefs = []
                for href in hrefs:
                    link = urljoin(url, href).split("#")[0]
                    parsed = urlparse(link)
                    if (parsed.scheme in ("http", "https") and parsed.netloc == base_netloc
                            and link not in visited and Path(parsed.path).suffix.lower() not in VIDEO_EXTS):
                        queue.append((link, level + 1))

            time.sleep(args.delay)

        browser.close()

    if all_embeds:
        print("\nEmbedded players found (not downloaded; try `yt-dlp <url>` if you have the right to):")
        for e in sorted(all_embeds):
            print(f"  {e}")

    action = "found" if args.list_only else "saved"
    count = len(seen_videos) if args.list_only else saved
    print(f"\nDone. Crawled {len(visited)} page(s), {action} {count} video(s)"
          + ("." if args.list_only else f" in '{out_dir}/'."))


if __name__ == "__main__":
    main()
    
# python scrape_videos.py https://example.com
# python scrape_videos.py https://example.com -o clips --depth 1
# python scrape_videos.py https://example.com --list-only 