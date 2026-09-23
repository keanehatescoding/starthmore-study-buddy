#!/bin/sh
# Nightly Postgres backup. Cron example (02:00, keep 14 days):
#   0 2 * * * /srv/study-buddy/scripts/backup.sh >> /var/log/study-buddy-backup.log 2>&1
# Reads DATABASE_URL from /srv/study-buddy/.env if set there.
set -eu

APP_DIR="${APP_DIR:-/srv/study-buddy}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
if [ -f "$APP_DIR/.env" ]; then
    # shellcheck disable=SC1090
    DATABASE_URL="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | cut -d= -f2-)"
    export DATABASE_URL
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$BACKUP_DIR/studybuddy-$STAMP.dump"

pg_dump --format=custom --file="$FILE" "${DATABASE_URL:?DATABASE_URL not set}"
find "$BACKUP_DIR" -name 'studybuddy-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $FILE"
