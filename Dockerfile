FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY hevy_intervals_sync.py ./

# Create data directory for SQLite ledger
RUN mkdir -p /data

ENV SYNC_DB_PATH=/data/hevy_icu_sync.db \
    PORT=8400 \
    LOG_LEVEL=INFO

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8400/health')" || exit 1

CMD ["python", "hevy_intervals_sync.py", "serve"]
