#!/bin/sh
# Point a Railway service's variables at the web service's, so secrets live in
# one place. Run from the repo root after `railway link`.
#   scripts/railway-sync-cron-env.sh [service] [environment]
# SECRET_KEY is needed off-web too: it decrypts users' stored Moodle tokens.
set -eu

SERVICE="${1:-sync-cron}"
ENVIRONMENT="${2:-production}"
WEB="starthmore-study-buddy"
# Everything the worker and crons read (app/config.py). Web-only settings
# (sessions, ALLOWED_EMAILS, LLM_GRADE_*) are left out.
KEYS="DATABASE_URL SECRET_KEY TIMEZONE
MOODLE_BASE_URL MOODLE_TOKEN MOODLE_TOKEN_OWNER
GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_REFRESH_TOKEN GOOGLE_REFRESH_TOKEN_OWNER
LLM_API_KEY LLM_BASE_URL LLM_CHUNK_MODEL LLM_QUIZ_MODEL LLM_PACE
RESEND_API_KEY EMAIL_FROM EMAIL_TO APP_BASE_URL
HEALTHCHECK_PING_URL JOB_RETENTION_DAYS"

# Only reference what web defines: a reference to an unset variable resolves
# may resolve to "", and an empty LLM_PACE or model name overrides the app default with
# an invalid value. Unset ones keep their app defaults.
WEB_KEYS="$(railway variables --service "$WEB" --environment "$ENVIRONMENT" --kv | cut -d= -f1)"
set --
for k in $KEYS; do
    if printf '%s\n' "$WEB_KEYS" | grep -qx "$k"; then
        set -- "$@" --set "$k=\${{$WEB.$k}}"
    else
        echo "skip $k (not set on $WEB)"
    fi
done

railway variables --service "$SERVICE" --environment "$ENVIRONMENT" "$@"

# Verify: names only, and the DB URL scheme (password masked).
echo "--- $SERVICE variables:"
railway variables --service "$SERVICE" --environment "$ENVIRONMENT" --kv \
    | cut -d= -f1 | grep -v '^RAILWAY_' | sort | tr '\n' ' '
echo
railway variables --service "$SERVICE" --environment "$ENVIRONMENT" --kv \
    | grep '^DATABASE_URL=' | sed -E 's#://([^:]+):[^@]*@#://\1:***@#'
