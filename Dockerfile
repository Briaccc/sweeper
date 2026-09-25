# sweeper — retention for Plex libraries managed by Radarr/Sonarr.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sweeper.py config.example.toml ./
COPY sweeper/ sweeper/
COPY webui/ webui/

# Configuration and state live in /config: mount it to keep them across upgrades.
# On first start, /config/config.toml is created from config.example.toml.
ENV SWEEPER_CONFIG=/config/config.toml \
    SWEEPER_STATE_DIR=/config/state \
    PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 29320

# Inside the container the server listens on all interfaces; whether that port
# leaves the host is decided by the `ports:` mapping (see docker-compose.example.yml).
CMD ["python3", "sweeper.py", "serve", "--host", "0.0.0.0"]
