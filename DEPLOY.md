# Deploy

## Railway (recommended)

1. `railway init`, add the repo. Add a Postgres plugin (sets `DATABASE_URL`).
2. Set env vars (see `.env.example`): `MOODLE_TOKEN`, `MOODLE_BASE_URL`,
   `GOOGLE_CLIENT_ID/SECRET` (after classroom consent), `LLM_*`, `RESEND_API_KEY`,
   `EMAIL_FROM`, `EMAIL_TO`. Note: `DATABASE_URL` must use the `psycopg`
   driver as-is; no code change needed.
3. Services from `Procfile`: `web` (FastAPI) and `worker` (hourly notification pass).
4. Sync on a schedule with a Railway cron service, daily:
   ```
   python -m app.sync_cli --source moodle && python -m app.pipeline --source moodle --extract-only
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

Cron (daily 06:00 sync + notify):

```
0 6 * * * cd /srv/study-buddy && .venv/bin/python -m app.sync_cli --source moodle && .venv/bin/python -m app.pipeline --source moodle --extract-only && .venv/bin/python -m app.worker
```

## Notes

- Never commit `.env` (gitignored). Rotate Moodle/Google credentials if exposed.
- `MOODLE_TOKEN` / Google refresh token live in `User` rows only in later
  phases; for now they come from env.
