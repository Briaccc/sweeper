# sweeper — retention for Plex libraries managed by Radarr/Sonarr.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="sweeper" \
      org.opencontainers.image.description="Retention for Plex libraries managed by Radarr and Sonarr that actually frees the disk space" \
      org.opencontainers.image.source="https://github.com/Briaccc/sweeper" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd --gid 1000 sweeper \
 && useradd --uid 1000 --gid 1000 --home-dir /config --no-create-home --shell /usr/sbin/nologin sweeper \
 && mkdir -p /config && chown 1000:1000 /config

COPY sweeper.py config.example.toml docker-entrypoint.sh ./
COPY sweeper/ sweeper/
COPY webui/ webui/

# Configuration and state live in /config: mount it to keep them across upgrades.
# On first start, /config/config.toml is created from config.example.toml.
# /data is where the compose example mounts the media: the dashboard measures it.
ENV SWEEPER_CONFIG=/config/config.toml \
    SWEEPER_STATE_DIR=/config/state \
    SWEEPER_STORAGE_PATH=/data \
    HOME=/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME /config
EXPOSE 29320

# Not root. Run the container as the owner of your media instead (`user:` in
# compose): sweeper reads the files to follow hardlinks and renames bare files.
USER 1000:1000

# The port opens once the first inventory is computed, hence the long start period.
HEALTHCHECK --interval=1m --timeout=10s --start-period=5m --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:29320/', timeout=5)"]

# Inside the container the server listens on all interfaces; whether that port
# leaves the host is decided by the `ports:` mapping (see docker-compose.example.yml).
ENTRYPOINT ["sh", "./docker-entrypoint.sh"]
CMD ["python3", "sweeper.py", "serve", "--host", "0.0.0.0"]
