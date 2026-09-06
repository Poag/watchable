# Build from the repo root:
#   docker build -t watchable -f docker/Dockerfile .
FROM python:3.12-slim

# gosu drops root privileges after entrypoint.sh remaps the `watchable`
# user to PUID/PGID -- needed because that remap itself requires root.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 watchable

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /config /data && chown -R watchable:watchable /data
VOLUME ["/config", "/data"]

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /data

# Stays root at container start -- entrypoint.sh remaps `watchable` to
# PUID/PGID (default 1000/1000) and fixes /data ownership before dropping
# to that user via gosu, so the container works under any host uid/gid
# without a rebuild.
ENTRYPOINT ["/entrypoint.sh"]
# `run` loops forever on sync.schedule.interval_minutes (must be set in
# config.yaml). Override the command to `sync` for a one-shot invocation
# driven by an external cron/systemd timer instead.
CMD ["run", "/config/config.yaml"]
