# Deploy

## Railway (recommended)

1. `railway init`, add the repo. Add a Postgres plugin (sets `DATABASE_URL`).
2. Set env vars (see `.env.example`): `MOODLE_TOKEN`, `MOODLE_BASE_URL`,
   `GOOGLE_CLIENT_ID/SECRET` (after classroom consent), `LLM_*`, `RESEND_API_KEY`,
   `EMAIL_FROM`, `EMAIL_TO`. Note: `DATABASE_URL` must use the `psycopg`
   driver as-is; no code change needed.
3. Services from `Procfile`: `web` (FastAPI) and `worker` (hourly notification pass).
4. Sync on a schedule with a Railway cron service, daily (replace
   `YOU@X` with the owner's login email). Syncs go through the job queue —
   enqueue, then the worker drains them and sends notifications:
   ```
   python -m app.sync_cli --source moodle --user YOU@X --enqueue && python -m app.worker
   ```
   Run chunking/quiz generation paced (`--pace 45`) afterwards — the free
   Gemini tier rate-limits hard, so don't bundle them into the same cron slot.
   All three steps are idempotent; re-running is always safe.

## Single VPS (podman)

```
podman-compose up -d db
.venv/bin/alembic upgrade head
nohup .venv/bin/uvicorn app.main:app --port 8000 &
```

Cron (daily 06:00 sync + notify, replace YOU@X with the login email):

```
0 6 * * * cd /srv/study-buddy && .venv/bin/python -m app.sync_cli --source moodle --user YOU@X --enqueue && .venv/bin/python -m app.worker
```

Long LLM pipeline runs (`app.pipeline` chunk/quiz backfills) stay
operator-triggered — they run paced over hours and are not queue jobs yet.
The quiz/chunk runners abort early with `quota_exhausted` when the LLM
quota is gone; re-run the same command later to resume (completed chunks
are skipped via generation keys).

Backups (nightly pg_dump, 14-day retention):

```
0 2 * * * /srv/study-buddy/scripts/backup.sh >> /var/log/study-buddy-backup.log 2>&1
```

## Notes

- Rate limiting is per-process memory (120 POSTs/min/IP default). Behind
  multiple uvicorn workers put a shared limiter or a proxy limit in front,
  and set the client IP from `X-Forwarded-For` (currently `request.client`).
- CI (`.github/workflows/ci.yml`) runs migrations + `alembic check` + the
  full suite on Postgres 16 for every push/PR.
- `/health` checks Postgres and returns 503 when unreachable — safe to use
  for platform restart decisions.
- Personal-tool mode: set `ALLOWED_EMAILS` to your address so random Google
  accounts get 403 at the callback instead of an empty session.
- Cron observability: set `HEALTHCHECK_PING_URL` (e.g. a healthchecks.io
  check) — the worker pings it after every successful pass, so a silent
  6am failure pages you instead of showing up as missing quizzes.
  multiple uvicorn workers put a shared limiter or a proxy limit in front,
  and set the client IP from `X-Forwarded-For` (currently `request.client`).
- Web UI requires Google sign-in (`/login`). Generate a real secret:
  `openssl rand -hex 32` → `SECRET_KEY`. Set `SESSION_SECURE_COOKIE=true`
  behind HTTPS. The Google OAuth consent screen must list your production
  origin as an authorized redirect (`.../auth/callback`).
- Never commit `.env` (gitignored). Rotate Moodle/Google credentials if exposed.
