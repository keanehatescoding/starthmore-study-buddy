#!/bin/sh
# Nightly Postgres backup. Cron example (02:00, keep 14 days):
#   0 2 * * * /srv/study-buddy/scripts/backup.sh >> /var/log/study-buddy-backup.log 2>&1
# Reads DATABASE_URL from /srv/study-buddy/.env if set there.
set -eu
umask 077  # dumps hold every user's data and encrypted tokens

APP_DIR="${APP_DIR:-/srv/study-buddy}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
PYTHON="$APP_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3

mkdir -p "$BACKUP_DIR"
# Ambient env wins; then .env (last DATABASE_URL line, `export` and quotes
# allowed); then the app default (see app/config.py).
if [ -z "${DATABASE_URL:-}" ] && [ -f "$APP_DIR/.env" ]; then
    DATABASE_URL="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?DATABASE_URL[[:space:]]*=[[:space:]]*//p' \
        "$APP_DIR/.env" | tail -n 1 | sed -E "s/^\"(.*)\"\$/\\1/; s/^'(.*)'\$/\\1/")"
fi
DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://studybuddy:studybuddy@localhost:5432/studybuddy}"

# pg_dump gets the URL without the password (argv is visible to every user
# in `ps`); the password goes through PGPASSWORD, which only this user and
# root can read. Also drops the SQLAlchemy driver suffix pg_dump rejects.
split_url() {
    "$PYTHON" - "$1" "$2" <<'PY'
import sys
from urllib.parse import unquote, urlsplit, urlunsplit

url, part = sys.argv[1], sys.argv[2]
u = urlsplit(url)
scheme = "postgresql" if u.scheme.split("+")[0] in ("postgres", "postgresql") else u.scheme
userinfo, at, hostport = u.netloc.rpartition("@")
user, _, password = userinfo.partition(":")
if part == "password":
    print(unquote(password))
else:
    print(urlunsplit((scheme, f"{user}{at}{hostport}", u.path, u.query, u.fragment)))
PY
}
DUMP_URL="$(split_url "$DATABASE_URL" url)"
PGPASSWORD="$(split_url "$DATABASE_URL" password)"
unset DATABASE_URL
if [ -n "$PGPASSWORD" ]; then export PGPASSWORD; else unset PGPASSWORD; fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$BACKUP_DIR/studybuddy-$STAMP.dump"
PARTIAL="$FILE.partial"
# a failed or interrupted dump must not look like a backup
trap 'rm -f "$PARTIAL"' EXIT
trap 'exit 1' HUP INT TERM

pg_dump --format=custom --file="$PARTIAL" "$DUMP_URL"
mv "$PARTIAL" "$FILE"
# only prune once today's dump exists; *.partial = a run that was SIGKILLed
find "$BACKUP_DIR" -name 'studybuddy-*.dump' -mtime +"$KEEP_DAYS" -delete
find "$BACKUP_DIR" -name 'studybuddy-*.dump.partial' -mtime +0 -delete
echo "backup ok: $FILE"
