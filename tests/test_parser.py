import pytest
from parser import normalize_url, parse_cell_to_tokens


# ── normalize_url ────────────────────────────────────────────────────────────

def test_normalize_strips_https():
    assert normalize_url("https://example.com/path") == "example.com/path"


def test_normalize_strips_http():
    assert normalize_url("http://example.com/path") == "example.com/path"


def test_normalize_strips_www():
    assert normalize_url("https://www.example.com/") == "example.com"


def test_normalize_trailing_slash():
    assert normalize_url("https://example.com/path/") == "example.com/path"


def test_normalize_trailing_punct():
    assert normalize_url("https://example.com/path.") == "example.com/path"
    assert normalize_url("https://example.com/path,") == "example.com/path"


def test_normalize_collapses_spaces():
    # Broken URLs like "youtube.com/ xxxxx" should collapse
    assert normalize_url("youtube.com/ xxxxx") == "youtube.com/xxxxx"


def test_normalize_lowercase():
    assert normalize_url("HTTPS://EXAMPLE.COM/PATH") == "example.com/path"


def test_normalize_ftp():
    assert normalize_url("ftp://files.example.com/") == "files.example.com"


# ── parse_cell_to_tokens ─────────────────────────────────────────────────────

def _types_tokens(tokens):
    """Return set of (type, token) tuples for easy assertion."""
    return {(t, tok) for _, tok, t in tokens}


def test_cell_http_url():
    tokens = parse_cell_to_tokens("https://t.me/weedsmokers")
    tt = _types_tokens(tokens)
    assert ("url", "t.me/weedsmokers") in tt
    assert ("domain", "t.me") in tt


def test_cell_bare_tme():
    tokens = parse_cell_to_tokens("t.me/weedsmokers")
    tt = _types_tokens(tokens)
    assert ("url", "t.me/weedsmokers") in tt
    assert ("domain", "t.me") in tt


def test_cell_tme_with_at_in_path():
    # t.me/@channel_name — @ should be stripped from path
    tokens = parse_cell_to_tokens("t.me/@weedsmokers")
    tt = _types_tokens(tokens)
    assert ("url", "t.me/weedsmokers") in tt


def test_cell_handle():
    tokens = parse_cell_to_tokens("@weedsmokers")
    tt = _types_tokens(tokens)
    assert ("handle", "@weedsmokers") in tt


def test_cell_handle_uppercase_normalized():
    tokens = parse_cell_to_tokens("@Weedsmokers")
    tt = _types_tokens(tokens)
    assert ("handle", "@weedsmokers") in tt


def test_cell_youtube_bare():
    tokens = parse_cell_to_tokens("youtube.com/channel/UCxxxxxx")
    tt = _types_tokens(tokens)
    assert ("url", "youtube.com/channel/ucxxxxxx") in tt
    # youtube.com is a platform domain — full URL stored but bare domain is not
    assert ("domain", "youtube.com") not in tt


def test_cell_youtube_with_www():
    tokens = parse_cell_to_tokens("www.youtube.com/c/testchannel")
    tt = _types_tokens(tokens)
    assert ("url", "youtube.com/c/testchannel") in tt


def test_cell_latin_text_name():
    tokens = parse_cell_to_tokens("Badactor channel")
    tt = _types_tokens(tokens)
    assert ("text", "badactor") in tt
    # "channel" is a stopword — should not appear
    assert ("text", "channel") not in tt


def test_cell_cyrillic_text():
    tokens = parse_cell_to_tokens("Запрещено")
    tt = _types_tokens(tokens)
    assert ("text", "запрещено") in tt


def test_cell_mixed_cyrillic_latin():
    tokens = parse_cell_to_tokens("Запрещено (Badactor)")
    tt = _types_tokens(tokens)
    assert ("text", "badactor") in tt
    assert ("text", "запрещено") in tt


def test_cell_empty():
    assert parse_cell_to_tokens("") == []


def test_cell_placeholder_dash():
    assert parse_cell_to_tokens("—") == []
    assert parse_cell_to_tokens("–") == []
    assert parse_cell_to_tokens("-") == []


def test_cell_na():
    assert parse_cell_to_tokens("N/A") == []
    assert parse_cell_to_tokens("н/д") == []


def test_cell_deduplicates_same_token():
    # Same URL mentioned twice in same cell
    tokens = parse_cell_to_tokens(
        "https://t.me/weedsmokers and also https://t.me/weedsmokers"
    )
    url_tokens = [tok for _, tok, t in tokens if t == "url" and tok == "t.me/weedsmokers"]
    assert len(url_tokens) == 1


def test_cell_multiple_handles():
    tokens = parse_cell_to_tokens("@handle_one, @handle_two")
    tt = _types_tokens(tokens)
    assert ("handle", "@handle_one") in tt
    assert ("handle", "@handle_two") in tt


def test_cell_short_words_ignored():
    # Words shorter than MIN_TEXT_LEN=4 should not become text tokens
    tokens = parse_cell_to_tokens("go to war")
    text_tokens = [tok for _, tok, t in tokens if t == "text"]
    assert "go" not in text_tokens
    assert "to" not in text_tokens
    assert "war" not in text_tokens  # 3 chars — below threshold


def test_cell_full_url_plus_handle():
    tokens = parse_cell_to_tokens("https://t.me/weedsmokers @weedsmokers")
    tt = _types_tokens(tokens)
    assert ("url", "t.me/weedsmokers") in tt
    assert ("handle", "@weedsmokers") in tt
