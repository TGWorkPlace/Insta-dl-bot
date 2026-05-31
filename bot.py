"""
Instagram Reel & Post Downloader Bot
--------------------------------------
• Accepts Instagram reel/post/tv links
• Detects content type: reel/video vs multi-image post
• Reels  → downloads MP4, extracts metadata + thumbnail via ffmpeg, sends as video
• Posts  → scrapes ALL image download links, downloads each, sends as photo album
• Force-subscription gate (AUTH_CHANNEL + INVITE_LINK)
• Logs every download to LOG_CHANNEL (video or media group)
• Koyeb health-check server on PORT (default 8080)
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Literal

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
log = logging.getLogger("instabot")

# ─────────────────────────────────────────────
# Config  (set these as env-vars on Koyeb)
# ─────────────────────────────────────────────
API_ID       = int(os.environ["API_ID"])
API_HASH     = os.environ["API_HASH"]
BOT_TOKEN    = os.environ["BOT_TOKEN"]
HEALTH_PORT  = int(os.environ.get("PORT", 8080))

# Force-subscription channel  (numeric ID like -1001234567890  OR  @username)
AUTH_CHANNEL = os.environ.get("AUTH_CHANNEL")
INVITE_LINK  = os.environ.get("INVITE_LINK", "")   # https://t.me/+xxxx  or  https://t.me/mychannel

# Log channel — bot must be admin here
LOG_CHANNEL  = os.environ.get("LOG_CHANNEL")

# ─────────────────────────────────────────────
# Regex
# ─────────────────────────────────────────────
INSTAGRAM_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(reel|p|tv)/([A-Za-z0-9_-]+)/?",
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
# Scraped-page result
# ─────────────────────────────────────────────
@dataclass
class PageResult:
    """What we parsed from the vxinstagram page."""
    kind: Literal["video", "images"]   # "video" = reel/tv,  "images" = photo post
    links: list[str] = field(default_factory=list)


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
    Thread(target=_run_health_server, daemon=True).start()


# ─────────────────────────────────────────────
# Force-subscription helpers
# ─────────────────────────────────────────────

def _parse_channel(raw: str | None) -> int | str | None:
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
    """True if user is a member of AUTH_CHANNEL (or feature is disabled)."""
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
        log.exception("Subscription check failed for user %d", user_id)
        return True  # fail open


def _fsub_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if INVITE_LINK:
        buttons.append(InlineKeyboardButton("📢 Join Channel", url=INVITE_LINK))
    buttons.append(InlineKeyboardButton("✅ Continue", callback_data="fsub_check"))
    return InlineKeyboardMarkup([buttons])


async def send_fsub_prompt(msg: Message) -> None:
    await msg.reply_text(
        "🔒 **You must join our channel to use this bot.**\n\n"
        "1. Click **Join Channel** below\n"
        "2. Then click **Continue**",
        reply_markup=_fsub_keyboard(),
    )


# ─────────────────────────────────────────────
# vxinstagram page scraper
# ─────────────────────────────────────────────

def _to_vx_url(instagram_url: str) -> str:
    return re.sub(r"instagram\.com", "vxinstagram.com", instagram_url, count=1)


async def scrape_page(instagram_url: str) -> PageResult | None:
    """
    Fetch the vxinstagram page and return a PageResult with:
      - kind="video"  + one MP4/cdn link  (reel / tv)
      - kind="images" + one or more image download links  (photo post)
    Returns None if nothing usable was found.
    """
    vx_url = _to_vx_url(instagram_url)
    log.info("Scraping vxinstagram: %s", vx_url)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        resp = await client.get(vx_url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Collect ALL btn-success download anchors  (one per media item in a carousel)
    download_anchors = soup.find_all("a", class_="btn-success", attrs={"download": True})

    # If we found at least one, inspect the first href to decide video vs image
    if download_anchors:
        links = [a["href"] for a in download_anchors if a.get("href")]
        if not links:
            return None

        first = links[0].lower()
        if ".mp4" in first or "offload" in first:
            # Video — only ever one item even in multi-anchor pages
            return PageResult(kind="video", links=[links[0]])
        else:
            # Image carousel — all links are photos
            return PageResult(kind="images", links=links)

    # Fallback scan for any href containing mp4/rapidcdn/offload
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".mp4" in href or "offload" in href:
            return PageResult(kind="video", links=[href])
        if "rapidcdn" in href:
            return PageResult(kind="images", links=[href])

    return None


# ─────────────────────────────────────────────
# Download helper
# ─────────────────────────────────────────────

async def download_file(url: str, dest: str) -> None:
    """Stream *url* to *dest* (works for both video and image)."""
    log.info("Downloading → %s", dest)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=120) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=512 * 1024):
                    f.write(chunk)
    log.info("Download complete: %s", dest)


# ─────────────────────────────────────────────
# ffmpeg helpers  (video only)
# ─────────────────────────────────────────────

def extract_metadata(video_path: str) -> tuple[int, int, int]:
    """Returns (width, height, duration_seconds). Falls back to (0,0,0)."""
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

        log.info("Metadata → %dx%d %ds", width, height, duration)
        return width, height, duration
    except Exception:
        log.exception("ffprobe failed")
        return 0, 0, 0


def extract_thumbnail(video_path: str, thumb_path: str, timestamp: float = 1.0) -> bool:
    """Extract a JPEG thumbnail from *video_path* at *timestamp* seconds."""
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
                log.info("Thumbnail extracted at %.1fs", ts)
                return True
        except Exception:
            log.exception("ffmpeg thumbnail failed at ts=%.1f", ts)
    return False


# ─────────────────────────────────────────────
# Log-channel helpers
# ─────────────────────────────────────────────

async def log_video_to_channel(
    client: Client,
    video_path: str,
    instagram_url: str,
    user,
    thumb_path: str,
    duration: int,
    width: int,
    height: int,
    has_thumb: bool,
) -> None:
    if _LOG_ID is None:
        return
    caption = (
        f"📥 **New Reel Downloaded**\n\n"
        f"👤 User: [{user.first_name}](tg://user?id={user.id}) (`{user.id}`)\n"
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
        log.info("Logged reel to log channel")
    except Exception:
        log.exception("Failed to log reel to LOG_CHANNEL")


async def log_images_to_channel(
    client: Client,
    image_paths: list[str],
    instagram_url: str,
    user,
) -> None:
    if _LOG_ID is None:
        return
    caption = (
        f"📥 **New Post Downloaded** ({len(image_paths)} image{'s' if len(image_paths) != 1 else ''})\n\n"
        f"👤 User: [{user.first_name}](tg://user?id={user.id}) (`{user.id}`)\n"
        f"🔗 Link: {instagram_url}"
    )
    try:
        if len(image_paths) == 1:
            await client.send_photo(chat_id=_LOG_ID, photo=image_paths[0], caption=caption)
        else:
            media = [
                InputMediaPhoto(p, caption=caption if i == 0 else "")
                for i, p in enumerate(image_paths)
            ]
            await client.send_media_group(chat_id=_LOG_ID, media=media)
        log.info("Logged post (%d images) to log channel", len(image_paths))
    except Exception:
        log.exception("Failed to log images to LOG_CHANNEL")


# ─────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────

def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


# ─────────────────────────────────────────────
# Pyrogram bot
# ─────────────────────────────────────────────
app = Client(
    "insta_bot",
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
        "👋 **Instagram Downloader**\n\n"
        "Send me any Instagram reel or post link!\n\n"
        "• **Reels / Videos** → sent as a streamable video\n"
        "• **Photo Posts** → sent as a photo album\n\n"
        "Example:\n"
        "`https://www.instagram.com/reel/DY9khhtxvnu/`\n"
        "`https://www.instagram.com/p/DYuExr6E7wu/`"
    )


# ── Continue button callback ──────────────────
@app.on_callback_query(filters.regex("^fsub_check$"))
async def fsub_check_callback(client: Client, query: CallbackQuery):
    if await is_subscribed(client, query.from_user.id):
        await query.message.delete()
        await client.send_message(
            chat_id=query.from_user.id,
            text=(
                "👋 **Instagram Downloader**\n\n"
                "Send me any Instagram reel or post link!\n\n"
                "• **Reels / Videos** → sent as a streamable video\n"
                "• **Photo Posts** → sent as a photo album\n\n"
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


# ── Main download handler ─────────────────────
@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_message(client: Client, msg: Message):
    # Force-sub gate
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

    url_type   = match.group(1).lower()   # "reel", "p", or "tv"
    instagram_url = match.group(0)

    # Human-readable label for status messages
    if url_type == "reel":
        label = "reel"
    elif url_type == "tv":
        label = "IGTV video"
    else:
        label = "post"

    status = await msg.reply_text(f"🔍 Fetching {label}…")

    # ── 1. Scrape vxinstagram ────────────────
    try:
        result = await scrape_page(instagram_url)
    except Exception as exc:
        log.exception("Scrape failed")
        await status.edit_text(f"❌ Could not fetch the page.\n`{exc}`")
        return

    if not result or not result.links:
        await status.edit_text(
            f"❌ No download link found for this {label}.\n"
            "It might be private or vxinstagram may be down."
        )
        return

    # ══════════════════════════════════════════
    # VIDEO PATH  (reel / tv / single video)
    # ══════════════════════════════════════════
    if result.kind == "video":
        await status.edit_text(f"⬇️ Downloading {label}…")

        tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp_video.close()
        video_path = tmp_video.name

        tmp_thumb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_thumb.close()
        thumb_path = tmp_thumb.name

        try:
            await download_file(result.links[0], video_path)
        except Exception as exc:
            log.exception("Video download failed")
            await status.edit_text(f"❌ Download failed.\n`{exc}`")
            _cleanup(video_path, thumb_path)
            return

        await status.edit_text("🎞️ Processing video…")
        loop = asyncio.get_event_loop()
        width, height, duration = await loop.run_in_executor(
            None, extract_metadata, video_path
        )
        has_thumb = await loop.run_in_executor(
            None, extract_thumbnail, video_path, thumb_path, 1.0
        )

        await status.edit_text(f"📤 Uploading {label}…")
        try:
            await msg.reply_video(
                video=video_path,
                caption=f"✅ Here's your {label}!",
                supports_streaming=True,
                thumb=thumb_path if has_thumb else None,
                duration=duration or None,
                width=width or None,
                height=height or None,
            )
            await status.delete()
        except Exception as exc:
            log.exception("Video upload failed")
            await status.edit_text(f"❌ Upload failed.\n`{exc}`")
            _cleanup(video_path, thumb_path)
            return

        # Log
        await log_video_to_channel(
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

    # ══════════════════════════════════════════
    # IMAGES PATH  (photo post / carousel)
    # ══════════════════════════════════════════
    else:
        total = len(result.links)
        await status.edit_text(
            f"⬇️ Downloading {total} image{'s' if total != 1 else ''} from {label}…"
        )

        image_paths: list[str] = []
        failed = False

        for i, link in enumerate(result.links, start=1):
            await status.edit_text(
                f"⬇️ Downloading image {i}/{total}…"
            )
            tmp_img = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp_img.close()
            try:
                await download_file(link, tmp_img.name)
                image_paths.append(tmp_img.name)
            except Exception as exc:
                log.exception("Image %d download failed", i)
                await status.edit_text(f"❌ Failed to download image {i}.\n`{exc}`")
                _cleanup(tmp_img.name)
                failed = True
                break

        if failed or not image_paths:
            _cleanup(*image_paths)
            return

        await status.edit_text(f"📤 Uploading {total} image{'s' if total != 1 else ''}…")

        try:
            if total == 1:
                # Single photo — send as photo
                await msg.reply_photo(
                    photo=image_paths[0],
                    caption="✅ Here's your post!",
                )
            else:
                # Carousel — send as media group (max 10 per group, Telegram limit)
                # Split into chunks of 10 if needed
                chunk_size = 10
                for chunk_start in range(0, total, chunk_size):
                    chunk = image_paths[chunk_start: chunk_start + chunk_size]
                    media = [
                        InputMediaPhoto(
                            p,
                            caption="✅ Here's your post!" if chunk_start == 0 and i == 0 else "",
                        )
                        for i, p in enumerate(chunk)
                    ]
                    await msg.reply_media_group(media=media)

            await status.delete()
        except Exception as exc:
            log.exception("Image upload failed")
            await status.edit_text(f"❌ Upload failed.\n`{exc}`")
            _cleanup(*image_paths)
            return

        # Log
        await log_images_to_channel(
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
