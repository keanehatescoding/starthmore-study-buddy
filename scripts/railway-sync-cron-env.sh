#!/bin/sh
# Point a Railway service's variables at the web service's, so secrets live in
# one place. Run from the repo root after `railway link`.
#   scripts/railway-sync-cron-env.sh [service] [environment]
# SECRET_KEY is needed off-web too: it decrypts users' stored Moodle tokens.
set -eu

SERVICE="${1:-sync-cron}"
ENVIRONMENT="${2:-production}"
WEB="starthmore-study-buddy"
KEYS="DATABASE_URL SECRET_KEY MOODLE_BASE_URL MOODLE_TOKEN MOODLE_TOKEN_OWNER
GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET RESEND_API_KEY EMAIL_FROM LLM_API_KEY LLM_BASE_URL"

set --
for k in $KEYS; do
    set -- "$@" --set "$k=\${{$WEB.$k}}"
done

railway variables --service "$SERVICE" --environment "$ENVIRONMENT" "$@"

# Verify: names only, and the DB URL scheme (password masked).
echo "--- $SERVICE variables:"
railway variables --service "$SERVICE" --environment "$ENVIRONMENT" --kv \
    | cut -d= -f1 | grep -v '^RAILWAY_' | sort | tr '\n' ' '
echo
railway variables --service "$SERVICE" --environment "$ENVIRONMENT" --kv \
    | grep '^DATABASE_URL=' | sed -E 's#://([^:]+):[^@]*@#://\1:***@#'
