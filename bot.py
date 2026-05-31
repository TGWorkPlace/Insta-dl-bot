"""
Instagram Reel & Post Downloader Bot
--------------------------------------
• Accepts Instagram reel/post links from users
• Identifies if the link is a REEL or a POST
  – Reel  → downloads video, extracts metadata (duration/thumb/dimensions)
             via ffmpeg, uploads as video
  – Post  → scrapes all images from vxinstagram, downloads them and
             sends as a media group (no metadata needed)
• Force-subscription gate before any action
• Logs every download to a log channel
• Lightweight HTTP server on port 8080 for Koyeb health-checks
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InputMediaPhoto,
)
from pyrogram.errors import UserNotParticipant

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
API_ID      = int(os.environ["API_ID"])
API_HASH    = os.environ["API_HASH"]
BOT_TOKEN   = os.environ["BOT_TOKEN"]
HEALTH_PORT = int(os.environ.get("PORT", 8080))

AUTH_CHANNEL = os.environ.get("AUTH_CHANNEL")
INVITE_LINK  = os.environ.get("INVITE_LINK", "")
LOG_CHANNEL  = os.environ.get("LOG_CHANNEL")

# ─────────────────────────────────────────────
# Regex helpers
# ─────────────────────────────────────────────

# Matches any Instagram reel / post / tv URL
INSTAGRAM_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:reel|p|tv)/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)

# A URL is a REEL if the path contains /reel/
REEL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/reel/",
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
# Koyeb health-check server
# ─────────────────────────────────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *_):
        pass


def _run_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
    log.info("Health-check server listening on port %d", HEALTH_PORT)
    server.serve_forever()


def start_health_server():
    t = Thread(target=_run_health_server, daemon=True)
    t.start()


# ─────────────────────────────────────────────
# Force-subscription helpers
# ─────────────────────────────────────────────

def _parse_channel(raw: str) -> int | str:
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        return raw.lstrip("@")


_CHANNEL_ID = _parse_channel(AUTH_CHANNEL)
_LOG_ID     = _parse_channel(LOG_CHANNEL)


async def is_subscribed(client: Client, user_id: int) -> bool:
    if _CHANNEL_ID is None:
        return True
    try:
        member = await client.get_chat_member(_CHANNEL_ID, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except UserNotParticipant:
        return False
    except Exception:
        log.exception("Error checking subscription for user %d", user_id)
        return True


def _fsub_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if INVITE_LINK:
        buttons.append(InlineKeyboardButton("📢 Join Channel", url=INVITE_LINK))
    buttons.append(
        InlineKeyboardButton("✅ Continue", callback_data="fsub_check")
    )
    return InlineKeyboardMarkup([buttons])


async def send_fsub_prompt(msg: Message) -> None:
    await msg.reply_text(
        "🔒 **You must join our channel to use this bot.**\n\n"
        "1. Click **Join Channel** below\n"
        "2. Then click **Continue**",
        reply_markup=_fsub_keyboard(),
    )


# ─────────────────────────────────────────────
# Log-channel helper
# ─────────────────────────────────────────────

async def log_reel_to_channel(
    client: Client,
    video_path: str,
    instagram_url: str,
    user: object,
    thumb_path: str | None,
    duration: int,
    width: int,
    height: int,
    has_thumb: bool,
) -> None:
    if _LOG_ID is None:
        return

    user_mention = f"[{user.first_name}](tg://user?id={user.id})"
    caption = (
        f"📥 **New Reel Downloaded**\n\n"
        f"👤 User: {user_mention} (`{user.id}`)\n"
        f"🔗 Link: {instagram_url}"
    )

    try:
        await client.send_video(
            chat_id=_LOG_ID,
            video=video_path,
            caption=caption,
            supports_streaming=True,
            thumb=thumb_path if has_thumb else None,
            duration=duration or None,
            width=width or None,
            height=height or None,
        )
        log.info("Logged reel to log channel for user %d", user.id)
    except Exception:
        log.exception("Failed to send reel log to LOG_CHANNEL")


async def log_post_to_channel(
    client: Client,
    image_paths: list[str],
    instagram_url: str,
    user: object,
) -> None:
    if _LOG_ID is None:
        return

    user_mention = f"[{user.first_name}](tg://user?id={user.id})"
    main_caption = (
        f"🖼 **New Post Downloaded**\n\n"
        f"👤 User: {user_mention} (`{user.id}`)\n"
        f"🔗 Link: {instagram_url}"
    )

    try:
        if len(image_paths) == 1:
            await client.send_photo(
                chat_id=_LOG_ID,
                photo=image_paths[0],
                caption=main_caption,
            )
        else:
            media_group = [
                InputMediaPhoto(
                    media=p,
                    caption=main_caption if i == 0 else "",
                )
                for i, p in enumerate(image_paths)
            ]
            await client.send_media_group(chat_id=_LOG_ID, media=media_group)
        log.info("Logged post to log channel for user %d", user.id)
    except Exception:
        log.exception("Failed to send post log to LOG_CHANNEL")


# ─────────────────────────────────────────────
# ffmpeg helpers  (reels only)
# ─────────────────────────────────────────────

def extract_metadata(video_path: str) -> tuple[int, int, int]:
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            video_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)

        width = height = duration = 0

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width  = int(stream.get("width", 0))
                height = int(stream.get("height", 0))
                break

        raw_dur = data.get("format", {}).get("duration")
        if raw_dur is None:
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    raw_dur = stream.get("duration")
                    break

        if raw_dur is not None:
            duration = int(float(raw_dur))

        log.info("Metadata → %dx%d  %ds", width, height, duration)
        return width, height, duration

    except Exception:
        log.exception("ffprobe failed, continuing without metadata")
        return 0, 0, 0


def extract_thumbnail(video_path: str, thumb_path: str, timestamp: float = 1.0) -> bool:
    for ts in (timestamp, 0.0):
        try:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(ts),
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                "-vf", "scale=320:-1",
                thumb_path,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode == 0 and os.path.exists(thumb_path):
                log.info("Thumbnail extracted at %.1fs → %s", ts, thumb_path)
                return True
        except Exception:
            log.exception("ffmpeg thumbnail extraction failed at ts=%.1f", ts)

    return False


# ─────────────────────────────────────────────
# Core scraping helpers
# ─────────────────────────────────────────────

def _to_vx_url(instagram_url: str) -> str:
    return re.sub(r"instagram\.com", "vxinstagram.com", instagram_url, count=1)


async def _get_vx_soup(instagram_url: str) -> BeautifulSoup:
    """Fetch the vxinstagram page and return a BeautifulSoup object."""
    vx_url = _to_vx_url(instagram_url)
    log.info("Fetching vxinstagram page: %s", vx_url)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        resp = await client.get(vx_url)
        resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# ── Reel scraper ─────────────────────────────

async def fetch_reel_download_link(instagram_url: str) -> str | None:
    """
    Scrape the direct .mp4 download URL for a reel from vxinstagram.
    Looks for the first <a class='btn-success' download> or any .mp4 href.
    """
    soup = await _get_vx_soup(instagram_url)

    btn = soup.find("a", class_="btn-success", attrs={"download": True})
    if btn and btn.get("href"):
        href = btn["href"]
        # vxinstagram returns images for posts, videos for reels
        # Accept any href from btn-success for reels
        log.info("Reel download URL found via btn-success: %s", href[:80])
        return href

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".mp4" in href or "rapidcdn" in href or "offload" in href:
            log.info("Reel download URL found via fallback: %s", href[:80])
            return href

    return None


# ── Post scraper ─────────────────────────────

async def fetch_post_image_urls(instagram_url: str) -> list[str]:
    """
    Scrape ALL image download URLs for a post from vxinstagram.

    vxinstagram renders each image in its own card with:
        <a class="btn-success" download href="...">Download</a>

    We collect every unique href from those buttons.
    For single-image posts there will be exactly one; for carousels, many.
    """
    soup = await _get_vx_soup(instagram_url)

    urls: list[str] = []
    seen: set[str] = set()

    for btn in soup.find_all("a", class_="btn-success", attrs={"download": True}):
        href = btn.get("href", "").strip()
        if href and href not in seen:
            seen.add(href)
            urls.append(href)
            log.info("Post image URL #%d: %s", len(urls), href[:80])

    if not urls:
        log.warning("No post image URLs found via btn-success, trying img src fallback")
        for img in soup.find_all("img", src=True):
            src = img["src"].strip()
            if ("rapidcdn" in src or "cdninstagram" in src) and src not in seen:
                seen.add(src)
                urls.append(src)

    log.info("Total post images found: %d", len(urls))
    return urls


# ─────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────

async def download_file(url: str, dest: str) -> None:
    """Stream any file (video or image) from url to dest."""
    log.info("Downloading → %s", dest)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=120) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=512 * 1024):
                    f.write(chunk)
    log.info("Download complete: %s", dest)


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


# ─────────────────────────────────────────────
# Pyrogram bot
# ─────────────────────────────────────────────
app = Client(
    "reel_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# ── /start ───────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, msg: Message):
    if not await is_subscribed(client, msg.from_user.id):
        await send_fsub_prompt(msg)
        return

    await msg.reply_text(
        "👋 **Instagram Reel & Post Downloader**\n\n"
        "Send me any Instagram reel or post link!\n\n"
        "• Reels → sent as video with full metadata\n"
        "• Posts → all images sent as a photo album\n\n"
        "Example:\n"
        "`https://www.instagram.com/reel/DY9khhtxvnu/`\n"
        "`https://www.instagram.com/p/DYuExr6E7wu/`"
    )


# ── Continue button callback ──────────────────
@app.on_callback_query(filters.regex("^fsub_check$"))
async def fsub_check_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id

    if await is_subscribed(client, user_id):
        await query.message.delete()
        await client.send_message(
            chat_id=user_id,
            text=(
                "👋 **Instagram Reel & Post Downloader**\n\n"
                "Send me any Instagram reel or post link!\n\n"
                "• Reels → sent as video with full metadata\n"
                "• Posts → all images sent as a photo album\n\n"
                "Example:\n"
                "`https://www.instagram.com/reel/DY9khhtxvnu/`\n"
                "`https://www.instagram.com/p/DYuExr6E7wu/`"
            ),
        )
    else:
        await query.answer(
            "❌ You haven't joined the channel yet!\nJoin and then press Continue.",
            show_alert=True,
        )


# ── Main message handler ──────────────────────
@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_message(client: Client, msg: Message):
    if not await is_subscribed(client, msg.from_user.id):
        await send_fsub_prompt(msg)
        return

    text = msg.text or ""
    match = INSTAGRAM_RE.search(text)

    if not match:
        await msg.reply_text(
            "⚠️ Please send a valid Instagram reel or post link.\n"
            "Example: `https://www.instagram.com/reel/ABC123/`"
        )
        return

    instagram_url = match.group(0)
    is_reel = bool(REEL_RE.search(instagram_url))

    log.info(
        "Received %s URL from user %d: %s",
        "REEL" if is_reel else "POST",
        msg.from_user.id,
        instagram_url,
    )

    if is_reel:
        await _handle_reel(client, msg, instagram_url)
    else:
        await _handle_post(client, msg, instagram_url)


# ─────────────────────────────────────────────
# Reel handler
# ─────────────────────────────────────────────

async def _handle_reel(client: Client, msg: Message, instagram_url: str) -> None:
    status = await msg.reply_text("🔍 Fetching reel download link…")

    # 1. Scrape
    try:
        download_url = await fetch_reel_download_link(instagram_url)
    except Exception as exc:
        log.exception("Failed to fetch reel page")
        await status.edit_text(f"❌ Could not fetch the page.\n`{exc}`")
        return

    if not download_url:
        await status.edit_text(
            "❌ No download link found on vxinstagram.\n"
            "The reel might be private or the service may be down."
        )
        return

    # 2. Download video
    await status.edit_text("⬇️ Downloading reel…")

    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_video.close()
    video_path = tmp_video.name

    tmp_thumb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp_thumb.close()
    thumb_path = tmp_thumb.name

    try:
        await download_file(download_url, video_path)
    except Exception as exc:
        log.exception("Reel download failed")
        await status.edit_text(f"❌ Download failed.\n`{exc}`")
        _cleanup(video_path, thumb_path)
        return

    # 3. Extract metadata + thumbnail
    await status.edit_text("🎞️ Processing video…")

    loop = asyncio.get_event_loop()
    width, height, duration = await loop.run_in_executor(None, extract_metadata, video_path)
    has_thumb = await loop.run_in_executor(None, extract_thumbnail, video_path, thumb_path, 1.0)

    # 4. Upload
    await status.edit_text("📤 Uploading to Telegram…")

    try:
        await msg.reply_video(
            video=video_path,
            caption="✅ Here's your reel!",
            supports_streaming=True,
            thumb=thumb_path if has_thumb else None,
            duration=duration or None,
            width=width or None,
            height=height or None,
        )
        await status.delete()
    except Exception as exc:
        log.exception("Reel upload failed")
        await status.edit_text(f"❌ Upload failed.\n`{exc}`")
        _cleanup(video_path, thumb_path)
        return

    # 5. Log
    await log_reel_to_channel(
        client=client,
        video_path=video_path,
        instagram_url=instagram_url,
        user=msg.from_user,
        thumb_path=thumb_path,
        duration=duration,
        width=width,
        height=height,
        has_thumb=has_thumb,
    )

    _cleanup(video_path, thumb_path)


# ─────────────────────────────────────────────
# Post handler
# ─────────────────────────────────────────────

async def _handle_post(client: Client, msg: Message, instagram_url: str) -> None:
    status = await msg.reply_text("🔍 Fetching post images…")

    # 1. Scrape all image URLs
    try:
        image_urls = await fetch_post_image_urls(instagram_url)
    except Exception as exc:
        log.exception("Failed to fetch post page")
        await status.edit_text(f"❌ Could not fetch the page.\n`{exc}`")
        return

    if not image_urls:
        await status.edit_text(
            "❌ No images found on vxinstagram.\n"
            "The post might be private or contain only a video.\n"
            "Try sharing the link as a reel URL if it's a video post."
        )
        return

    # 2. Download all images
    await status.edit_text(f"⬇️ Downloading {len(image_urls)} image(s)…")

    image_paths: list[str] = []
    try:
        for i, url in enumerate(image_urls, start=1):
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.close()
            await download_file(url, tmp.name)
            image_paths.append(tmp.name)
            log.info("Downloaded image %d/%d", i, len(image_urls))
    except Exception as exc:
        log.exception("Post image download failed")
        await status.edit_text(f"❌ Image download failed.\n`{exc}`")
        _cleanup(*image_paths)
        return

    # 3. Upload as media group (or single photo)
    await status.edit_text("📤 Uploading to Telegram…")

    try:
        if len(image_paths) == 1:
            await client.send_photo(
                chat_id=msg.chat.id,
                photo=image_paths[0],
                caption="✅ Here's your post!",
                reply_to_message_id=msg.id,
            )
        else:
            media_group = [
                InputMediaPhoto(
                    media=p,
                    caption="✅ Here's your post!" if i == 0 else "",
                )
                for i, p in enumerate(image_paths)
            ]
            await client.send_media_group(
                chat_id=msg.chat.id,
                media=media_group,
                reply_to_message_id=msg.id,
            )

        await status.delete()
    except Exception as exc:
        log.exception("Post upload failed")
        await status.edit_text(f"❌ Upload failed.\n`{exc}`")
        _cleanup(*image_paths)
        return

    # 4. Log
    await log_post_to_channel(
        client=client,
        image_paths=image_paths,
        instagram_url=instagram_url,
        user=msg.from_user,
    )

    _cleanup(*image_paths)


# ─────────────────────────────────────────────
# Entry-point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    start_health_server()
    log.info("Starting bot…")
    app.run()
