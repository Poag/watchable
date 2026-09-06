#!/bin/sh
# Remaps the built-in `watchable` user to PUID/PGID (default 1000/1000) if
# set, fixes ownership of the writable /data volume, then drops root and
# execs the real command as that user -- so the container can be started as
# any host uid/gid without needing to rebuild the image.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    if [ "$(id -g watchable)" != "$PGID" ]; then
        groupmod -o -g "$PGID" watchable
    fi
    if [ "$(id -u watchable)" != "$PUID" ]; then
        usermod -o -u "$PUID" watchable
    fi

    chown -R watchable:watchable /data

    exec gosu watchable watchable "$@"
fi

# Already non-root (e.g. a custom `docker run --user`) -- run directly.
exec watchable "$@"
