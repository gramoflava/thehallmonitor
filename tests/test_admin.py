"""
tests/test_admin.py — Unit tests for admin.py helpers.

Tests cover pure functions and DB-interaction helpers only.
No Telegram API calls are made.
"""

import pytest
from database import Database
from config import (
    ChatConfig,
    MODE_OFF,
    MODE_REACT,
    MODE_REPLY,
    MODE_DELETE_SILENT,
    MODE_DELETE_NOTIFY,
    MODE_LABELS,
    MODE_PERMISSIONS,
    DEFAULT_REACTION_EMOJI,
    DEFAULT_WARNING_TEXT,
)
from admin import _settings_text, _settings_keyboard


@pytest.fixture
def db():
    d = Database(":memory:")
    d.connect()
    d.create_schema()
    yield d
    d.close()


# ── ChatConfig dataclass ───────────────────────────────────────────────────────

def test_chatconfig_defaults():
    cfg = ChatConfig(chat_id=-100123)
    assert cfg.mode == MODE_REPLY
    assert cfg.warning_text is None
    assert cfg.reaction_emoji == DEFAULT_REACTION_EMOJI


def test_chatconfig_effective_warning_text_default():
    cfg = ChatConfig(chat_id=-100123)
    assert cfg.effective_warning_text() == DEFAULT_WARNING_TEXT


def test_chatconfig_effective_warning_text_custom():
    cfg = ChatConfig(chat_id=-100123, warning_text="Custom!")
    assert cfg.effective_warning_text() == "Custom!"


def test_chatconfig_from_db_row():
    row = {
        "chat_id": -100123,
        "mode": MODE_REACT,
        "warning_text": "Watch out!",
        "reaction_emoji": "🔥",
    }
    cfg = ChatConfig.from_db_row(row)
    assert cfg.chat_id == -100123
    assert cfg.mode == MODE_REACT
    assert cfg.warning_text == "Watch out!"
    assert cfg.reaction_emoji == "🔥"


def test_chatconfig_from_db_row_defaults_for_missing_fields():
    row = {"chat_id": -100123}
    cfg = ChatConfig.from_db_row(row)
    assert cfg.mode == MODE_REPLY
    assert cfg.reaction_emoji == DEFAULT_REACTION_EMOJI


# ── MODE_LABELS / MODE_PERMISSIONS ────────────────────────────────────────────

def test_all_modes_have_labels():
    for mode in [MODE_OFF, MODE_REACT, MODE_REPLY, MODE_DELETE_SILENT, MODE_DELETE_NOTIFY]:
        assert mode in MODE_LABELS, f"Mode {mode} missing from MODE_LABELS"


def test_all_modes_have_permissions():
    for mode in [MODE_OFF, MODE_REACT, MODE_REPLY, MODE_DELETE_SILENT, MODE_DELETE_NOTIFY]:
        assert mode in MODE_PERMISSIONS, f"Mode {mode} missing from MODE_PERMISSIONS"


def test_delete_modes_mention_permissions():
    for mode in [MODE_DELETE_SILENT, MODE_DELETE_NOTIFY]:
        assert "Delete" in MODE_PERMISSIONS[mode]


def test_non_delete_modes_need_no_permissions():
    for mode in [MODE_OFF, MODE_REACT, MODE_REPLY]:
        assert "No special" in MODE_PERMISSIONS[mode]


# ── _settings_text ─────────────────────────────────────────────────────────────

def test_settings_text_shows_mode_label():
    cfg = ChatConfig(chat_id=-100123, mode=MODE_REPLY)
    text = _settings_text(cfg, "My Group", False)
    assert "Reply" in text
    assert "My Group" in text


def test_settings_text_shows_permission_warning():
    cfg = ChatConfig(chat_id=-100123, mode=MODE_DELETE_NOTIFY)
    text = _settings_text(cfg, "G", True)
    assert "does not currently have" in text


def test_settings_text_no_permission_warning_when_ok():
    cfg = ChatConfig(chat_id=-100123, mode=MODE_DELETE_NOTIFY)
    text = _settings_text(cfg, "G", False)
    assert "does not currently have" not in text


def test_settings_text_shows_custom_warning():
    cfg = ChatConfig(chat_id=-100123, warning_text="Stay safe!")
    text = _settings_text(cfg, "G", False)
    assert "Stay safe!" in text


def test_settings_text_shows_default_placeholder():
    cfg = ChatConfig(chat_id=-100123, warning_text=None)
    text = _settings_text(cfg, "G", False)
    assert "(default)" in text


# ── _settings_keyboard ────────────────────────────────────────────────────────

def test_settings_keyboard_has_all_modes():
    cfg = ChatConfig(chat_id=-100123, mode=MODE_REPLY)
    kb = _settings_keyboard(cfg, -100123)
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    for label in MODE_LABELS.values():
        # Strip emoji prefix and check label fragment
        core = label.split(" ", 1)[-1]
        assert any(core in t for t in button_texts), f"{label} not found in keyboard"


def test_settings_keyboard_marks_current_mode():
    cfg = ChatConfig(chat_id=-100123, mode=MODE_OFF)
    kb = _settings_keyboard(cfg, -100123)
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    off_btn = next(t for t in button_texts if "Off" in t)
    assert off_btn.startswith("✓")


def test_settings_keyboard_only_one_checkmark():
    cfg = ChatConfig(chat_id=-100123, mode=MODE_REACT)
    kb = _settings_keyboard(cfg, -100123)
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    checkmarks = [t for t in button_texts if t.startswith("✓")]
    assert len(checkmarks) == 1


def test_settings_keyboard_callback_data_format():
    cfg = ChatConfig(chat_id=-100123, mode=MODE_REPLY)
    kb = _settings_keyboard(cfg, -100123)
    all_callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any(c.startswith("mode:-100123:") for c in all_callbacks)
    assert any(c == "msg:-100123" for c in all_callbacks)
    assert any(c == "reaction:-100123" for c in all_callbacks)
    assert any(c == "reset:-100123" for c in all_callbacks)


# ── DB integration: config round-trip ─────────────────────────────────────────

def test_config_roundtrip_via_db(db):
    db.set_chat_config(-100123, mode=MODE_REACT, reaction_emoji="🔥")
    row = db.get_chat_config(-100123)
    cfg = ChatConfig.from_db_row(row)
    assert cfg.mode == MODE_REACT
    assert cfg.reaction_emoji == "🔥"
