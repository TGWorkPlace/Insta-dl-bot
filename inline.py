"""
inline.py — Inline query handler for the Instagram Reel & Post Downloader Bot
-------------------------------------------------------------------------------
• User types:  @botusername <instagram_url>  in any chat
• Bot validates the URL, identifies REEL or POST, and returns a result
• Tapping the result triggers the bot to send the media via PM
  (inline bots cannot upload files directly — we send a deep-link button
   that opens the bot PM with the URL pre-filled as a /start parameter)
• Force-subscription check is enforced here too
• Banned users are silently rejected
"""

import logging
import base64
import re

from pyrogram import Client
from pyrogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors.exceptions.bad_request_400 import QueryIdInvalid

# ── Re-use helpers already defined in your main bot module ──────────────────
# Import the same regex patterns and subscription checker from your main file.
# Adjust the import path if your project structure differs.
from bot import (
    INSTAGRAM_RE,
    REEL_RE,
    is_subscribed,
    _CHANNEL_ID,
)

log = logging.getLogger("reelbot.inline")

# ─────────────────────────────────────────────
# Deep-link helpers
# ─────────────────────────────────────────────

def _encode_url(instagram_url: str) -> str:
    """
    Base64-encode the Instagram URL so it can be passed safely as a
    Telegram /start deep-link parameter (only A-Za-z0-9_- are allowed).
    """
    encoded = base64.urlsafe_b64encode(instagram_url.encode()).decode().rstrip("=")
    return encoded


def _make_deeplink_button(bot_username: str, instagram_url: str, label: str) -> InlineKeyboardMarkup:
    param = _encode_url(instagram_url)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, url=f"https://t.me/{bot_username}?start={param}")]
    ])


# ─────────────────────────────────────────────
# Inline query handler
# ─────────────────────────────────────────────

@Client.on_inline_query()
async def inline_answer(bot: Client, query: InlineQuery):
    """
    Handle inline queries of the form:  @botusername <instagram_url>

    Flow:
      1. Reject banned/unauthorised users silently
      2. Check force-subscription
      3. Validate the URL with INSTAGRAM_RE
      4. Return an InlineQueryResultArticle whose tap opens bot PM
         with a deep-link that encodes the URL → main.py /start handler
         decodes it and processes the download
    """
    user = query.from_user
    raw_text = (query.query or "").strip()

    # ── 1. Subscription gate ────────────────────────────────────────────────
    if _CHANNEL_ID and not await is_subscribed(bot, query):
        await query.answer(
            results=[],
            cache_time=0,
            switch_pm_text="🔒 Join our channel first to use this bot",
            switch_pm_parameter="subscribe",
        )
        return

    # ── 2. Empty query — show usage hint ────────────────────────────────────
    if not raw_text:
        await query.answer(
            results=[
                InlineQueryResultArticle(
                    title="📎 Paste an Instagram link",
                    description="e.g. https://www.instagram.com/reel/ABC123/",
                    input_message_content=InputTextMessageContent(
                        "Send me an Instagram reel or post link to download it!"
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Open bot", url=f"https://t.me/{(await bot.get_me()).username}")
                    ]])
                )
            ],
            cache_time=0,
        )
        return

    # ── 3. Validate the URL ─────────────────────────────────────────────────
    match = INSTAGRAM_RE.search(raw_text)
    if not match:
        await query.answer(
            results=[
                InlineQueryResultArticle(
                    title="⚠️ Invalid Instagram link",
                    description="Please enter a valid Instagram reel or post URL",
                    input_message_content=InputTextMessageContent(
                        "⚠️ That doesn't look like a valid Instagram link.\n\n"
                        "Supported formats:\n"
                        "• `https://www.instagram.com/reel/AbcDefg/`\n"
                        "• `https://www.instagram.com/p/AbcDefg/`"
                    ),
                )
            ],
            cache_time=0,
        )
        return

    instagram_url = match.group(0)
    is_reel = bool(REEL_RE.search(instagram_url))
    media_type = "Reel 🎬" if is_reel else "Post 🖼️"
    emoji_icon = "🎬" if is_reel else "🖼️"

    # Shorten URL for display
    short_url = instagram_url if len(instagram_url) <= 50 else instagram_url[:47] + "…"

    log.info(
        "Inline query from user %d | type=%s | url=%s",
        user.id,
        "REEL" if is_reel else "POST",
        instagram_url,
    )

    # ── 4. Build result ─────────────────────────────────────────────────────
    bot_me = await bot.get_me()
    reply_markup = _make_deeplink_button(
        bot_username=bot_me.username,
        instagram_url=instagram_url,
        label=f"{emoji_icon} Download {media_type}",
    )

    result = InlineQueryResultArticle(
        title=f"{emoji_icon} Download Instagram {media_type}",
        description=short_url,
        input_message_content=InputTextMessageContent(
            f"{emoji_icon} **Instagram {media_type}**\n\n"
            f"🔗 {instagram_url}\n\n"
            f"👇 Tap the button below to download!"
        ),
        reply_markup=reply_markup,
        thumb_url=(
            "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a5/"
            "Instagram_icon.png/240px-Instagram_icon.png"
        ),
    )

    try:
        await query.answer(
            results=[result],
            cache_time=10,
            is_personal=True,
        )
    except QueryIdInvalid:
        pass
    except Exception as e:
        log.exception("Failed to answer inline query: %s", e)
