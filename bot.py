"""
Instagram Reel Downloader Bot
------------------------------
• Accepts Instagram reel/post links from users
• Converts to vxinstagram, scrapes the direct download URL
• Downloads the video and sends it back to the user
• Runs a lightweight HTTP server on port 8080 for Koyeb health-checks
"""

import asyncio
import logging
import os
import re
import tempfile
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import Message

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reelbot")

# ─────────────────────────────────────────────
# Config  (set these as env-vars on Koyeb)
# ─────────────────────────────────────────────
API_ID   = int(os.environ["API_ID"])          # Telegram API id
API_HASH = os.environ["API_HASH"]             # Telegram API hash
BOT_TOKEN = os.environ["BOT_TOKEN"]           # Bot token from @BotFather
HEALTH_PORT = int(os.environ.get("PORT", 8080))

# ─────────────────────────────────────────────
# Regex – matches every common Instagram URL shape
# ─────────────────────────────────────────────
INSTAGRAM_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:reel|p|tv)/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────
# Koyeb health-check server (port 8080)
# ─────────────────────────────────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *_):      # silence access logs
        pass


def _run_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
    log.info("Health-check server listening on port %d", HEALTH_PORT)
    server.serve_forever()


def start_health_server():
    t = Thread(target=_run_health_server, daemon=True)
    t.start()


# ─────────────────────────────────────────────
# Core helpers
# ─────────────────────────────────────────────

def _to_vx_url(instagram_url: str) -> str:
    """Convert an instagram.com URL to vxinstagram.com."""
    return re.sub(r"instagram\.com", "vxinstagram.com", instagram_url, count=1)


async def fetch_download_link(instagram_url: str) -> str | None:
    """
    1. Convert URL → vxinstagram
    2. Fetch the rendered HTML
    3. Parse out the direct .mp4 download href
    Returns the download URL or None.
    """
    vx_url = _to_vx_url(instagram_url)
    log.info("Fetching vxinstagram page: %s", vx_url)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        resp = await client.get(vx_url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Primary: <a class="btn btn-success" download href="...">
    btn = soup.find("a", class_="btn-success", attrs={"download": True})
    if btn and btn.get("href"):
        return btn["href"]

    # Fallback: any <a> whose href ends with .mp4 or contains rapidcdn/offload
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".mp4" in href or "rapidcdn" in href or "offload" in href:
            return href

    return None


async def download_video(url: str, dest: str) -> None:
    """Stream the video file from *url* to *dest* path."""
    log.info("Downloading video → %s", dest)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=120) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 512):  # 512 KB
                    f.write(chunk)
    log.info("Download complete: %s", dest)


# ─────────────────────────────────────────────
# Pyrogram bot
# ─────────────────────────────────────────────
app = Client(
    "reel_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


@app.on_message(filters.command("start"))
async def cmd_start(_, msg: Message):
    await msg.reply_text(
        "👋 **Instagram Reel Downloader**\n\n"
        "Send me any Instagram reel / post link and I'll download it for you!\n\n"
        "Example:\n`https://www.instagram.com/reel/DY9khhtxvnu/`"
    )


@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_message(_, msg: Message):
    text = msg.text or ""
    match = INSTAGRAM_RE.search(text)

    if not match:
        await msg.reply_text(
            "⚠️ Please send a valid Instagram reel or post link.\n"
            "Example: `https://www.instagram.com/reel/ABC123/`"
        )
        return

    instagram_url = match.group(0)

    # ── Step 1: acknowledge ──────────────────
    status = await msg.reply_text("🔍 Fetching download link…")

    try:
        download_url = await fetch_download_link(instagram_url)
    except Exception as exc:
        log.exception("Failed to fetch vxinstagram page")
        await status.edit_text(f"❌ Could not fetch the page.\n`{exc}`")
        return

    if not download_url:
        await status.edit_text(
            "❌ No download link found on vxinstagram.\n"
            "The reel might be private or the service may be down."
        )
        return

    # ── Step 2: download ─────────────────────
    await status.edit_text("⬇️ Downloading reel…")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await download_video(download_url, tmp_path)
    except Exception as exc:
        log.exception("Download failed")
        await status.edit_text(f"❌ Download failed.\n`{exc}`")
        os.unlink(tmp_path)
        return

    # ── Step 3: upload ───────────────────────
    await status.edit_text("📤 Uploading to Telegram…")

    try:
        await msg.reply_video(
            video=tmp_path,
            caption="✅ Here's your reel!",
            supports_streaming=True,
        )
        await status.delete()
    except Exception as exc:
        log.exception("Upload failed")
        await status.edit_text(f"❌ Upload failed.\n`{exc}`")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ─────────────────────────────────────────────
# Entry-point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    start_health_server()
    log.info("Starting bot…")
    app.run()
