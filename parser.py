"""
parser.py — Convert a .doc file into a list of forbidden tokens.

Conversion chain:
  1. abiword headless: .doc → .docx, then python-docx table extraction
     (lightweight — ~30 MB RAM, suitable for low-memory servers)
  2. LibreOffice headless fallback (if abiword is not available)
  3. antiword fallback: .doc → plain text, treat each line as a candidate cell
  4. RuntimeError if all three fail

Exported public API:
  parse_doc_file(path)         → list of (raw_text, token, token_type)
  parse_cell_to_tokens(text)   → list of (raw_text, token, token_type)
  normalize_url(url)           → str  (MUST match matcher.py's copy exactly)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Words too generic to store as text tokens.
# This list is intentionally broad to prevent common English words appearing
# in book/channel titles from generating false positives in matching.
_STOPWORDS: frozenset[str] = frozenset(
    {
        # ── Common English words that appear in forbidden list titles ──────────
        "about", "after", "again", "against", "also", "another", "army",
        "back", "been", "being", "book", "both", "case", "come", "comes",
        "coming", "copy", "dark", "days", "dear", "dead", "does", "down",
        "each", "edition", "even", "ever", "every", "evil", "eyes",
        "face", "fact", "feel", "file", "find", "fire", "first", "five",
        "form", "four", "free", "from", "front", "full", "give", "goes",
        "good", "great", "hand", "hard", "have", "hear", "hello", "help",
        "here", "high", "home", "hope", "into", "just", "keep", "kill",
        "kind", "know", "land", "last", "late", "left", "less", "life",
        "like", "line", "list", "live", "long", "look", "love", "made",
        "make", "many", "mark", "mass", "media", "meet", "more", "most",
        "move", "much", "must", "name", "need", "news", "next", "night",
        "none", "note", "nothing", "noty", "only", "open", "over",
        "page", "part", "pass", "past", "plan", "play", "plus", "post",
        "power", "print", "race", "real", "rest", "right", "rise",
        "road", "role", "rule", "same", "save", "self", "send", "show",
        "side", "sign", "site", "size", "some", "soon", "stay", "step",
        "stop", "such", "take", "talk", "tell", "than", "that", "their",
        "them", "then", "there", "these", "they", "this", "time", "today",
        "told", "took", "turn", "type", "under", "unit", "upon", "used",
        "very", "view", "want", "warn", "week", "well", "were", "what",
        "when", "where", "which", "while", "will", "with", "word",
        "work", "world", "year", "your",
        # ── Structural/meta words ─────────────────────────────────────────────
        "channel", "youtube", "telegram", "https", "http", "www",
        "link", "page", "site", "list", "info", "news",
        "photo", "video", "image", "group", "chat", "room",
        # ── Platform names (bare name without domain is too broad) ────────────
        "facebook", "instagram", "linkedin", "twitter", "tiktok",
        "pinterest", "snapchat", "reddit", "discord", "twitch",
        "spotify", "patreon", "github", "medium",
        # ── Russian common words ──────────────────────────────────────────────
        "канал", "сайт", "ссылка", "список", "материал", "другие",
        "страница", "ресурс", "название", "описание", "адрес",
        "ссылки", "интернет", "сеть", "контент", "информация",
        "решение", "суда", "года", "вступило", "силу", "законную",
        "района", "города", "минска", "области", "флага",
        "надписью", "изображение", "символика", "атрибутика",
        "продукция", "материалов", "издания", "книга", "диск",
    }
)

# Major platforms where only specific paths/channels are forbidden —
# storing the bare domain would block the entire platform.
# The full URL token is still stored; only the domain extraction is skipped.
# Any subdomain of these (e.g. m.facebook.com, ru-ru.facebook.com) is also
# suppressed — checked via _is_platform_domain() below.
_PLATFORM_DOMAINS: frozenset[str] = frozenset({
    "youtube.com", "youtu.be",
    "facebook.com", "fb.com", "fb.me",
    "instagram.com",
    "twitter.com", "x.com",
    "tiktok.com",
    "vk.com", "vkontakte.ru",
    "ok.ru",                        # Odnoklassniki
    "linkedin.com",
    "reddit.com",
    "pinterest.com",
    "soundcloud.com",
    "spotify.com",
    "apple.com", "apps.apple.com",
    "play.google.com",
    "patreon.com",
    "twitch.tv",
    "discord.com", "discord.gg",
    "github.com",
    "medium.com",
    "wordpress.com",
    "blogspot.com",
    "livejournal.com",
})


def _is_platform_domain(domain: str) -> bool:
    """Return True if domain is a platform domain or any subdomain of one."""
    domain = domain.lower()
    if domain in _PLATFORM_DOMAINS:
        return True
    # Check subdomains: m.facebook.com, ru-ru.facebook.com, etc.
    for platform in _PLATFORM_DOMAINS:
        if domain.endswith("." + platform):
            return True
    return False

# Minimum char length for text-type tokens
_MIN_TEXT_LEN = 4

# Cyrillic character range for regex
_CYR = r"\u0430-\u044f\u0451\u0410-\u042f\u0401"


# ── URL normalization (identical copy must exist in matcher.py) ──────────────


def normalize_url(url: str) -> str:
    """
    Normalize a URL for storage and matching.
    Strip scheme, www prefix, trailing punctuation, and internal whitespace.
    """
    url = url.strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^ftp://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = url.rstrip("/.,;:)\"'")
    url = re.sub(r"\s+", "", url)  # "youtube.com/ xxxxx" → "youtube.com/xxxxx"
    return url


# Cyrillic homoglyphs that appear in copy-pasted or OCR'd URLs.
# Maps Cyrillic lookalike → correct Latin character.
_HOMOGLYPHS: dict[str, str] = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ј": "j",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "У": "Y", "Х": "X",
}
_HOMOGLYPH_RE = re.compile("[" + "".join(_HOMOGLYPHS) + "]")


def _fix_homoglyphs(s: str) -> str:
    return _HOMOGLYPH_RE.sub(lambda m: _HOMOGLYPHS[m.group()], s)


def _extract_domain(normalized_url: str) -> str:
    """Extract bare domain from an already-normalized URL."""
    domain = normalized_url.split("/")[0].split("?")[0]
    # Strip trailing dot (e.g. "instagram.com.")
    domain = domain.rstrip(".")
    # Replace Cyrillic homoglyphs (e.g. "tiktok.сom" → "tiktok.com")
    domain = _fix_homoglyphs(domain)
    # Reject domains containing @ (OCR artifact like "twitter.com@handle")
    if "@" in domain:
        return ""
    # Reject OCR artifacts like "youtube.com.channel" where a dot replaces a slash.
    # A real domain has its TLD as the very last label. If a known short TLD
    # (.com, .org, .net, .by, .ru, …) appears as a non-final label, it's junk.
    _COMMON_TLDS = {"com", "org", "net", "by", "ru", "io", "co", "me",
                    "tv", "fm", "app", "live", "online", "info", "biz"}
    parts = domain.split(".")
    # Any non-final label that looks like a TLD → reject
    for part in parts[:-1]:
        if part in _COMMON_TLDS:
            return ""
    return domain


# ── Cell-level token extraction ──────────────────────────────────────────────


def parse_cell_to_tokens(raw_text: str) -> list[tuple[str, str, str]]:
    """
    Extract forbidden tokens from one table cell (or one text line).
    Returns list of (raw_text, normalized_token, token_type).
    Deduplicates by (token, token_type).
    """
    text = raw_text.strip()
    # Skip empty cells and common placeholder values
    if not text or text in ("—", "–", "-", "−", "N/A", "н/д", "нд", ""):
        return []

    seen: set[tuple[str, str]] = set()
    results: list[tuple[str, str, str]] = []
    # We'll progressively blank out matched portions so text matching
    # at the end doesn't re-match things already handled.
    remaining = text

    def add(token: str, ttype: str) -> None:
        key = (token, ttype)
        if key not in seen and token:
            seen.add(key)
            results.append((raw_text, token, ttype))

    # ── 1. Full http/https/ftp URLs ──────────────────────────────────────────
    _http_re = re.compile(r"https?://[^\s,;\)\]\"\'<>]+", re.IGNORECASE)
    for m in _http_re.finditer(text):
        raw_url = m.group().rstrip(".,;)\"'")
        norm = normalize_url(raw_url)
        if norm:
            add(norm, "url")
            domain = _extract_domain(norm)
            if "." in domain and len(domain) > 3 and not _is_platform_domain(domain):
                add(domain, "domain")
        remaining = remaining.replace(m.group(), " ", 1)

    # ── 2. t.me/ bare links (no scheme) ─────────────────────────────────────
    _tme_re = re.compile(r"\bt\.me/@?([a-zA-Z0-9_]{3,})", re.IGNORECASE)
    for m in _tme_re.finditer(text):
        handle_part = m.group(1).lower()
        norm = "t.me/" + handle_part
        add(norm, "url")
        # t.me is a routing domain, not a platform — keep as domain token
        add("t.me", "domain")
        remaining = remaining.replace(m.group(), " ", 1)

    # ── 3. Bare domain URLs (no scheme): domain.tld/path ─────────────────────
    # Matches things like "facebook.com/pagename" or "www.youtube.com/channel/x"
    # that appear without https:// in the source document.
    # Must run before text extraction so the domain name isn't tokenised as text.
    _bare_re = re.compile(
        r"\b(?:www\.)?([a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
        r"(?:\.[a-zA-Z]{2,})+)"   # domain.tld (no scheme)
        r"(/[^\s,;\)\]\"\'<>]*)?",  # optional /path
        re.IGNORECASE,
    )
    for m in _bare_re.finditer(remaining):
        path = m.group(2) or ""
        norm = normalize_url(m.group())
        domain = _extract_domain(norm)
        if not domain or "." not in domain or len(domain) <= 3:
            continue
        if path:
            # Has a specific path — store the full URL
            add(norm, "url")
        # Store domain only if it's not a platform (to avoid blocking all of facebook.com)
        if not _is_platform_domain(domain):
            add(domain, "domain")
        remaining = remaining.replace(m.group(), " ", 1)

    # ── 4. @handles ──────────────────────────────────────────────────────────
    _handle_re = re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{3,})")
    for m in _handle_re.finditer(remaining):
        handle = "@" + m.group(1).lower()
        add(handle, "handle")
        remaining = remaining.replace(m.group(), " ", 1)

    # ── 5. Plain text names (what remains after stripping URLs + handles) ────
    # Latin words
    for word in re.findall(r"\b[a-zA-Z]{%d,}\b" % _MIN_TEXT_LEN, remaining):
        wl = word.lower()
        if wl not in _STOPWORDS:
            add(wl, "text")

    # Cyrillic words
    for word in re.findall(
        r"\b[%s]{%d,}\b" % (_CYR, _MIN_TEXT_LEN), remaining
    ):
        wl = word.lower()
        if wl not in _STOPWORDS:
            add(wl, "text")

    return results


# ── .doc → .docx conversion ──────────────────────────────────────────────────


def _convert_with_abiword(doc_path: str, outdir: str) -> str:
    """
    Convert .doc → .docx using abiword headless (~30 MB RAM).
    Returns the path to the resulting .docx file.
    """
    if not shutil.which("abiword"):
        raise RuntimeError("abiword not found in PATH")

    stem = Path(doc_path).stem
    docx_path = os.path.join(outdir, stem + ".docx")
    cmd = ["abiword", "--to=docx", f"--to-name={docx_path}", doc_path]
    logger.info("abiword: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"abiword exited {result.returncode}: {result.stderr[:600]}"
        )
    if not os.path.exists(docx_path):
        candidates = list(Path(outdir).glob("*.docx"))
        if not candidates:
            raise RuntimeError(
                f"abiword produced no .docx in {outdir}. "
                f"stdout={result.stdout[:300]}"
            )
        docx_path = str(candidates[0])
    logger.info("abiword output: %s", docx_path)
    return docx_path


def _convert_with_libreoffice(doc_path: str, outdir: str) -> str:
    """
    Convert .doc → .docx using LibreOffice headless (~500 MB RAM).
    Returns the path to the resulting .docx file.
    """
    soffice = None
    for candidate in [
        "soffice", "libreoffice",
        "/usr/bin/soffice", "/usr/bin/libreoffice",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]:
        if shutil.which(candidate) or os.path.isfile(candidate):
            soffice = candidate
            break
    if not soffice:
        raise RuntimeError("LibreOffice (soffice) not found in PATH")

    cmd = [
        soffice, "--headless", "--norestore", "--nofirststartwizard",
        "--convert-to", "docx:MS Word 2007 XML", "--outdir", outdir, doc_path,
    ]
    logger.info("LibreOffice: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(
            f"LibreOffice exited {result.returncode}: {result.stderr[:600]}"
        )
    stem = Path(doc_path).stem
    docx_path = os.path.join(outdir, stem + ".docx")
    if not os.path.exists(docx_path):
        candidates = list(Path(outdir).glob("*.docx"))
        if not candidates:
            raise RuntimeError(
                f"LibreOffice produced no .docx in {outdir}. "
                f"stdout={result.stdout[:300]}"
            )
        docx_path = str(candidates[0])
    logger.info("LibreOffice output: %s", docx_path)
    return docx_path


def _convert_doc_to_docx(doc_path: str, outdir: str) -> str:
    """Try abiword first (low RAM), fall back to LibreOffice."""
    try:
        return _convert_with_abiword(doc_path, outdir)
    except RuntimeError as e:
        logger.warning("abiword failed: %s — trying LibreOffice", e)
        return _convert_with_libreoffice(doc_path, outdir)


def _extract_cells_from_docx(docx_path: str) -> list[str]:
    """Extract all non-empty cell texts from all tables in a .docx."""
    import docx  # python-docx

    doc = docx.Document(docx_path)
    cells: list[str] = []
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                ct = cell.text.strip()
                if ct:
                    cells.append(ct)
    logger.info("Extracted %d cells from %s", len(cells), docx_path)
    return cells


# ── antiword fallback ────────────────────────────────────────────────────────


def _extract_lines_via_antiword(doc_path: str) -> list[str]:
    """
    Use antiword (default table-format output) to extract text from a .doc.

    antiword outputs table rows as pipe-delimited lines:
        |cell content         |cell content         |cell content|

    We split each such line on | and treat each non-empty segment as a cell.
    Non-table paragraphs are passed through as-is.

    Encoding note: antiword's output encoding depends on the system locale.
    On Linux (Docker, production) with en_US.UTF-8 locale it produces UTF-8.
    On macOS/other it may produce Latin-1 byte sequences for Cyrillic.
    We try UTF-8 first, fall back to Latin-1. URLs and @handles are ASCII
    and are preserved correctly under any single-byte encoding.
    """
    if not shutil.which("antiword"):
        raise RuntimeError("antiword not found in PATH")

    # ANTIWORDHOME tells antiword where to find its encoding mapping files.
    # Without it, antiword looks in $HOME/.antiword/ which may not exist
    # (e.g. when running as a non-login user in Docker).
    # -m UTF-8.txt forces UTF-8 output regardless of system locale — this
    # avoids "Can't set the UTF-8 locale" errors in slim Docker images
    # that don't have en_US.UTF-8 installed.
    antiword_home = os.environ.get("ANTIWORDHOME", "/usr/share/antiword")
    result = subprocess.run(
        ["antiword", "-m", "UTF-8.txt", doc_path],
        capture_output=True,
        timeout=120,
        env=dict(os.environ, ANTIWORDHOME=antiword_home),
    )
    text = result.stdout.decode("utf-8", errors="replace")

    cells: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            # Table row: split on | and collect non-empty segments
            for segment in line.split("|"):
                s = segment.strip()
                if s:
                    cells.append(s)
        else:
            cells.append(line)

    logger.info("antiword extracted %d cell segments", len(cells))
    return cells


# ── Public entry point ───────────────────────────────────────────────────────


def parse_doc_file(doc_path: str) -> list[tuple[str, str, str]]:
    """
    Full pipeline: .doc file path → list of (raw_text, token, token_type).

    1. Try abiword → .docx → python-docx table extraction.
    2. Fall back to LibreOffice → .docx → python-docx table extraction.
    3. Fall back to antiword plain-text extraction.
    4. Raise RuntimeError if all three fail.
    """
    cells: list[str] = []

    with tempfile.TemporaryDirectory(prefix="thehallmonitor_") as tmpdir:
        # Attempt 1: LibreOffice
        try:
            docx_path = _convert_doc_to_docx(doc_path, tmpdir)
            cells = _extract_cells_from_docx(docx_path)
            logger.info("doc→docx conversion succeeded (%d cells)", len(cells))
        except Exception as lo_err:
            logger.warning("doc→docx conversion failed: %s — trying antiword", lo_err)
            try:
                cells = _extract_lines_via_antiword(doc_path)
            except Exception as aw_err:
                raise RuntimeError(
                    f"All converters failed.\n"
                    f"  abiword/LibreOffice: {lo_err}\n"
                    f"  antiword:           {aw_err}"
                ) from aw_err

        # Parse every cell
        all_tokens: list[tuple[str, str, str]] = []
        for cell in cells:
            all_tokens.extend(parse_cell_to_tokens(cell))

    # Global deduplication
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str, str]] = []
    for raw, token, ttype in all_tokens:
        key = (token, ttype)
        if key not in seen:
            seen.add(key)
            unique.append((raw, token, ttype))

    logger.info("Total unique tokens: %d", len(unique))
    return unique
