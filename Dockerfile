# Pin to a specific patch version tag — reproducible across all platforms
# without needing digest pinning (which is architecture-specific).
# To upgrade: change the version below, e.g. 3.11.15-slim-bookworm
FROM python:3.11.14-slim-bookworm

# ── System dependencies ───────────────────────────────────────────────────────
# libreoffice-writer: .doc → .docx conversion (primary path)
# antiword:           plain-text fallback if LibreOffice fails
# fonts: needed by LibreOffice to avoid rendering warnings
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-common \
        antiword \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies (own layer for caching) ───────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY bot.py updater.py parser.py database.py matcher.py config.py admin.py ./

# ── Data directory (mounted as a volume in production) ────────────────────────
RUN mkdir -p data

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN useradd --create-home --shell /bin/bash botuser \
    && chown -R botuser:botuser /app
USER botuser

# LibreOffice writes its user profile here
ENV HOME=/home/botuser

VOLUME ["/app/data"]

CMD ["python", "bot.py"]
