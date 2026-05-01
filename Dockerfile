# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# sqlite3 CLI for ad-hoc inspection; curl for HEALTHCHECK and as the
# chabo.telegram fallback HTTP backend; ca-certificates for HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        sqlite3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user; /data is the mounted SQLite volume.
RUN useradd --create-home --shell /bin/bash chabo \
    && mkdir -p /data \
    && chown chabo:chabo /data

WORKDIR /home/chabo/app
COPY --chown=chabo:chabo pyproject.toml README.md ./
COPY --chown=chabo:chabo src/ ./src/

USER chabo
RUN pip install --user --no-cache-dir .

ENV PATH="/home/chabo/.local/bin:$PATH" \
    CHABO_DB_PATH=/data/chabo.sqlite3 \
    CHABO_WEB_HOST=0.0.0.0 \
    CHABO_WEB_PORT=8080

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["chabo"]
CMD ["run-web"]
