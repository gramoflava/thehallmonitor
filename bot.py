"""
bot.py — thehallmonitor Telegram bot.

Monitors group messages for references to materials listed in the
configured forbidden-materials list. Action taken on a match depends
on the per-group mode set by an admin via private chat (/settings).

Modes:
  off             — silent, no action
  react           — react to the offending message with a configurable emoji
  reply (default) — reply with a warning message
  delete_silent   — delete the offending message silently
  delete_notify   — delete + post a brief "removed by" notice

Cleanup (react / reply modes):
  • User edits their message to remove violation → bot removes its reaction/reply
  • Any user reacts to the bot's warning → bot removes the warning
    (covers the case where the original was deleted — Telegram has no delete event)

Setup:
    cp .env.example .env
    # Set BOT_TOKEN, INDEX_PAGE, BASE_URL, DOC_LINK_RE in .env
    python updater.py   # initial DB population
    python bot.py
"""

from __future__ import annotations

import asyncio
import datetime
import datetime as _dt
import logging
import os

from dotenv import load_dotenv
from telegram import Message, ReactionTypeEmoji, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from admin import build_admin_handlers
from config import (
    MODE_DELETE_NOTIFY,
    MODE_DELETE_SILENT,
    MODE_LABELS,
    MODE_OFF,
    MODE_PERMISSIONS,
    MODE_REACT,
    MODE_REPLY,
    ChatConfig,
)
from database import Database, DB_PATH
from matcher import Matcher
from updater import run_update

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set. "
        "Create a .env file with BOT_TOKEN=<your token>."
    )

# Scheduled update time (daily, UTC)
_UPDATE_HOUR_UTC   = 4
_UPDATE_MINUTE_UTC = 0

# ── In-memory state ───────────────────────────────────────────────────────────

# (chat_id, original_msg_id) → bot_warning_msg_id
_active_warnings: dict[tuple[int, int], int] = {}

# (chat_id, bot_warning_msg_id) → original_msg_id  (reverse lookup for reactions)
_warning_to_original: dict[tuple[int, int], int] = {}

# Per-group config cache  {chat_id → ChatConfig}
_config_cache: dict[int, ChatConfig] = {}

# Global matcher
_db: Database | None = None
_matcher = Matcher()


# ── DB / matcher helpers ──────────────────────────────────────────────────────

def _reload_matcher() -> None:
    global _db
    if _db is None:
        _db = Database(DB_PATH)
        _db.connect()
    _matcher.load_from_db(_db)


def _get_chat_config(chat_id: int) -> ChatConfig:
    if chat_id not in _config_cache:
        assert _db is not None
        _config_cache[chat_id] = ChatConfig.from_db_row(_db.get_chat_config(chat_id))
    return _config_cache[chat_id]


# ── Warning state helpers ─────────────────────────────────────────────────────

def _store_warning(chat_id: int, original_msg_id: int, warning_msg_id: int) -> None:
    _active_warnings[(chat_id, original_msg_id)] = warning_msg_id
    _warning_to_original[(chat_id, warning_msg_id)] = original_msg_id


def _remove_warning(chat_id: int, original_msg_id: int) -> int | None:
    warning_msg_id = _active_warnings.pop((chat_id, original_msg_id), None)
    if warning_msg_id is not None:
        _warning_to_original.pop((chat_id, warning_msg_id), None)
    return warning_msg_id


# ── Content extraction ────────────────────────────────────────────────────────

def _extract_content(msg: Message) -> str:
    """
    Build the full string to check from a Telegram message.

    Sources:
      1. Message text or caption
      2. Forward origin channel username (catches forwarded posts from channels)
      3. Forward origin channel title (catches forwarded posts by plain-text name)
    """
    parts: list[str] = []

    typed = msg.text or msg.caption or ""
    if typed:
        parts.append(typed)

    if msg.forward_origin:
        fo = msg.forward_origin
        chat = getattr(fo, "chat", None)
        if chat:
            if chat.username:
                parts.append("@" + chat.username)
            if chat.title:
                parts.append(chat.title)

    return " ".join(parts)


# ── Group message handlers ────────────────────────────────────────────────────

async def handle_new_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    if not msg:
        return

    # Ignore the bot's own messages
    if msg.from_user and msg.from_user.id == context.bot.id:
        return

    # Record this group as known (handles bots added before this feature existed)
    assert _db is not None
    _db.upsert_known_chat(
        msg.chat_id,
        msg.chat.title if msg.chat else None,
        msg.chat.username if msg.chat else None,
    )

    content = _extract_content(msg)
    if not content:
        return

    matches = _matcher.check_message(content)
    if not matches:
        return

    key = (msg.chat_id, msg.message_id)
    if key in _active_warnings:
        return  # Already warned; don't duplicate

    cfg = _get_chat_config(msg.chat_id)

    if cfg.mode == MODE_OFF:
        return

    # Record violation (one event per message)
    token_types = list({r.token_type for r in matches})
    _db.record_violation(msg.chat_id, token_types)

    try:
        if cfg.mode == MODE_REACT:
            await context.bot.set_message_reaction(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                reaction=[ReactionTypeEmoji(emoji=cfg.reaction_emoji)],
            )
            # Store with the original msg_id as both key and "warning" id
            # so reaction-cleanup and edit-cleanup can find it
            _store_warning(msg.chat_id, msg.message_id, msg.message_id)
            logger.info(
                "Reacted chat=%d msg=%d emoji=%s | %s",
                msg.chat_id, msg.message_id, cfg.reaction_emoji,
                [(r.token_type, r.token) for r in matches],
            )

        elif cfg.mode == MODE_REPLY:
            warning = await context.bot.send_message(
                chat_id=msg.chat_id,
                text=cfg.effective_warning_text(),
                reply_to_message_id=msg.message_id,
            )
            _store_warning(msg.chat_id, msg.message_id, warning.message_id)
            logger.info(
                "Warned chat=%d msg=%d warning=%d | %s",
                msg.chat_id, msg.message_id, warning.message_id,
                [(r.token_type, r.token) for r in matches],
            )

        elif cfg.mode == MODE_DELETE_SILENT:
            await context.bot.delete_message(msg.chat_id, msg.message_id)
            logger.info(
                "Deleted silently chat=%d msg=%d | %s",
                msg.chat_id, msg.message_id,
                [(r.token_type, r.token) for r in matches],
            )

        elif cfg.mode == MODE_DELETE_NOTIFY:
            await context.bot.delete_message(msg.chat_id, msg.message_id)
            name = (
                msg.from_user.first_name
                if msg.from_user and msg.from_user.first_name
                else "A user"
            )
            notice = await context.bot.send_message(
                chat_id=msg.chat_id,
                text=f"ℹ️ A message by {name} was removed.",
            )
            _store_warning(msg.chat_id, msg.message_id, notice.message_id)
            logger.info(
                "Deleted+notified chat=%d msg=%d user=%s | %s",
                msg.chat_id, msg.message_id, name,
                [(r.token_type, r.token) for r in matches],
            )

    except (BadRequest, TelegramError) as exc:
        logger.error(
            "Failed to act (mode=%s chat=%d msg=%d): %s",
            cfg.mode, msg.chat_id, msg.message_id, exc,
        )


async def handle_edited_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    On edit: re-check the message.
    - If now clean AND a warning/reaction exists → remove it.
    - If still matches → leave in place (don't re-warn to avoid spam).
    """
    msg = update.edited_message
    if not msg:
        return

    content = _extract_content(msg)
    matches = _matcher.check_message(content) if content else []

    if matches:
        return  # Still violating — nothing to do

    key = (msg.chat_id, msg.message_id)
    warning_msg_id = _active_warnings.get(key)
    if warning_msg_id is None:
        return  # No active warning for this message

    cfg = _get_chat_config(msg.chat_id)
    _remove_warning(msg.chat_id, msg.message_id)

    try:
        if cfg.mode == MODE_REACT:
            await context.bot.set_message_reaction(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                reaction=[],
            )
            logger.info(
                "Removed reaction chat=%d msg=%d — message is now clean.",
                msg.chat_id, msg.message_id,
            )
        else:
            await context.bot.delete_message(msg.chat_id, warning_msg_id)
            logger.info(
                "Deleted warning chat=%d original=%d — message is now clean.",
                msg.chat_id, msg.message_id,
            )
    except (BadRequest, TelegramError) as exc:
        logger.warning("Could not clean up warning %d: %s", warning_msg_id, exc)


async def handle_bot_added(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Record the group when the bot is added to it."""
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    if any(m.id == context.bot.id for m in msg.new_chat_members):
        chat = update.effective_chat
        assert _db is not None
        _db.upsert_known_chat(chat.id, chat.title, chat.username)
        logger.info("Added to group chat_id=%d title=%r", chat.id, chat.title)


# ── Reaction cleanup trigger ──────────────────────────────────────────────────

async def handle_reaction_on_warning(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Any user reacting to one of the bot's warning messages is a signal to
    clean up — covers the case where the original message was deleted
    (Telegram sends no delete event to bots).
    """
    reaction = update.message_reaction
    if not reaction:
        return

    chat_id = reaction.chat.id
    warning_msg_id = reaction.message_id

    original_msg_id = _warning_to_original.get((chat_id, warning_msg_id))
    if original_msg_id is None:
        return  # Not one of our warning messages

    _remove_warning(chat_id, original_msg_id)

    try:
        await context.bot.delete_message(chat_id, warning_msg_id)
        logger.info(
            "Warning %d deleted in chat=%d after user reaction (original=%d).",
            warning_msg_id, chat_id, original_msg_id,
        )
    except (BadRequest, TelegramError) as exc:
        logger.warning(
            "Could not delete warning %d on reaction: %s", warning_msg_id, exc
        )


# ── Group commands (/status, /stats, /rules) ──────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    assert _db is not None

    counts  = _db.count_tokens()
    total   = sum(counts.values())
    updated = _db.get_metadata("last_updated") or "never"

    # Month-over-month delta
    now       = _dt.datetime.utcnow()
    prev_month = (now.replace(day=1) - _dt.timedelta(days=1))
    prev_key   = prev_month.strftime("token_count_%Y-%m")
    prev_s     = _db.get_metadata(prev_key)
    delta_str  = ""
    if prev_s:
        delta    = total - int(prev_s)
        sign     = "+" if delta >= 0 else ""
        delta_str = f"  ({sign}{delta} vs last month)"

    cfg        = _get_chat_config(msg.chat_id)
    mode_label = MODE_LABELS.get(cfg.mode, str(cfg.mode))
    perm_note  = MODE_PERMISSIONS.get(cfg.mode, "")

    text = (
        f"🤖 <b>thehallmonitor</b>\n"
        f"\n"
        f"<b>Mode:</b> {mode_label}\n"
        f"<i>{perm_note}</i>\n"
        f"<b>Custom warning text:</b> {'Yes' if cfg.warning_text else 'No (using default)'}\n"
        f"\n"
        f"<b>📋 Forbidden list</b>\n"
        f"Last updated: {updated} UTC\n"
        f"Entries: {total:,}{delta_str}\n"
        f"\n"
        f"To configure, message me privately."
    )
    await msg.reply_text(text, parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    assert _db is not None

    stats   = _db.get_violation_stats(msg.chat_id, days=30)
    total   = stats["total"]
    last    = stats["last_occurred_at"] or "—"
    by_type = stats["by_type"]

    if total == 0:
        await msg.reply_text("📊 No violations detected in the last 30 days.")
        return

    breakdown = "\n".join(
        f"  {label}: {by_type.get(t, 0)}"
        for t, label in [
            ("handle", "Handles (@channel)"),
            ("url",    "Links (URL)"),
            ("domain", "Domains"),
            ("text",   "Names (text)"),
        ]
        if by_type.get(t, 0) > 0
    )

    text = (
        f"📊 <b>Activity (last 30 days)</b>\n"
        f"\n"
        f"Violations detected: {total}\n"
        f"Last violation: {last} UTC\n"
        f"\n"
        f"{breakdown}"
    )
    await msg.reply_text(text, parse_mode="HTML")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg        = update.effective_message
    cfg        = _get_chat_config(msg.chat_id)
    mode_label = MODE_LABELS.get(cfg.mode, str(cfg.mode))

    text = (
        f"📋 <b>How this bot works</b>\n"
        f"\n"
        f"I monitor all messages in this group against a list of restricted materials.\n"
        f"\n"
        f"<b>Current mode:</b> {mode_label}\n"
        f"\n"
        f"<b>What I check:</b>\n"
        f"• Links and URLs (exact matches to specific pages, not entire platforms)\n"
        f"• Telegram channel handles (<code>@channel</code>)\n"
        f"• Channel and page names (exact word match only — not partial)\n"
        f"• Forwarded messages from restricted channels\n"
        f"\n"
        f"<b>To dismiss a warning:</b>\n"
        f"Edit your message to remove the flagged content, or react to my warning "
        f"message — I will remove it if the original is gone or now clean.\n"
        f"\n"
        f"Questions? Contact a group admin."
    )
    await msg.reply_text(text, parse_mode="HTML")


# ── Scheduled job ─────────────────────────────────────────────────────────────

async def _daily_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Scheduled daily update starting...")
    loop = asyncio.get_running_loop()
    try:
        stats = await loop.run_in_executor(None, run_update)
        if stats["success"]:
            if stats.get("skipped"):
                logger.info("Daily update: no changes in source document.")
            else:
                _reload_matcher()
                _config_cache.clear()
                logger.info(
                    "Daily update: DB refreshed — %d tokens loaded.",
                    stats["tokens_total"],
                )
        else:
            logger.error(
                "Daily update failed: %s", stats.get("error", "unknown error")
            )
    except Exception as exc:
        logger.error("Daily update raised an exception: %s", exc, exc_info=True)


# ── Startup ───────────────────────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    """
    Called once after the Application is built but before polling starts.
    Initialises the shared DB and loads the matcher.
    """
    global _db

    if _db is None:
        _db = Database(DB_PATH)
        _db.connect()

    # Expose DB and cache to admin.py handlers via bot_data
    application.bot_data["db"] = _db
    application.bot_data["config_cache"] = _config_cache

    _reload_matcher()
    token_count = (
        len(_matcher.urls) + len(_matcher.domains)
        + len(_matcher.handles) + len(_matcher.texts)
    )

    if token_count == 0:
        logger.warning("Database is empty on startup — running initial update.")
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(None, run_update)
        if stats["success"]:
            _reload_matcher()
            logger.info(
                "Initial update complete: %d tokens loaded.", stats["tokens_total"]
            )
        else:
            logger.error(
                "Initial update failed: %s. Bot will start but cannot check "
                "messages until the database is populated.",
                stats.get("error"),
            )
    else:
        logger.info(
            "Matcher loaded: %d tokens (%d urls, %d domains, %d handles, %d text).",
            token_count,
            len(_matcher.urls), len(_matcher.domains),
            len(_matcher.handles), len(_matcher.texts),
        )

    bot_info = await application.bot.get_me()
    logger.info(
        "Bot started: @%s (id=%d) — monitoring group messages.",
        bot_info.username, bot_info.id,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    group_filter = filters.ChatType.GROUPS

    # Bot added to a group
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_bot_added)
    )

    # Public group commands
    app.add_handler(CommandHandler("status", cmd_status, filters=group_filter))
    app.add_handler(CommandHandler("stats",  cmd_stats,  filters=group_filter))
    app.add_handler(CommandHandler("rules",  cmd_rules,  filters=group_filter))

    # Admin private-chat handlers (ConversationHandler must come first)
    for handler in build_admin_handlers():
        app.add_handler(handler)

    # All non-command group messages
    app.add_handler(
        MessageHandler(group_filter & ~filters.COMMAND, handle_new_message)
    )

    # Edited messages in groups
    app.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE & group_filter,
            handle_edited_message,
        )
    )

    # Reactions on messages (warning cleanup trigger)
    app.add_handler(MessageReactionHandler(handle_reaction_on_warning))

    # Daily DB update at 04:00 UTC
    app.job_queue.run_daily(
        callback=_daily_update,
        time=datetime.time(
            hour=_UPDATE_HOUR_UTC,
            minute=_UPDATE_MINUTE_UTC,
            tzinfo=datetime.timezone.utc,
        ),
        name="daily_update",
    )

    logger.info("Starting polling...")
    app.run_polling(
        allowed_updates=[
            Update.MESSAGE,
            Update.EDITED_MESSAGE,
            Update.MESSAGE_REACTION,
            Update.CALLBACK_QUERY,
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
