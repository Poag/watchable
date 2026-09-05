# Build from the repo root:
#   docker build -t watchable -f docker/Dockerfile .
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 watchable

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /config /data && chown -R watchable:watchable /data
VOLUME ["/config", "/data"]

USER watchable
WORKDIR /data

ENTRYPOINT ["watchable"]
# `run` loops forever on sync.schedule.interval_minutes (must be set in
# config.yaml). Override the command to `sync` for a one-shot invocation
# driven by an external cron/systemd timer instead.
CMD ["run", "/config/config.yaml"]
