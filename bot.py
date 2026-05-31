"""
Instagram Reel Downloader Bot
------------------------------
• Accepts Instagram reel/post links from users
• Converts to vxinstagram, scrapes the direct download URL
• Downloads the video and sends it back to the user
• Extracts thumbnail + duration via ffmpeg after download
• Force-subscription gate before any action
• Logs every downloaded reel to a log channel
• Runs a lightweight HTTP server on port 8080 for Koyeb health-checks
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
)
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelInvalid

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

# Force-subscription channel (username or numeric ID like -1001234567890)
AUTH_CHANNEL = os.environ.get("AUTH_CHANNEL")          # e.g. "-1001234567890" or "mychannel"
INVITE_LINK  = os.environ.get("INVITE_LINK", "")       # e.g. "https://t.me/+xxxx" or "https://t.me/mychannel"

# Log channel (numeric ID or username — bot must be admin there)
LOG_CHANNEL  = os.environ.get("LOG_CHANNEL")           # e.g. "-1009876543210"

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
    """
    Return AUTH_CHANNEL as int if it looks like a numeric ID,
    otherwise return the string (username without @).
    """
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
    """
    Return True if the user is a member/admin/owner of AUTH_CHANNEL.
    If AUTH_CHANNEL is not set, always return True (feature disabled).
    """
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
        # Fail open so a mis-configured channel doesn't block everyone
        return True


def _fsub_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard shown when user hasn't subscribed yet."""
    buttons = []
    if INVITE_LINK:
        buttons.append(InlineKeyboardButton("📢 Join Channel", url=INVITE_LINK))
    buttons.append(
        InlineKeyboardButton("✅ Continue", callback_data="fsub_check")
    )
    return InlineKeyboardMarkup([buttons])


async def send_fsub_prompt(msg: Message) -> None:
    """Send the force-subscription message."""
    await msg.reply_text(
        "🔒 **You must join our channel to use this bot.**\n\n"
        "1. Click **Join Channel** below\n"
        "2. Then click **Continue**",
        reply_markup=_fsub_keyboard(),
    )


# ─────────────────────────────────────────────
# Log-channel helper
# ─────────────────────────────────────────────

async def log_to_channel(
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
    """
    Forward the downloaded reel to LOG_CHANNEL with the original link as caption.
    Silently skips if LOG_CHANNEL is not configured or upload fails.
    """
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
        log.exception("Failed to send log to LOG_CHANNEL")


# ─────────────────────────────────────────────
# ffmpeg helpers
# ─────────────────────────────────────────────

def extract_metadata(video_path: str) -> tuple[int, int, int]:
    """
    Use ffprobe to extract (width, height, duration_seconds) from a video file.
    Returns (0, 0, 0) on any failure.
    """
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
    """
    Use ffmpeg to grab a single frame at *timestamp* seconds as a JPEG thumbnail.
    Falls back to 0.0 s if the video is shorter than *timestamp*.
    Returns True on success.
    """
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
# Core helpers
# ─────────────────────────────────────────────

def _to_vx_url(instagram_url: str) -> str:
    return re.sub(r"instagram\.com", "vxinstagram.com", instagram_url, count=1)


async def fetch_download_link(instagram_url: str) -> str | None:
    """
    Convert URL → vxinstagram, fetch the page, parse the direct download href.
    """
    vx_url = _to_vx_url(instagram_url)
    log.info("Fetching vxinstagram page: %s", vx_url)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        resp = await client.get(vx_url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    btn = soup.find("a", class_="btn-success", attrs={"download": True})
    if btn and btn.get("href"):
        return btn["href"]

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".mp4" in href or "rapidcdn" in href or "offload" in href:
            return href

    return None


async def download_video(url: str, dest: str) -> None:
    """Stream the video from *url* to *dest*."""
    log.info("Downloading video → %s", dest)
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
    # Force-subscription gate
    if not await is_subscribed(client, msg.from_user.id):
        await send_fsub_prompt(msg)
        return

    await msg.reply_text(
        "👋 **Instagram Reel Downloader**\n\n"
        "Send me any Instagram reel / post link and I'll download it for you!\n\n"
        "Example:\n`https://www.instagram.com/reel/DY9khhtxvnu/`"
    )


# ── Continue button callback ──────────────────
@app.on_callback_query(filters.regex("^fsub_check$"))
async def fsub_check_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id

    if await is_subscribed(client, user_id):
        # Delete the fsub prompt and send the welcome message
        await query.message.delete()
        await client.send_message(
            chat_id=user_id,
            text=(
                "👋 **Instagram Reel Downloader**\n\n"
                "Send me any Instagram reel / post link and I'll download it for you!\n\n"
                "Example:\n`https://www.instagram.com/reel/DY9khhtxvnu/`"
            ),
        )
    else:
        # Show an alert pop-up (does NOT close the inline message)
        await query.answer(
            "❌ You haven't joined the channel yet!\nJoin and then press Continue.",
            show_alert=True,
        )


# ── Reel download handler ─────────────────────
@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_message(client: Client, msg: Message):
    # Force-subscription gate
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
    status = await msg.reply_text("🔍 Fetching download link…")

    # ── 1. Scrape vxinstagram ────────────────
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

    # ── 2. Download video ────────────────────
    await status.edit_text("⬇️ Downloading reel…")

    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_video.close()
    video_path = tmp_video.name

    tmp_thumb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp_thumb.close()
    thumb_path = tmp_thumb.name

    try:
        await download_video(download_url, video_path)
    except Exception as exc:
        log.exception("Download failed")
        await status.edit_text(f"❌ Download failed.\n`{exc}`")
        _cleanup(video_path, thumb_path)
        return

    # ── 3. Extract metadata + thumbnail ──────
    await status.edit_text("🎞️ Processing video…")

    loop = asyncio.get_event_loop()

    width, height, duration = await loop.run_in_executor(
        None, extract_metadata, video_path
    )
    has_thumb = await loop.run_in_executor(
        None, extract_thumbnail, video_path, thumb_path, 1.0
    )

    # ── 4. Upload to Telegram ────────────────
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
        log.exception("Upload failed")
        await status.edit_text(f"❌ Upload failed.\n`{exc}`")
        _cleanup(video_path, thumb_path)
        return

    # ── 5. Log to log channel ────────────────
    await log_to_channel(
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
# Entry-point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    start_health_server()
    log.info("Starting bot…")
    app.run()
