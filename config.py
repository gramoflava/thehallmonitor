"""
config.py — Mode constants and per-group configuration dataclass.

Shared by bot.py and admin.py to ensure a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Action modes ──────────────────────────────────────────────────────────────

MODE_OFF            = 0   # Bot is silent in this group
MODE_REACT          = 1   # Bot reacts to the offending message with an emoji
MODE_REPLY          = 2   # Bot replies with a warning (default)
MODE_DELETE_SILENT  = 3   # Bot deletes the offending message silently
MODE_DELETE_NOTIFY  = 4   # Bot deletes + posts a brief notice

MODE_LABELS: dict[int, str] = {
    MODE_OFF:           "🚫 Off",
    MODE_REACT:         "😡 React",
    MODE_REPLY:         "💬 Reply (warn)",
    MODE_DELETE_SILENT: "🗑 Delete silently",
    MODE_DELETE_NOTIFY: "🗑 Delete + notify",
}

MODE_PERMISSIONS: dict[int, str] = {
    MODE_OFF:           "No special permissions needed",
    MODE_REACT:         "No special permissions needed",
    MODE_REPLY:         "No special permissions needed",
    MODE_DELETE_SILENT: "Requires: Delete messages",
    MODE_DELETE_NOTIFY: "Requires: Delete messages",
}

_ALL_MODES = list(MODE_LABELS.keys())

# Default warning emoji for react mode
DEFAULT_REACTION_EMOJI = "😡"

# Global fallback warning text (used when mode=reply and no custom text set)
DEFAULT_WARNING_TEXT = (
    "⚠️ This message may contain a reference to restricted "
    "materials. Please verify before sharing."
)


# ── Per-group config dataclass ────────────────────────────────────────────────

@dataclass
class ChatConfig:
    chat_id: int
    mode: int = MODE_REPLY
    warning_text: str | None = None          # None → use DEFAULT_WARNING_TEXT
    reaction_emoji: str = DEFAULT_REACTION_EMOJI

    def effective_warning_text(self) -> str:
        return self.warning_text or DEFAULT_WARNING_TEXT

    @classmethod
    def from_db_row(cls, row: dict) -> "ChatConfig":
        return cls(
            chat_id=row["chat_id"],
            mode=row.get("mode", MODE_REPLY),
            warning_text=row.get("warning_text"),
            reaction_emoji=row.get("reaction_emoji", DEFAULT_REACTION_EMOJI),
        )
