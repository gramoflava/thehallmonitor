import pytest
from database import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.connect()
    d.create_schema()
    yield d
    d.close()


def test_insert_and_retrieve(db):
    rows = [
        ("@weedsmokers", "@weedsmokers", "handle"),
        ("https://t.me/weedsmokers", "t.me/weedsmokers", "url"),
        ("Testchan", "testchan", "text"),
        ("t.me", "t.me", "domain"),
    ]
    db.replace_all_tokens(rows)
    grouped = db.get_all_tokens_by_type()
    assert "@weedsmokers" in grouped["handle"]
    assert "t.me/weedsmokers" in grouped["url"]
    assert "testchan" in grouped["text"]
    assert "t.me" in grouped["domain"]


def test_deduplication(db):
    rows = [
        ("Testchan", "testchan", "text"),
        ("NEXTA", "testchan", "text"),  # same normalized token — should be ignored
    ]
    db.replace_all_tokens(rows)
    grouped = db.get_all_tokens_by_type()
    assert len(grouped["text"]) == 1


def test_replace_all_is_atomic(db):
    db.replace_all_tokens([("old", "oldtoken", "text")])
    assert "oldtoken" in db.get_all_tokens_by_type()["text"]

    db.replace_all_tokens([("new", "newtoken", "text")])
    grouped = db.get_all_tokens_by_type()
    assert "oldtoken" not in grouped["text"]
    assert "newtoken" in grouped["text"]


def test_replace_all_empty(db):
    db.replace_all_tokens([("a", "tok", "text")])
    db.replace_all_tokens([])
    grouped = db.get_all_tokens_by_type()
    assert len(grouped["text"]) == 0


def test_metadata_set_and_get(db):
    db.set_metadata("last_updated", "2025-01-01T00:00:00")
    assert db.get_metadata("last_updated") == "2025-01-01T00:00:00"


def test_metadata_overwrite(db):
    db.set_metadata("key", "v1")
    db.set_metadata("key", "v2")
    assert db.get_metadata("key") == "v2"


def test_metadata_missing_key(db):
    assert db.get_metadata("nonexistent") is None


def test_count_tokens(db):
    db.replace_all_tokens([
        ("a", "tok1", "text"),
        ("b", "tok2", "handle"),
        ("c", "tok3", "url"),
        ("d", "tok4", "domain"),
    ])
    counts = db.count_tokens()
    assert counts.get("text") == 1
    assert counts.get("handle") == 1
    assert counts.get("url") == 1
    assert counts.get("domain") == 1


def test_count_tokens_empty(db):
    counts = db.count_tokens()
    assert counts == {}


def test_all_types_present_in_result(db):
    # Even with no tokens, all four keys should exist in get_all_tokens_by_type
    grouped = db.get_all_tokens_by_type()
    assert set(grouped.keys()) == {"url", "domain", "handle", "text"}


# ── known_chats ───────────────────────────────────────────────────────────────

def test_upsert_known_chat(db):
    db.upsert_known_chat(-100123, "Test Group", "testgroup")
    chats = db.get_known_chats()
    assert any(c["chat_id"] == -100123 for c in chats)


def test_upsert_known_chat_updates_title(db):
    db.upsert_known_chat(-100123, "Old Title", None)
    db.upsert_known_chat(-100123, "New Title", "newuser")
    chats = db.get_known_chats()
    chat = next(c for c in chats if c["chat_id"] == -100123)
    assert chat["title"] == "New Title"
    assert chat["username"] == "newuser"


def test_get_known_chats_empty(db):
    assert db.get_known_chats() == []


# ── chat_config ───────────────────────────────────────────────────────────────

def test_get_chat_config_defaults(db):
    cfg = db.get_chat_config(-100999)
    assert cfg["mode"] == 2
    assert cfg["warning_text"] is None
    assert cfg["reaction_emoji"] == "😡"


def test_set_chat_config_mode(db):
    db.upsert_known_chat(-100123, "G", None)
    db.set_chat_config(-100123, mode=0)
    cfg = db.get_chat_config(-100123)
    assert cfg["mode"] == 0


def test_set_chat_config_creates_known_chat_if_missing(db):
    # set_chat_config must work even if upsert_known_chat was never called
    db.set_chat_config(-100456, mode=1, reaction_emoji="🔥")
    cfg = db.get_chat_config(-100456)
    assert cfg["mode"] == 1
    assert cfg["reaction_emoji"] == "🔥"


def test_set_chat_config_warning_text(db):
    db.set_chat_config(-100123, warning_text="Custom warning!")
    assert db.get_chat_config(-100123)["warning_text"] == "Custom warning!"


def test_set_chat_config_reset_warning_text(db):
    db.set_chat_config(-100123, warning_text="Custom")
    db.set_chat_config(-100123, warning_text=None)
    assert db.get_chat_config(-100123)["warning_text"] is None


def test_set_chat_config_ignores_unknown_fields(db):
    # Should not raise even with unexpected kwargs
    db.set_chat_config(-100123, mode=2, unknown_field="x")
    assert db.get_chat_config(-100123)["mode"] == 2


# ── violations ────────────────────────────────────────────────────────────────

def test_record_and_count_violations(db):
    db.upsert_known_chat(-100123, "G", None)
    db.record_violation(-100123, ["url", "handle"])
    db.record_violation(-100123, ["text"])
    stats = db.get_violation_stats(-100123)
    assert stats["total"] == 2


def test_violation_stats_empty(db):
    stats = db.get_violation_stats(-100999)
    assert stats["total"] == 0
    assert stats["last_occurred_at"] is None


def test_violation_priority_handle_over_url(db):
    db.record_violation(-100123, ["url", "handle"])
    stats = db.get_violation_stats(-100123)
    assert "handle" in stats["by_type"]


def test_violation_by_type_breakdown(db):
    db.record_violation(-100123, ["url"])
    db.record_violation(-100123, ["url"])
    db.record_violation(-100123, ["text"])
    stats = db.get_violation_stats(-100123)
    assert stats["by_type"].get("url") == 2
    assert stats["by_type"].get("text") == 1
