# Deploy

## Railway (recommended)

1. `railway init`, add the repo. Add a Postgres plugin (sets `DATABASE_URL`).
2. Set env vars (see `.env.example`): `MOODLE_TOKEN`, `MOODLE_BASE_URL`,
   `GOOGLE_CLIENT_ID/SECRET` (after classroom consent; enable both the Classroom
   API and the Drive API in that Google Cloud project, since Classroom materials
   are Drive files read with `drive.readonly`), `LLM_*`, `RESEND_API_KEY`,
   `EMAIL_FROM`, `EMAIL_TO`, `APP_BASE_URL` (the public web URL, used in
   email links). Note: `DATABASE_URL` must use the `psycopg`
   driver as-is; no code change needed.
3. Services — five in all, counting Postgres:

   | Service | Start command | Schedule |
   |---|---|---|
   | `web` | `Procfile` / `Dockerfile` default | always on |
   | `worker` | `alembic upgrade head && python -m app.worker --loop 3600` | always on |
   | `sync-cron` | step 5 | `0 3 * * *` |
   | `pipeline-cron` | step 6 | `0 6 * * *` |

   The worker drains the job queue hourly: queued syncs (sign-in, the Sync
   button) and the notification pass. Every service starts with `alembic
   upgrade head`: services deploy independently, and one running new code
   against an old schema fails (e.g. `column ... does not exist`) until
   web happens to redeploy. It is a no-op at head, and concurrent runs
   serialize on a Postgres advisory lock. Point all services at the same
   repo and branch, so a merge redeploys them together.
4. Each user connects their own Moodle account at **Moodle** in the nav
   (`/settings/moodle`: sign in once via `login/token.php`, or paste their
   mobile web service key). Tokens are stored encrypted with a key derived
   from `SECRET_KEY` — rotating `SECRET_KEY` means everyone reconnects, and
   the worker and cron services need the same `SECRET_KEY` as web
   (`scripts/railway-sync-cron-env.sh <service>` references it).
   The global `MOODLE_TOKEN` is only used for `MOODLE_TOKEN_OWNER` (with no
   owner it is unused), and likewise `GOOGLE_REFRESH_TOKEN` for
   `GOOGLE_REFRESH_TOKEN_OWNER`. Courses synced before sign-in existed stay
   hidden until the owner signs in, or `python -m app.admin_cli claim-unowned
   EMAIL` assigns them.
5. `sync-cron`: a Railway cron service, daily at 03:00 UTC. Syncs go
   through the job queue — one job per connected user, then the worker
   drains them and sends notifications:
   ```
   alembic upgrade head && python -m app.sync_cli --source moodle --all-users --enqueue && python -m app.sync_cli --source classroom --all-users --enqueue && python -m app.worker
   ```
   Signing in with Google also queues a Classroom sync for that user. Users
   who signed in before Drive access was requested must sign in once more;
   until then their Drive files stay pending.
6. `pipeline-cron`: a second cron service, daily at 06:00 UTC — a separate
   slot from the sync, since paced LLM runs take hours and the free Gemini
   tier rate-limits hard. It extracts, chunks and writes quizzes for what
   the sync brought in:
   ```
   alembic upgrade head && python -m app.pipeline --source moodle --pace 45 && python -m app.pipeline --source classroom --pace 45
   ```
   A run that exhausts the LLM quota stops early (`quota_exhausted`) and the
   next day's run resumes; resources that keep failing back off and are
   eventually marked failed instead of being re-billed daily.

   Both crons need the web service's variables (`scripts/railway-sync-cron-env.sh
   <service>` references them, `LLM_*` included); set `LLM_CHUNK_MODEL` /
   `LLM_QUIZ_MODEL` on `pipeline-cron` only if they differ from the defaults.
   All steps are idempotent; re-running is always safe.

## Single VPS (podman)

```
podman-compose up -d db
.venv/bin/alembic upgrade head
nohup .venv/bin/uvicorn app.main:app --port 8000 &
```

Cron (daily 06:00 sync + notify for every connected user):

```
0 6 * * * cd /srv/study-buddy && .venv/bin/python -m app.sync_cli --source moodle --all-users --enqueue && .venv/bin/python -m app.sync_cli --source classroom --all-users --enqueue && .venv/bin/python -m app.worker
```

Pipeline (daily 09:00, after the sync; paced LLM runs take hours):

```
0 9 * * * cd /srv/study-buddy && .venv/bin/python -m app.pipeline --source moodle --pace 45 && .venv/bin/python -m app.pipeline --source classroom --pace 45
```

The pipeline is not a queue job: it runs from cron (or by hand for a
backfill), never inside the worker.
The quiz/chunk runners abort early with `quota_exhausted` when the LLM
quota is gone; re-run the same command later to resume (completed chunks
are skipped via generation keys).

Backups (nightly pg_dump, 14-day retention):

```
0 2 * * * /srv/study-buddy/scripts/backup.sh >> /var/log/study-buddy-backup.log 2>&1
```

## Notes

- Rate limiting is per-process memory (120 POSTs/min default), keyed by
  signed-in user and by client IP for anonymous requests, with at most
  10,000 clients tracked (least recently seen evicted). Behind multiple
  uvicorn workers put a shared limiter or a proxy limit in front.
- Client IPs come from `X-Forwarded-For`, trusted from the peers listed in
  `FORWARDED_ALLOW_IPS` (Dockerfile/Procfile default `*`: any peer). Uvicorn
  takes the leftmost address that isn't a trusted proxy — with `*` that is
  the leftmost entry, which a client can forge unless the proxy **replaces**
  any incoming `X-Forwarded-For` rather than appending to it. So: only let
  the proxy reach uvicorn (never publish the port directly), make the proxy
  overwrite the header (nginx: `proxy_set_header X-Forwarded-For
  $remote_addr;`, not `$proxy_add_x_forwarded_for`), and where the proxy's
  address is known set `FORWARDED_ALLOW_IPS` to it (IPs or CIDRs,
  comma-separated). A forged address only affects the anonymous POST budget
  and logs — signed-in users are rate-limited by account.
- CSP: scripts and `<style>` elements need the per-response nonce
  (`{{ request.state.csp_nonce }}` in templates); no `unsafe-inline`.
  `style="…"` attributes are blocked — add a class to `static/css/app.css`.
- CI (`.github/workflows/ci.yml`) runs migrations + `alembic check` + the
  full suite on Postgres 16 for every push/PR.
- `/health` checks Postgres and returns 503 when unreachable — safe to use
  for platform restart decisions.
- Sign-in is limited by `ALLOWED_EMAILS` (default `@strathmore.edu`): a
  comma-separated mix of exact addresses and `@domain` entries. Anyone else
  is sent back to `/login` with an explanation. Set it to your address for
  personal-tool mode, or to an empty value to admit any Google account.
  Removing an address ends that account's existing sessions on its next
  request.
- Sessions last `SESSION_MAX_AGE` seconds without use (default 7 days).
  Logging out ends the session server-side, so a copied cookie stops
  working too; `python -m app.admin_cli revoke-sessions EMAIL` signs an
  account out on every device (e.g. after a lost laptop).
- Moodle password sign-in (`/settings/moodle`) is limited to 5 attempts per
  15 minutes per account and per Moodle username, so it can't be used to
  guess another student's password. Like the POST limit, it is per-process.
- Cron observability: set `HEALTHCHECK_PING_URL` (e.g. a healthchecks.io
  check) — the worker pings it after every successful pass, so a silent
  6am failure pages you instead of showing up as missing quizzes.
- Web UI requires Google sign-in (`/login`). Generate a real secret:
  `openssl rand -hex 32` → `SECRET_KEY`. Every service refuses to start
  with the public default key unless `DEV=1` is set, which is for local
  development only — never set it on a server. Set `SESSION_SECURE_COOKIE=true`
  behind HTTPS. The Google OAuth consent screen must list your production
  origin as an authorized redirect (`.../auth/callback`).
- Never commit `.env` (gitignored). Rotate Moodle/Google credentials if exposed.
