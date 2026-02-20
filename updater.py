"""
updater.py — Fetch and parse the remote forbidden-materials list.

Downloads the .doc index page, finds the current .doc link, downloads
the file, parses it into forbidden tokens, and atomically replaces the
SQLite database content.

Standalone usage:
    python updater.py             # skip if doc URL unchanged
    python updater.py --force     # always re-download
    python updater.py --db /tmp/test.db
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

from dotenv import load_dotenv
import requests as _requests

load_dotenv()

logger = logging.getLogger(__name__)

# ── Deployment configuration (set in .env) ────────────────────────────────────

_SOURCE_PAGE = os.environ.get("INDEX_PAGE", "")
_BASE_URL    = os.environ.get("BASE_URL", "")
_raw_re      = os.environ.get("DOC_LINK_RE", r'href=["\'](/[^"\']+\.doc)["\']')
_DOC_LINK_RE = re.compile(_raw_re, re.IGNORECASE)

if not _SOURCE_PAGE or not _BASE_URL:
    raise RuntimeError(
        "INDEX_PAGE and BASE_URL must be set in the environment (or .env file)."
    )

_DOWNLOAD_TIMEOUT = 180   # seconds
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; thehallmonitor/1.0)"
    )
}


# ── Fetch logic ───────────────────────────────────────────────────────────────


def fetch_doc_url() -> str:
    """
    Scrape the index page and return the current .doc download URL.
    Raises RuntimeError if no link is found.
    """
    logger.info("Fetching index page: %s", _SOURCE_PAGE)
    resp = _requests.get(_SOURCE_PAGE, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    matches = _DOC_LINK_RE.findall(html)
    if not matches:
        raise RuntimeError(
            "Could not find a .doc download link on the index page. "
            "The page structure may have changed."
        )
    doc_path = matches[0]
    full_url = _BASE_URL + doc_path
    logger.info("Found doc URL: %s", full_url)
    return full_url


def download_doc(url: str, dest_path: str) -> str:
    """
    Download the .doc file to dest_path.
    Returns the SHA-256 hex digest of the downloaded content.
    Uses streaming to avoid loading the full 10 MB into memory.
    """
    logger.info("Downloading %s -> %s", url, dest_path)
    sha256 = hashlib.sha256()
    total = 0
    with _requests.get(
        url, headers=_HEADERS, stream=True, timeout=_DOWNLOAD_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                sha256.update(chunk)
                total += len(chunk)
    logger.info("Downloaded %.2f MB", total / 1024 / 1024)
    return sha256.hexdigest()


# ── Main pipeline ─────────────────────────────────────────────────────────────


def run_update(db_path: str | None = None, force: bool = False) -> dict:
    """
    Full update pipeline.

    Returns a stats dict with keys:
        success       bool
        skipped       bool   (True if URL unchanged and force=False)
        doc_url       str
        tokens_total  int
        tokens_by_type dict[str, int]
        timestamp     str (ISO-8601 UTC)
        error         str | None
    """
    from database import Database, DB_PATH
    from parser import parse_doc_file

    db_path = db_path or DB_PATH

    stats: dict = {
        "success": False,
        "skipped": False,
        "doc_url": None,
        "tokens_total": 0,
        "tokens_by_type": {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    try:
        with Database(db_path) as db:
            db.create_schema()

            # Step 1: Discover current doc URL
            doc_url = fetch_doc_url()
            stats["doc_url"] = doc_url

            # Step 2: Skip if URL unchanged (unless --force)
            if not force:
                stored_url = db.get_metadata("last_doc_url")
                if stored_url == doc_url:
                    logger.info("Doc URL unchanged — skipping download.")
                    stats["skipped"] = True
                    stats["success"] = True
                    counts = db.count_tokens()
                    stats["tokens_total"] = sum(counts.values())
                    stats["tokens_by_type"] = counts
                    return stats

            # Step 3: Download to a temp file
            with tempfile.NamedTemporaryFile(
                suffix=".doc", delete=False, prefix="thehallmonitor_"
            ) as tmp:
                tmp_path = tmp.name

            try:
                file_hash = download_doc(doc_url, tmp_path)

                # Step 4: Parse
                logger.info("Parsing document...")
                token_rows = parse_doc_file(tmp_path)

                # Step 5: Atomic DB replacement
                db.replace_all_tokens(token_rows)

                # Step 6: Update metadata
                db.set_metadata("last_doc_url", doc_url)
                db.set_metadata("last_doc_hash", file_hash)
                db.set_metadata("last_updated", stats["timestamp"])

                counts = db.count_tokens()
                stats["tokens_total"] = sum(counts.values())
                stats["tokens_by_type"] = counts
                stats["success"] = True
                logger.info("Update complete. Token counts: %s", counts)

                # Save monthly snapshot for /status delta display
                month_key = datetime.now(timezone.utc).strftime("token_count_%Y-%m")
                db.set_metadata(month_key, str(stats["tokens_total"]))

            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    except Exception as exc:
        stats["error"] = str(exc)
        logger.error("Update failed: %s", exc, exc_info=True)

    return stats


# ── CLI entry point ───────────────────────────────────────────────────────────


def _cli_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Update the forbidden-materials database from the configured source"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the doc URL has not changed since last run",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (default: data/forbidden.db)",
    )
    args = parser.parse_args()

    stats = run_update(db_path=args.db, force=args.force)

    print()
    print("=" * 40)
    print("Update Results")
    print("=" * 40)
    print(f"Timestamp : {stats['timestamp']}")
    print(f"Doc URL   : {stats['doc_url']}")
    print(f"Success   : {stats['success']}")
    print(f"Skipped   : {stats['skipped']}")
    if stats.get("error"):
        print(f"Error     : {stats['error']}")
    print(f"Total tokens: {stats['tokens_total']}")
    for ttype, count in sorted(stats.get("tokens_by_type", {}).items()):
        print(f"  {ttype:8s}: {count}")
    print()

    sys.exit(0 if stats["success"] else 1)


if __name__ == "__main__":
    _cli_main()
