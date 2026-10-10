# Web Scraper

A lightweight, high-performance Python web scraping suite built with [Playwright](https://playwright.dev/python/) to extract images and videos from dynamic, JavaScript-heavy websites (React, Next.js, Vue, lazy-loaded media, etc.).

---

## Features

### 📸 Image Scraper (`scrape_images.py`)
* **Network & DOM Extraction**: Captures media directly from network traffic, standard `<img>` tags, `<picture>` elements with `srcset`, CSS `background-image` declarations, lazy-loading attributes (`data-src`, `data-original`, etc.), and OpenGraph/Twitter meta tags.
* **Lazy-Load Support**: Auto-scrolls pages to trigger lazy-loaded image requests before scraping.
* **Deduplication**: Hashes images using SHA-1 to prevent duplicate file saves.
* **Recursive Crawling**: Crawls internal links within the same domain up to a user-defined depth.
* **Robots.txt Compliance**: Checks `robots.txt` before crawling (can be bypassed if needed).

### 📹 Video Scraper (`scrape_videos.py`)
* **Multiple Stream Formats**: Detects direct video files (`.mp4`, `.webm`, `.mov`, `.mkv`, etc.), HLS playlists (`.m3u8`), and DASH manifests (`.mpd`).
* **Auto-Trigger Playback**: Automatically scrolls to and plays `<video>` elements (muted) to force network requests for video manifests and streams.
* **HLS & DASH Download Support**: Uses `ffmpeg` to download and convert HLS/DASH streams into standard MP4 files.
* **Cookie & Session Persistence**: Reuses browser cookies and session context so authenticated or session-protected media can be downloaded.
* **Embedded Player Detection**: Lists embedded iframe players (YouTube, Vimeo, Loom, Wistia, Twitch, etc.) that require dedicated tools like `yt-dlp`.
* **Dry-Run / List-Only Mode**: Discover video URLs across a site without downloading.

---

## Prerequisites

* **Python**: 3.10 or higher
* **System Dependencies**: `ffmpeg` (required for downloading HLS `.m3u8` and DASH `.mpd` video streams)

---

## Installation

1. **Clone the repository** (or navigate to the project directory):
   ```bash
   cd scraper
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install Python dependencies**:
   ```bash
   pip install playwright requests
   ```

4. **Install Playwright Chromium browser**:
   ```bash
   playwright install chromium
   ```
   *Note for Linux users: You may need system dependencies installed via `playwright install-deps chromium`.*

5. **Install `ffmpeg`** (optional, required for video stream downloads):
   * **Ubuntu/Debian**: `sudo apt update && sudo apt install ffmpeg`
   * **macOS**: `brew install ffmpeg`
   * **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html) or `winget install ffmpeg`

---

## Usage

### Image Scraper (`scrape_images.py`)

#### Basic Command
Download all images from a single page into the default `downloaded_images/` folder:
```bash
python scrape_images.py https://example.com
```

#### Advanced Options
```bash
# Crawl same-domain links 1 level deep and save to custom folder
python scrape_images.py https://example.com -o output_folder --depth 1

# Skip auto-scrolling and ignore images smaller than 5 KB
python scrape_images.py https://example.com --no-scroll --min-size 5120

# Run in headed mode (opens visible browser window for debugging)
python scrape_images.py https://example.com --headed
```

#### Options Summary
| Flag | Description | Default |
| :--- | :--- | :--- |
| `url` | Target website URL (must include `https://` or `http://`) | *Required* |
| `-o`, `--output` | Output folder for saved images | `downloaded_images` |
| `-d`, `--depth` | Link depth to crawl on the same domain (`0` = single page) | `0` |
| `--max-pages` | Maximum total pages to crawl | `25` |
| `--min-size` | Skip images smaller than N bytes | `1024` |
| `--delay` | Delay in seconds between page crawls | `1.0` |
| `--timeout` | Page load timeout in seconds | `30` |
| `--scroll-pause` | Pause between scroll steps in seconds | `0.4` |
| `--max-scrolls` | Maximum scroll steps per page | `40` |
| `--no-scroll` | Disable auto-scrolling | `False` |
| `--headed` | Run browser in visible window mode | `False` |
| `--ignore-robots` | Skip `robots.txt` checks | `False` |

---

### Video Scraper (`scrape_videos.py`)

#### Basic Command
Find and download direct video files and streams into `downloaded_videos/`:
```bash
python scrape_videos.py https://example.com
```

#### Advanced Options
```bash
# List all discovered video URLs without downloading
python scrape_videos.py https://example.com --list-only

# Crawl 1 level deep and filter out files smaller than 1 MB
python scrape_videos.py https://example.com -o clips --depth 1 --min-size 1000000

# Skip video auto-play phase
python scrape_videos.py https://example.com --no-play
```

#### Options Summary
| Flag | Description | Default |
| :--- | :--- | :--- |
| `url` | Target website URL (must include `https://` or `http://`) | *Required* |
| `-o`, `--output` | Output folder for saved videos | `downloaded_videos` |
| `-d`, `--depth` | Link depth to crawl on the same domain (`0` = single page) | `0` |
| `--max-pages` | Maximum total pages to crawl | `10` |
| `--min-size` | Skip video files smaller than N bytes | `100000` |
| `--delay` | Delay in seconds between page crawls | `1.0` |
| `--timeout` | Page load timeout in seconds | `30` |
| `--play-wait` | Seconds to let videos auto-play to trigger stream requests | `5.0` |
| `--max-scrolls` | Maximum scroll steps per page | `30` |
| `--no-play` | Disable auto-playing `<video>` elements | `False` |
| `--no-scroll` | Disable auto-scrolling | `False` |
| `--list-only` | Only list found videos/streams without downloading | `False` |
| `--headed` | Run browser in visible window mode | `False` |
| `--ignore-robots` | Skip `robots.txt` checks | `False` |

---

## How It Works

1. **Headless Chromium Engine**: Uses Playwright to render JavaScript, execute dynamic scripts, and trigger network calls.
2. **Network Interception**: Listens to HTTP response events to identify media assets sent via APIs or CDN endpoints.
3. **DOM Inspection**: Executes custom JavaScript in the browser context to inspect rendered DOM nodes, CSS computed styles, `<picture>` tags, and metadata.
4. **Auto-Scroll & Auto-Play**: Simulates user interaction (scrolling down and triggering video elements) to unleash lazy-loaded assets and stream playlist initialization.
5. **Stream Assembly**: Intercepts HLS (`.m3u8`) and DASH (`.mpd`) stream manifest URLs and hands them off to `ffmpeg` for re-assembly into standard `.mp4` video files.

---

## Legal & Ethical Disclaimer

This project is intended strictly for educational and legitimate personal backup purposes. 
* It **does not** bypass Digital Rights Management (DRM) or encrypted streams (e.g., Widevine, FairPlay).
* Respect website Terms of Service and copyright regulations.
* Only crawl and download content you own or have explicit permission to access.
