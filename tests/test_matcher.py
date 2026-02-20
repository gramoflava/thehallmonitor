import pytest
from database import Database
from matcher import Matcher


@pytest.fixture
def matcher():
    db = Database(":memory:")
    db.connect()
    db.create_schema()
    db.replace_all_tokens([
        # URLs
        ("https://t.me/weedsmokers", "t.me/weedsmokers", "url"),
        ("https://t.me/some_banned", "t.me/some_banned", "url"),
        # Domains
        ("t.me", "t.me", "domain"),
        ("badsite.example", "badsite.example", "domain"),
        # Handles
        ("@weedsmokers raw", "@weedsmokers", "handle"),
        ("@bannedchan raw", "@bannedchan", "handle"),
        # Text names
        ("Testchan channel", "testchan", "text"),
        ("Запрещено", "запрещено", "text"),
        ("Badactor", "badactor", "text"),
    ])
    m = Matcher()
    m.load_from_db(db)
    db.close()
    return m


# ── Handle matching ───────────────────────────────────────────────────────────

def test_handle_exact_match(matcher):
    r = matcher.check_message("Follow @weedsmokers for news")
    assert any(x.token_type == "handle" and x.token == "@weedsmokers" for x in r)


def test_handle_case_insensitive(matcher):
    r = matcher.check_message("Follow @Weedsmokers")
    assert any(x.token_type == "handle" and x.token == "@weedsmokers" for x in r)


def test_handle_not_matched_unknown(matcher):
    r = matcher.check_message("Follow @totally_random_user")
    assert not any(x.token_type == "handle" for x in r)


# ── URL matching ──────────────────────────────────────────────────────────────

def test_full_https_url(matcher):
    r = matcher.check_message("See https://t.me/weedsmokers now")
    assert any(x.token_type == "url" and "weedsmokers" in x.token for x in r)


def test_bare_tme_link(matcher):
    r = matcher.check_message("See t.me/weedsmokers today")
    assert any(x.token_type == "url" for x in r)


def test_domain_match_via_url(matcher):
    r = matcher.check_message("Check https://t.me/somethingelse")
    assert any(x.token_type == "domain" and x.token == "t.me" for x in r)


def test_non_platform_domain_match(matcher):
    r = matcher.check_message("Check https://badsite.example/some/path")
    assert any(x.token_type == "domain" and x.token == "badsite.example" for x in r)


def test_youtube_domain_not_matched(matcher):
    # youtube.com is a platform — sharing a YouTube link should not trigger
    # a domain match (only a specific forbidden URL would match)
    r = matcher.check_message("Watch https://youtube.com/watch?v=abc123")
    assert not any(x.token_type == "domain" and x.token == "youtube.com" for x in r)


# ── Text name matching ────────────────────────────────────────────────────────

def test_text_latin_case_insensitive(matcher):
    r = matcher.check_message("Follow Testchan for news")
    assert any(x.token_type == "text" and x.token == "testchan" for x in r)


def test_text_cyrillic(matcher):
    r = matcher.check_message("Подпишитесь на Запрещено")
    assert any(x.token_type == "text" and x.token == "запрещено" for x in r)


def test_text_name_exact_word_match(matcher):
    # "badactor" should match as an exact word
    r = matcher.check_message("I watch badactor every day")
    assert any(x.token_type == "text" and x.token == "badactor" for x in r)


def test_text_name_no_substring_match(matcher):
    # "badactor" should NOT match inside "badactornews" (substring only)
    r = matcher.check_message("I watch badactornews all day")
    assert not any(x.token_type == "text" and x.token == "badactor" for x in r)


# ── Clean messages ────────────────────────────────────────────────────────────

def test_clean_message_no_match(matcher):
    assert matcher.check_message("Hello world, nothing to see here!") == []


def test_empty_string(matcher):
    assert matcher.check_message("") == []


def test_none(matcher):
    assert matcher.check_message(None) == []


# ── Deduplication ─────────────────────────────────────────────────────────────

def test_no_duplicate_results_same_token(matcher):
    r = matcher.check_message("@weedsmokers and @weedsmokers again")
    handle_hits = [x for x in r if x.token_type == "handle" and x.token == "@weedsmokers"]
    assert len(handle_hits) == 1


def test_no_duplicate_domain_from_two_urls(matcher):
    # Two different URLs on the same forbidden domain → domain matched once
    r = matcher.check_message(
        "https://badsite.example/page1 and https://badsite.example/page2"
    )
    domain_hits = [x for x in r if x.token_type == "domain" and x.token == "badsite.example"]
    assert len(domain_hits) == 1


# ── Multiple matches ──────────────────────────────────────────────────────────

def test_multiple_different_violations(matcher):
    r = matcher.check_message("@weedsmokers posted https://t.me/some_banned")
    types = {x.token_type for x in r}
    # At minimum handle + url should both trigger
    assert "handle" in types
    assert "url" in types


# ── Forwarded channel simulation ──────────────────────────────────────────────

def test_forwarded_channel_appended(matcher):
    # bot.py appends "@channel_username" to the content string
    # Simulate that here by including it in the text
    r = matcher.check_message("Some content here @weedsmokers")
    assert any(x.token_type == "handle" for x in r)


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_long_message_truncated(matcher):
    # 5000-char message should still work (truncated to 4096)
    long_msg = "x" * 4500 + " @weedsmokers"
    # The @weedsmokers is beyond char 4096, so it should NOT match
    r = matcher.check_message(long_msg)
    assert not any(x.token_type == "handle" for x in r)


def test_tme_with_at_in_path(matcher):
    # t.me/@weedsmokers (@ in path) should still match t.me/weedsmokers URL
    r = matcher.check_message("See t.me/@weedsmokers")
    assert any(x.token_type == "url" and "weedsmokers" in x.token for x in r)
