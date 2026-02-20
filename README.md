# thehallmonitor

A Telegram bot for school (or any other) group chats that monitors messages
against a configurable forbidden-materials list. Group admins choose how the
bot responds, per group, via a private chat with the bot.

---

## How it works

Two processes run independently:

**Updater** (runs once a day, or manually)
1. Fetches an index page from a configured web source
2. Downloads the current `.doc` file (may be large — streamed in chunks)
3. Parses all URLs, Telegram handles (`@channel`), domain names, and
   plain-text names out of every table cell
4. Atomically replaces the SQLite database with the new token set
5. Skips the download if the source URL has not changed since the last run

**Bot** (runs continuously)
1. Reads every message in every group it is a member of
2. Extracts URLs, handles, domains, and words from the message text
3. Also inspects forwarded-message origin (channel username and title)
4. If any token matches the forbidden list → acts according to the group's
   configured **mode** (see below)
5. On message edit → re-checks; if now clean → removes the bot's reaction
   or warning reply
6. On reaction to the bot's warning → removes the warning (covers undetectable
   message deletion — see details below)
7. Runs the updater automatically once a day at 04:00 UTC

---

## Action modes

Each group can be independently configured to one of five modes:

| Mode | What the bot does | Bot permissions required |
|---|---|---|
| **Off** | Does nothing | None |
| **React** | Sets a configurable emoji reaction on the offending message | None |
| **Reply** *(default)* | Replies with a warning message | None |
| **Delete silently** | Deletes the offending message without comment | Delete messages |
| **Delete + notify** | Deletes the message and posts a brief notice: *"ℹ️ A message by [Name] was removed."* | Delete messages |

Per-group settings also include:
- **Warning text** — custom reply text for Reply mode (falls back to a default)
- **Reaction emoji** — which emoji to use in React mode (default: 😡)

---

## Warning cleanup

The bot removes its own warnings automatically in two ways:

**User edits their message**
The bot re-checks the edited content. If the violation is gone, the bot removes
its reaction or deletes its warning reply. If the violation is still present, the
warning stays.

**User reacts to the bot's warning**
Because Telegram does not send events when a message is deleted, any reaction on
the bot's warning message is treated as a "this has been handled" signal: the bot
removes its warning. This keeps group chat tidy even when the original message
was deleted rather than edited.

---

## Per-group configuration (admins only)

Group admins configure the bot in a **private chat** — no other group members
see the interaction.

1. Add the bot to a group and make it an admin (required permissions depend on
   the chosen mode).
2. Open a private chat with the bot and send `/settings` (or `/start`).
3. The bot lists only the groups where you are an admin.
4. Tap a group → see its current settings and action buttons.
5. Change the mode, custom message, or reaction emoji instantly.

The settings menu shows which permissions are required for the selected mode
and warns you if the bot doesn't currently have them.

---

## Group commands (available to all members)

| Command | Description |
|---|---|
| `/status` | Bot's current mode, token count, last list update, and month-over-month change |
| `/stats` | Violation activity for the past 30 days — count, last occurrence, breakdown by type |
| `/rules` | Plain-language explanation of what the bot checks and how it responds |

---

## Matching rules (what gets flagged)

- **Telegram URLs** — exact matches to specific `t.me/…` paths
- **Domain tokens** — exact domain matches (e.g. `example.com`); broad platforms
  like `youtube.com` or `facebook.com` are **never** stored as bare domain tokens —
  only specific URLs from those platforms can match
- **Telegram handles** — `@channelname` (case-insensitive)
- **Text names** — whole-word, case-insensitive matches only (no partial-word hits)
- **Forwarded messages** — origin channel username and title are also checked

---

## Setup

### Requirements

- Python 3.9+
- **abiword** (for `.doc` → `.docx` conversion, preferred — lightweight, ~30 MB RAM) **or**
  **LibreOffice** (heavier fallback, ~500 MB RAM) **or**
  **antiword** (plain-text last resort)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Install

```bash
git clone <repo>
cd thehallmonitor
make init          # creates venv, installs dependencies, copies .env.example → .env
```

Or manually:

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p data
```

### Configure

Edit `.env`:

```ini
# Required — Telegram bot token from @BotFather
BOT_TOKEN=your_token_here

# Required — where to fetch the forbidden-materials document
INDEX_PAGE=https://sch135.no.net/forbidden/
BASE_URL=https://sch135.no.net

# Optional — regex that finds the .doc link in the index page HTML
# Default matches:  href="/path/to/file.doc"
DOC_LINK_RE=href=["'](/[^"']+\.doc)["']
```

### Populate the database (first run)

```bash
make update-db     # or: python updater.py
```

This downloads and parses the current list.
Expected output: several thousand tokens (URLs, handles, domains, text names).

### Run

```bash
make run           # or: python bot.py
```

On first start, if the database is empty, the bot runs the updater automatically
before beginning to poll for messages.

---

## Telegram setup

1. Create a bot via [@BotFather](https://t.me/BotFather), save the token.
2. **Disable Group Privacy**: @BotFather → /mybots → your bot →
   Bot Settings → Group Privacy → **Turn Off**.
   This allows the bot to read all group messages (not just commands).
3. Add the bot to your group.
4. Grant admin permissions as needed for the mode you intend to use:
   - *Off / React / Reply* — no admin permissions required
   - *Delete silently / Delete + notify* — grant **"Delete messages"** permission

---

## Makefile reference

```
make help          — list all targets
make init          — create venv + install deps + copy .env.example
make run           — run the bot locally
make update-db     — fetch and parse the latest forbidden list
make reset-db      — delete the SQLite database (keeps .env and venv)
make clean         — remove venv
make reset         — remove venv + database + .env (full wipe)
make test          — run all tests with pytest

make install       — install as a systemd service (Ubuntu/Linux only)
make uninstall     — remove the systemd service

make docker-build  — build the Docker image
make docker-up     — start in background (docker-compose up -d)
make docker-down   — stop (docker-compose down)
make docker-logs   — tail container logs
make docker-restart — restart the container
make docker-upgrade — list available base image versions (to update Dockerfile)
```

---

## Docker (recommended for production)

```bash
cp .env.example .env
# Set BOT_TOKEN and source variables in .env

docker-compose up -d
```

The `./data` directory is mounted as a volume so the SQLite database persists
across container restarts. abiword and antiword are included in the Docker image
(abiword uses ~30 MB RAM vs ~500 MB for LibreOffice, suitable for small VPS).
The base image is pinned to a specific Python patch version tag
(e.g. `3.11.14-slim-bookworm`) for reproducible builds across all platforms.
Run `make docker-upgrade` to check for newer versions.

---

## Testing (no Telegram required)

```bash
make test                             # run all 88 unit tests

# Matcher CLI — check a message against the live database
python matcher.py "@weedsmokers shared this"
python matcher.py "check https://t.me/weedsmokers"
python matcher.py "hello world"

# Force a database refresh
python updater.py --force
```

---

## Project structure

```
thehallmonitor/
├── bot.py          — Telegram bot (long-running)
├── admin.py        — Private-chat admin UI (/settings, mode switching)
├── config.py       — Mode constants and ChatConfig dataclass
├── updater.py      — Daily doc fetcher / parser (also CLI)
├── parser.py       — .doc → token list (LibreOffice + antiword)
├── matcher.py      — Message checker (also CLI)
├── database.py     — SQLite layer
├── tests/          — Unit tests (88 tests, no network needed)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── data/           — SQLite database (created at runtime)
```

---

## Known limitations

**In-memory warning state.**
The mapping of original messages to bot warnings is kept in memory only.
On bot restart, any pending warnings (where the original may later be edited
or deleted) are forgotten. The bot will not attempt to clean them up after
restarting. This is a hard constraint of the Telegram Bot API.

**Text-name matching is exact-word only.**
Short names match as whole words, not as substrings inside longer words,
to avoid false positives.

**Reaction support is group-type dependent.**
Telegram restricts which emojis can be used as reactions in certain group types.
If the chosen emoji fails in practice, use `/settings` to configure a different one.

---

## License

This is free and unencumbered software released into the public domain.
See [UNLICENSE](UNLICENSE).
