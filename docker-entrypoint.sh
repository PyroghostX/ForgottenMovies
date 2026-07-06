#!/bin/sh
set -e

# Honor the standard PUID/PGID convention: remap the bundled appuser to the
# requested UID/GID, make sure the data directory is writable, then drop from
# root to that user before launching the app.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_DIR="${DATA_DIR:-/app/data}"

if [ "$(id -u)" = "0" ]; then
    groupmod -o -g "$PGID" appuser 2>/dev/null || true
    usermod -o -u "$PUID" -g "$PGID" appuser 2>/dev/null || true

    mkdir -p "$DATA_DIR"
    chown -R "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true

    echo "Starting Forgotten Movies as UID:GID ${PUID}:${PGID}"
    exec gosu "$PUID:$PGID" "$@"
fi

# Already running as a non-root user (e.g. compose `user:` override); just run.
echo "Starting Forgotten Movies as UID:GID $(id -u):$(id -g)"
exec "$@"
