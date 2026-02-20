"""
admin.py — Private-chat admin interface for thehallmonitor.

Admins interact with the bot one-on-one to configure per-group settings.
All handlers here are restricted to private chats only.

Flow:
  /start | /settings  →  list groups where the user is admin
  tap group           →  show current settings + action buttons
  tap mode button     →  update mode immediately
  tap "Custom message"→  ConversationHandler asks for text, then saves
  tap "Reaction emoji"→  ConversationHandler asks for emoji, then saves
  tap "Reset"         →  restore defaults for that group

Callback data format (all prefixed):
  grp:<chat_id>              show settings for a group
  mode:<chat_id>:<mode_int>  set action mode
  msg:<chat_id>              start custom-message conversation
  reaction:<chat_id>         start reaction-emoji conversation
  reset:<chat_id>            reset group to defaults
  back:<chat_id>             return to settings from a sub-menu
"""

from __future__ import annotations

import logging

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import (
    MODE_LABELS,
    MODE_OFF,
    MODE_PERMISSIONS,
    ChatConfig,
)
from database import Database

logger = logging.getLogger(__name__)

# ConversationHandler state constants
_AWAIT_MSG_TEXT   = 1
_AWAIT_REACTION   = 2


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    """Retrieve the shared Database from bot_data."""
    return context.bot_data["db"]


def _invalidate_config_cache(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    cache: dict = context.bot_data.setdefault("config_cache", {})
    cache.pop(chat_id, None)


async def _get_admin_groups(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> list[dict]:
    """
    Return known groups where `user_id` is an administrator or creator.
    Silently skips any group where the query fails (bot removed, etc.).
    """
    db = _db(context)
    known = db.get_known_chats()
    admin_groups = []
    for chat in known:
        try:
            member = await context.bot.get_chat_member(chat["chat_id"], user_id)
            if member.status in ("administrator", "creator"):
                admin_groups.append(chat)
        except (BadRequest, TelegramError):
            pass
    return admin_groups


async def _check_bot_can_delete(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Return True if the bot has delete-messages permission in the group."""
    try:
        bot_member = await context.bot.get_chat_member(
            chat_id, context.bot.id
        )
        # can_delete_messages is only present for administrators
        return getattr(bot_member, "can_delete_messages", False)
    except (BadRequest, TelegramError):
        return False


def _settings_keyboard(cfg: ChatConfig, chat_id: int) -> InlineKeyboardMarkup:
    """Build the inline keyboard for a group's settings screen."""
    rows = []
    # Mode buttons — current mode shown with ✓
    for mode_int, label in MODE_LABELS.items():
        marker = "✓ " if cfg.mode == mode_int else ""
        rows.append([InlineKeyboardButton(
            f"{marker}{label}",
            callback_data=f"mode:{chat_id}:{mode_int}",
        )])
    rows.append([
        InlineKeyboardButton("✏️ Custom message", callback_data=f"msg:{chat_id}"),
        InlineKeyboardButton("😡 Reaction emoji", callback_data=f"reaction:{chat_id}"),
    ])
    rows.append([InlineKeyboardButton("🔄 Reset to defaults", callback_data=f"reset:{chat_id}")])
    return InlineKeyboardMarkup(rows)


def _settings_text(cfg: ChatConfig, chat_title: str, needs_perm_warn: bool) -> str:
    mode_label = MODE_LABELS.get(cfg.mode, str(cfg.mode))
    perm_note  = MODE_PERMISSIONS.get(cfg.mode, "")
    warn_text  = cfg.warning_text or "(default)"
    lines = [
        f"⚙️ Settings for <b>{chat_title}</b>",
        "",
        f"Mode: {mode_label}",
        f"  {perm_note}",
    ]
    if needs_perm_warn:
        lines.append("  ⚠️ Bot does not currently have Delete messages permission!")
    lines += [
        "",
        f"Warning text: {warn_text}",
        f"Reaction emoji: {cfg.reaction_emoji}",
    ]
    return "\n".join(lines)


# ── /start and /settings ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_settings(update, context)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    admin_groups = await _get_admin_groups(user.id, context)

    if not admin_groups:
        await update.message.reply_text(
            "I don't see any groups where you are an admin and I am a member.\n\n"
            "Add me to a group, grant me admin rights, and then come back here."
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            chat.get("title") or f"Group {chat['chat_id']}",
            callback_data=f"grp:{chat['chat_id']}",
        )]
        for chat in admin_groups
    ])
    await update.message.reply_text(
        "Select a group to configure:",
        reply_markup=keyboard,
    )


# ── Callback query dispatcher ─────────────────────────────────────────────────

async def handle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    query: CallbackQuery = update.callback_query
    await query.answer()

    data = query.data or ""
    user_id = query.from_user.id

    # ── grp:<chat_id> — show settings ────────────────────────────────────────
    if data.startswith("grp:"):
        chat_id = int(data.split(":")[1])
        return await _show_settings(query, context, user_id, chat_id)

    # ── mode:<chat_id>:<mode_int> — set mode ─────────────────────────────────
    if data.startswith("mode:"):
        _, chat_id_s, mode_s = data.split(":")
        chat_id, mode = int(chat_id_s), int(mode_s)
        if not await _verify_admin(query, context, user_id, chat_id):
            return None
        _db(context).set_chat_config(chat_id, mode=mode)
        _invalidate_config_cache(context, chat_id)
        return await _show_settings(query, context, user_id, chat_id)

    # ── reset:<chat_id> — restore defaults ───────────────────────────────────
    if data.startswith("reset:"):
        chat_id = int(data.split(":")[1])
        if not await _verify_admin(query, context, user_id, chat_id):
            return None
        _db(context).set_chat_config(
            chat_id, mode=2, warning_text=None, reaction_emoji="😡"
        )
        _invalidate_config_cache(context, chat_id)
        return await _show_settings(query, context, user_id, chat_id)

    # ── msg:<chat_id> — start custom-message conversation ────────────────────
    if data.startswith("msg:"):
        chat_id = int(data.split(":")[1])
        if not await _verify_admin(query, context, user_id, chat_id):
            return ConversationHandler.END
        context.user_data["editing_chat_id"] = chat_id
        context.user_data["editing_field"] = "msg"
        await query.edit_message_text(
            "Send the custom warning text for this group.\n"
            "Send /cancel to abort or /clear to restore the default text."
        )
        return _AWAIT_MSG_TEXT

    # ── reaction:<chat_id> — start reaction-emoji conversation ───────────────
    if data.startswith("reaction:"):
        chat_id = int(data.split(":")[1])
        if not await _verify_admin(query, context, user_id, chat_id):
            return ConversationHandler.END
        context.user_data["editing_chat_id"] = chat_id
        context.user_data["editing_field"] = "reaction"
        await query.edit_message_text(
            "Send the emoji you want the bot to react with.\n"
            "Note: not all emojis are allowed in all groups — if the reaction "
            "fails in practice, try a different one.\n"
            "Send /cancel to abort."
        )
        return _AWAIT_REACTION

    return None


async def _show_settings(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
) -> None:
    if not await _verify_admin(query, context, user_id, chat_id):
        return

    db = _db(context)
    cfg = ChatConfig.from_db_row(db.get_chat_config(chat_id))

    # Get chat title
    known = {c["chat_id"]: c for c in db.get_known_chats()}
    chat_info = known.get(chat_id, {})
    chat_title = chat_info.get("title") or f"Group {chat_id}"

    needs_perm_warn = cfg.mode in (3, 4) and not await _check_bot_can_delete(
        chat_id, context
    )

    text = _settings_text(cfg, chat_title, needs_perm_warn)
    keyboard = _settings_keyboard(cfg, chat_id)

    try:
        await query.edit_message_text(
            text, reply_markup=keyboard, parse_mode="HTML"
        )
    except BadRequest:
        pass  # Message unchanged — no edit needed


async def _verify_admin(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in ("administrator", "creator"):
            return True
    except (BadRequest, TelegramError):
        pass
    await query.answer(
        "You are not an admin in that group.", show_alert=True
    )
    return False


# ── ConversationHandler: custom warning text ──────────────────────────────────

async def receive_msg_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    chat_id = context.user_data.get("editing_chat_id")
    if chat_id is None:
        return ConversationHandler.END

    _db(context).set_chat_config(chat_id, warning_text=update.message.text.strip())
    _invalidate_config_cache(context, chat_id)
    await update.message.reply_text("✅ Warning text updated.")
    return ConversationHandler.END


async def clear_msg_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    chat_id = context.user_data.get("editing_chat_id")
    if chat_id:
        _db(context).set_chat_config(chat_id, warning_text=None)
        _invalidate_config_cache(context, chat_id)
    await update.message.reply_text("✅ Warning text reset to default.")
    return ConversationHandler.END


# ── ConversationHandler: reaction emoji ───────────────────────────────────────

async def receive_reaction(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    chat_id = context.user_data.get("editing_chat_id")
    if chat_id is None:
        return ConversationHandler.END

    emoji = update.message.text.strip()
    _db(context).set_chat_config(chat_id, reaction_emoji=emoji)
    _invalidate_config_cache(context, chat_id)
    await update.message.reply_text(f"✅ Reaction emoji set to {emoji}")
    return ConversationHandler.END


async def cancel_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.pop("editing_chat_id", None)
    context.user_data.pop("editing_field", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── Handler registration ──────────────────────────────────────────────────────

def build_admin_handlers() -> list:
    """
    Return all handlers for the admin interface.
    Call application.add_handler() on each one in bot.py main().
    The ConversationHandler must be registered before the plain CallbackQueryHandler.
    """
    private = filters.ChatType.PRIVATE

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callback, pattern=r"^(msg|reaction):")],
        states={
            _AWAIT_MSG_TEXT: [
                CommandHandler("clear", clear_msg_text),
                CommandHandler("cancel", cancel_conversation),
                MessageHandler(private & ~filters.COMMAND, receive_msg_text),
            ],
            _AWAIT_REACTION: [
                CommandHandler("cancel", cancel_conversation),
                MessageHandler(private & ~filters.COMMAND, receive_reaction),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_chat=True,
        per_user=True,
    )

    return [
        CommandHandler("start",    cmd_start,    filters=private),
        CommandHandler("settings", cmd_settings, filters=private),
        conv,
        CallbackQueryHandler(handle_callback, filters=private),
    ]
