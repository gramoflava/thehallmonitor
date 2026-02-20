# Pin by digest so the build is reproducible months later.
# To update: docker pull python:3.11-slim, then copy the new digest from
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.11-slim
FROM python:3.11-slim@sha256:ad48e588f8208ce2e0890e4cdc43c3477a3bc96e9c06c8ffad41f64da1c43b19

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
