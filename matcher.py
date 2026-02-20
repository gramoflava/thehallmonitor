"""
matcher.py — Extract tokens from a Telegram message and match against the DB.

Also works as a standalone CLI tool:
    python matcher.py "message text to check"
    python matcher.py "@weedsmokers shared this"
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Must be identical to normalize_url() in parser.py
def normalize_url(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^ftp://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = url.rstrip("/.,;:)\"'")
    url = re.sub(r"\s+", "", url)
    return url


_MIN_WORD_LEN = 4
_CYR = r"\u0430-\u044f\u0451\u0410-\u042f\u0401"
_WORD_RE = re.compile(
    r"\b(?:[a-zA-Z]{%d,}|[%s]{%d,})\b" % (_MIN_WORD_LEN, _CYR, _MIN_WORD_LEN)
)


@dataclass
class MatchResult:
    token_type: str   # 'url' | 'domain' | 'handle' | 'text'
    token: str        # the forbidden token from the database
    found_in: str     # the substring from the message that triggered it


class Matcher:
    """
    Holds forbidden token sets in memory for fast per-message checks.
    Call load_from_db() at startup and after each daily update.
    """

    def __init__(self) -> None:
        self.urls: set[str] = set()
        self.domains: set[str] = set()
        self.handles: set[str] = set()
        # texts is a list so we can sort by length (longer tokens first)
        self.texts: list[str] = []

    def load_from_db(self, db: object) -> None:
        """Load all token sets from a connected Database instance."""
        grouped = db.get_all_tokens_by_type()  # type: ignore[attr-defined]
        self.urls = grouped["url"]
        self.domains = grouped["domain"]
        self.handles = grouped["handle"]
        # Sort descending by length: longer names match before their substrings
        self.texts = sorted(grouped["text"], key=len, reverse=True)
        logger.info(
            "Matcher loaded: %d urls, %d domains, %d handles, %d text tokens",
            len(self.urls), len(self.domains),
            len(self.handles), len(self.texts),
        )

    def is_loaded(self) -> bool:
        return bool(self.urls or self.domains or self.handles or self.texts)

    def check_message(self, text: str | None) -> list[MatchResult]:
        """
        Extract all tokens from text and return any matches against
        the forbidden list. Returns empty list if no violations found.
        """
        if not text:
            return []

        text = text[:4096]  # Telegram hard message limit
        results: list[MatchResult] = []
        seen: set[tuple[str, str]] = set()

        def add(ttype: str, token: str, found: str) -> None:
            key = (ttype, token)
            if key not in seen:
                seen.add(key)
                results.append(MatchResult(ttype, token, found))

        # ── 1. Full http/https URLs ──────────────────────────────────────────
        _http_re = re.compile(r"https?://[^\s,;\)\]\"\'<>]+", re.IGNORECASE)
        for m in _http_re.finditer(text):
            raw = m.group().rstrip(".,;)\"'")
            norm = normalize_url(raw)
            if norm in self.urls:
                add("url", norm, raw)
            domain = norm.split("/")[0]
            if domain in self.domains:
                add("domain", domain, raw)

        # ── 2. t.me/ bare links (no scheme) ─────────────────────────────────
        _tme_re = re.compile(r"\bt\.me/@?([a-zA-Z0-9_]{3,})", re.IGNORECASE)
        for m in _tme_re.finditer(text):
            handle_part = m.group(1).lower()
            norm = "t.me/" + handle_part
            if norm in self.urls:
                add("url", norm, m.group())
            if "t.me" in self.domains:
                add("domain", "t.me", m.group())

        # ── 3. youtube.com/ bare links ───────────────────────────────────────
        _yt_re = re.compile(
            r"\b(?:www\.)?youtube\.com/[^\s,;\)\]\"\'<>]+", re.IGNORECASE
        )
        for m in _yt_re.finditer(text):
            norm = normalize_url(m.group())
            if norm in self.urls:
                add("url", norm, m.group())
            if "youtube.com" in self.domains:
                add("domain", "youtube.com", m.group())

        # ── 4. @handles ──────────────────────────────────────────────────────
        _handle_re = re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{3,})")
        for m in _handle_re.finditer(text):
            handle = "@" + m.group(1).lower()
            if handle in self.handles:
                add("handle", handle, m.group())

        # ── 5. Text name matching ─────────────────────────────────────────────
        # Extract all words ≥4 chars from the message (Latin + Cyrillic)
        message_words = [m.group().lower() for m in _WORD_RE.finditer(text)]
        message_word_set = set(message_words)

        # Exact word match: the forbidden name must appear as a standalone word.
        # Substring matching would generate too many false positives for
        # short common words (e.g. "news", "world") that appear in book titles.
        for forbidden_name in self.texts:
            if forbidden_name in message_word_set:
                add("text", forbidden_name, forbidden_name)
                continue
            # Also check if the forbidden name is a multi-word phrase —
            # these are stored as space-joined tokens; check as substring
            # of the original text (case-insensitive) only if the token
            # contains a space (i.e. it's a phrase, not a single word).
            if " " in forbidden_name and forbidden_name in text.lower():
                add("text", forbidden_name, forbidden_name)

        return results


# ── CLI entry point ───────────────────────────────────────────────────────────


def _cli_main() -> None:
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from database import Database, DB_PATH

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python matcher.py \"message text to check\"")
        sys.exit(1)

    text = " ".join(sys.argv[1:])

    with Database(DB_PATH) as db:
        last_updated = db.get_metadata("last_updated")
        if not last_updated:
            print("ERROR: Database is empty. Run 'python updater.py' first.")
            sys.exit(1)
        m = Matcher()
        m.load_from_db(db)
        counts = db.count_tokens()

    print(f"DB last updated : {last_updated}")
    print(f"Tokens in DB    : {sum(counts.values())} "
          f"({', '.join(f'{t}={n}' for t, n in sorted(counts.items()))})")
    print(f"Checking        : {text!r}")
    print()

    results = m.check_message(text)
    if results:
        print(f"MATCHES FOUND ({len(results)}):")
        for r in results:
            print(f"  [{r.token_type:6s}] forbidden={r.token!r}  "
                  f"found_in={r.found_in!r}")
    else:
        print("No matches found — message appears clean.")


if __name__ == "__main__":
    _cli_main()
