#!/bin/sh
set -eu

: "${POSTGRES_USER:?required}"
: "${POSTGRES_DB:?required}"
: "${BACKUP_DIR:?required}"

umask 077
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
pg_dump --format=custom --no-owner --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" > "$BACKUP_DIR/recruiting-$stamp.dump"
find "$BACKUP_DIR" -type f -name 'recruiting-*.dump' -mtime +7 -delete
