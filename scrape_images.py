#!/usr/bin/env python3
"""
scrape_images_playwright.py - download all images from a JavaScript-rendered website.

Uses a real headless Chromium so it sees images that are injected by JS (React,
Next.js, etc.), lazy-loaded on scroll, or referenced from external CSS.

Setup:
    python3 -m venv .venv && source .venv/bin/activate
    pip install playwright
    playwright install chromium
    # Linux may also need: playwright install-deps chromium

Usage:
    python scrape_images_playwright.py https://example.com
    python scrape_images_playwright.py https://example.com -o shots --depth 1 --headed
"""

import argparse
import hashlib
import mimetypes
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Runs inside the page: collects image URLs the network listener might miss
# (currentSrc of <img>, computed CSS background images, <picture> sources).
DOM_IMAGE_JS = r"""
() => {
  const urls = new Set();
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch { return null; } };
  const add = (u) => { const a = u && abs(u); if (a && !a.startsWith('data:')) urls.add(a); };

  document.querySelectorAll('img').forEach(img => {
    add(img.currentSrc || img.src);
    ['data-src', 'data-original', 'data-lazy-src'].forEach(a => add(img.getAttribute(a)));
  });
  document.querySelectorAll('picture source[srcset]').forEach(s => {
    const parts = s.srcset.split(',').map(p => p.trim().split(/\s+/)[0]);
    parts.forEach(add);
  });
  document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"]')
    .forEach(m => add(m.content));

  const re = /url\((['"]?)(.*?)\1\)/g;
  document.querySelectorAll('*').forEach(el => {
    for (const pseudo of [null, '::before', '::after']) {
      const bg = getComputedStyle(el, pseudo).backgroundImage;
      if (bg && bg !== 'none') {
        let m; re.lastIndex = 0;
        while ((m = re.exec(bg)) !== null) add(m[2]);
      }
    }
  });
  return [...urls];
}
"""

LINKS_JS = "els => els.map(a => a.href)"

SCROLL_JS = """
async ({pause, maxSteps}) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let last = -1;
  for (let i = 0; i < maxSteps; i++) {
    window.scrollBy(0, window.innerHeight * 0.8);
    await sleep(pause);
    const h = document.documentElement.scrollHeight;
    const atBottom = window.innerHeight + window.scrollY >= h - 2;
    if (atBottom && h === last) break;
    last = h;
  }
  window.scrollTo(0, 0);
}
"""


def pick_extension(url: str, content_type: str | None) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix and len(suffix) <= 5 else ".jpg"


def safe_name(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).stem or "image"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


def save_image(url, data: bytes, ctype: str, out_dir: Path, seen_hashes: set, min_bytes: int) -> bool:
    if len(data) < min_bytes:
        return False
    digest = hashlib.sha1(data).hexdigest()
    if digest in seen_hashes:
        return False
    seen_hashes.add(digest)
    path = out_dir / f"{safe_name(url)}_{digest[:8]}{pick_extension(url, ctype)}"
    path.write_bytes(data)
    print(f"  [saved] {path.name} ({len(data) / 1024:.1f} KB)")
    return True


def scrape_page(page, context, url, args, out_dir, seen_urls, seen_hashes) -> int:
    """Load one page, collect every image, save them. Returns number saved."""
    captured: dict[str, tuple[bytes, str]] = {}  # url -> (bytes, content-type)

    def on_response(resp):
        try:
            ctype = resp.headers.get("content-type", "")
            if resp.request.resource_type == "image" or ctype.startswith("image/"):
                if resp.ok and resp.url not in captured and not resp.url.startswith("data:"):
                    captured[resp.url] = (resp.body(), ctype)
        except Exception:
            pass  # body evicted / redirect / navigation; DOM pass will refetch

    page.on("response", on_response)
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeout:
                pass  # busy sites never go idle; carry on
        except PlaywrightTimeout:
            print("  [warn] page load timed out, using what loaded")

        if not args.no_scroll:
            page.evaluate(SCROLL_JS, {"pause": int(args.scroll_pause * 1000), "maxSteps": args.max_scrolls})
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeout:
                pass

        dom_urls = page.evaluate(DOM_IMAGE_JS)
    finally:
        page.remove_listener("response", on_response)

    saved = 0

    # 1) images the browser downloaded itself
    for img_url, (data, ctype) in captured.items():
        if img_url in seen_urls:
            continue
        seen_urls.add(img_url)
        saved += save_image(img_url, data, ctype, out_dir, seen_hashes, args.min_size)

    # 2) images found in the DOM that weren't captured (fetch with the browser's cookies)
    for img_url in dom_urls:
        if img_url in seen_urls:
            continue
        seen_urls.add(img_url)
        try:
            resp = context.request.get(img_url, timeout=20000)
            ctype = resp.headers.get("content-type", "")
            if resp.ok and ctype.startswith("image/"):
                saved += save_image(img_url, resp.body(), ctype, out_dir, seen_hashes, args.min_size)
        except Exception as e:
            print(f"  [fail] {img_url} ({e})")

    return saved


def main():
    p = argparse.ArgumentParser(description="Scrape all images from a JS-rendered website.")
    p.add_argument("url", help="Starting URL (include https://)")
    p.add_argument("-o", "--output", default="downloaded_images", help="Output folder")
    p.add_argument("-d", "--depth", type=int, default=0,
                   help="Link levels to follow on the same domain (0 = only the given page)")
    p.add_argument("--max-pages", type=int, default=25, help="Safety cap on pages crawled")
    p.add_argument("--min-size", type=int, default=1024, help="Skip images smaller than N bytes")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between pages")
    p.add_argument("--timeout", type=int, default=30, help="Page load timeout in seconds")
    p.add_argument("--scroll-pause", type=float, default=0.4, help="Seconds to wait between scroll steps")
    p.add_argument("--max-scrolls", type=int, default=40, help="Max scroll steps per page")
    p.add_argument("--no-scroll", action="store_true", help="Don't auto-scroll (skips lazy-load triggering)")
    p.add_argument("--headed", action="store_true", help="Show the browser window (useful for debugging)")
    p.add_argument("--ignore-robots", action="store_true", help="Don't check robots.txt")
    args = p.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

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
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    total = 0

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
                total += scrape_page(page, context, url, args, out_dir, seen_urls, seen_hashes)
            except Exception as e:
                print(f"  [fail] {e}")
                continue

            if level < args.depth:
                try:
                    hrefs = page.eval_on_selector_all("a[href]", LINKS_JS)
                except Exception:
                    hrefs = []
                for href in hrefs:
                    link = urljoin(url, href).split("#")[0]
                    parsed = urlparse(link)
                    if parsed.scheme in ("http", "https") and parsed.netloc == base_netloc and link not in visited:
                        queue.append((link, level + 1))

            time.sleep(args.delay)

        browser.close()

    print(f"\nDone. Crawled {len(visited)} page(s), saved {total} image(s) to '{out_dir}/'.")


if __name__ == "__main__":
    main()
    
    
# python scrape_images_playwright.py https://example.com
# python scrape_images_playwright.py https://example.com -o shots --depth 1
# python scrape_images_playwright.py https://example.com --headed
# python scrape.py