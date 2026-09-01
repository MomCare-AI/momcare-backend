# Deploying the MomCare backend

Written for Railway, but nothing here is Railway-specific beyond the dashboard
names — the same commands work on Render, Heroku or a plain VM.

---

## What runs, and when

The `Procfile` declares two processes.

**`release`** — runs once per deploy, before the new version serves traffic:

```
python manage.py migrate --noinput
python manage.py createcachetable
```

`createcachetable` matters more than it looks. Production caches to the
database, and the cache holds DRF's throttle counters — so without that table
every rate-limited endpoint errors. It is idempotent, so running it on every
deploy is fine.

**`web`** — the server itself:

```
python manage.py collectstatic --noinput
gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 60
```

`collectstatic` runs here, not in `release`, on purpose. Static files use
`whitenoise.storage.CompressedManifestStaticFilesStorage`, which needs a
manifest file on local disk at request time — and on Railway (like Heroku)
the `release` step and the `web` process do not share a filesystem. Anything
`release` writes to disk is gone by the time `web` starts; only its database
changes (migrations, the cache table) persist. Putting `collectstatic` in
`web`'s own start line guarantees the manifest exists in the exact container
that serves it. Running it before `gunicorn` starts on every boot costs a few
seconds and is idempotent, so this is cheap insurance, not a workaround.

Two workers is deliberate. The free and hobby tiers give roughly 0.5–1 GB, and
each Django worker holds its own copy of the application; more workers on a
small instance means memory pressure, not throughput. Access and error logs go
to stdout so the platform captures them.

---

## Environment variables

Set every one of these before the first deploy. Several have no default and the
process will refuse to start without them.

### Required

| Variable | Value |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` |
| `DJANGO_SECRET_KEY` | 50+ random characters. **Never reuse the development one.** |
| `DJANGO_ALLOWED_HOSTS` | `api.momcare.solutions` — add the platform's own domain too while testing |
| `DJANGO_ADMIN_URL` | A non-obvious path ending in `/`, e.g. `mc-admin-7f3a/`. Not `admin/`. |
| `DATABASE_URL` | From Neon, **as the restricted `momcare_app` role** — see "Database roles" below. Must include `?sslmode=require`. |
| `MIGRATION_DATABASE_URL` | From Neon, as the table-owning role. Used only for `migrate` and `createcachetable` — never by the running app. |
| `DJANGO_NUM_PROXIES` | `1` behind a single load balancer |

### Database roles — why there are two connection strings

RLS policies (`organization/migrations/0006_row_level_security.py`) only do
anything against a role that can't bypass them. Every role on this project's
databases used to be the same table-owning role, which has `BYPASSRLS` and
ignores every policy unconditionally — a Postgres limitation, not a flaw in
the policies. Two roles fix this:

- **`MIGRATION_DATABASE_URL`** — the original owner role (`neondb_owner`).
  Needs to run DDL (`CREATE TABLE`, `ALTER TABLE`), so it stays privileged.
- **`DATABASE_URL`** — the `momcare_app` role: `LOGIN`, `NOSUPERUSER`,
  `NOCREATEDB`, `NOCREATEROLE`, `NOBYPASSRLS`, with `SELECT, INSERT,
  UPDATE, DELETE` on every table, `USAGE, SELECT` on every sequence, and
  `ALTER DEFAULT PRIVILEGES` set so both grants extend automatically to
  whatever a future migration adds — no `DROP`/`ALTER`/`TRUNCATE`. This is
  what `web` (gunicorn) actually connects as, so RLS is finally enforced
  for real traffic.

**The switch is handled in `config/settings/production.py`, not the
`Procfile`.** `migrate` and `createcachetable` are detected by command name
(`sys.argv[1]`) and pointed at `MIGRATION_DATABASE_URL` when it's set;
every other invocation — `runserver`, and gunicorn loading `wsgi.py`, which
never goes through `manage.py` at all — uses `DATABASE_URL` as normal. A
Procfile-level env override (`DATABASE_URL=$MIGRATION_DATABASE_URL python
manage.py migrate`) was tried first and deliberately abandoned: Railway's
current builder (Railpack) doesn't document `release` as a first-class
Procfile phase the way the older Nixpacks builder did, so betting a
migration step on unverified platform-specific shell/process behavior
wasn't worth it. The settings-level switch is portable, works identically
regardless of platform or how the process was invoked, and was verified
directly — `migrate`/`createcachetable` resolve to the owner role,
`runserver` and the gunicorn/`wsgi.py` entrypoint resolve to `momcare_app`,
and with `MIGRATION_DATABASE_URL` unset entirely, `migrate` correctly falls
back to `DATABASE_URL` rather than erroring — checked by directly
inspecting `settings.DATABASES` under each `sys.argv`, not by reasoning
about it on paper.

The role itself already exists in production (created independently of
this settings change, then verified rather than trusted): correct
attributes (`NOSUPERUSER`/`NOBYPASSRLS`/etc., confirmed via `pg_roles`),
grants on all 38 tables (confirmed via `information_schema.role_table_
grants`), 16 sequence grants (confirmed via `information_schema.usage_
privileges`), and default privileges covering both tables and sequences
for future migrations (confirmed via `pg_default_acl`). The pattern was
also verified end-to-end against a locally-built identical role before any
of this was trusted: login, an org-scoped list endpoint, and a cross-tenant
detail read (correctly 404s, matching the "404 never 403" rule elsewhere in
this doc) all behaved correctly.

**Rollout order matters** — `DATABASE_URL` must not point at `momcare_app`
until `MIGRATION_DATABASE_URL` exists, or the next migration has nothing to
fall back to and fails outright:

1. Confirm `MIGRATION_DATABASE_URL` is set in Railway (copy of the
   pre-switch `DATABASE_URL`, the owner role) — must happen before step 2.
2. Change `DATABASE_URL` to the `momcare_app` connection string.
3. Deploy. Watch the release logs — migrations should still succeed (they
   now resolve to `MIGRATION_DATABASE_URL` via the settings-level switch).
4. Immediately verify with the same walkthrough as this doc's own
   "Verifying a deploy properly" section below, plus specifically: sign in,
   confirm you see your own hospital's data, and confirm you do *not* get a
   500 (fails-closed RLS would show as everything returning empty or
   erroring, not as a leak).

**Incident, 2 Sep 2026 — resolved.** `MIGRATION_DATABASE_URL` disappeared
from Railway after step 1 had already been done and verified the day
before. Every deploy from the care-team migration
(`patients.0005_careteammembership`) onward failed at the pre-deploy step
with `psycopg.errors.InsufficientPrivilege: permission denied for schema
public` on `CREATE TABLE` — the exact signature of `migrate` silently
falling back to the restricted `momcare_app` role because the variable it
needed wasn't there. Several deploys failed silently over hours before
this was caught; nothing paged anyone, because Railway doesn't alert on a
failed deploy the way it would an app crash. Fixed by re-adding the
variable from Neon's `neondb_owner` connection string (Neon dashboard →
Connect → role `neondb_owner` → copy the pooled connection string).
Confirmed resolved: the redeploy shows **Active / Deployment successful**
in Railway with all three pending migrations applied, and the health
check plus a real behavioral check both passed afterward. Root cause of
the disappearance itself was not established — worth returning to if it
happens again. **Check this variable's presence as part of any future
deploy troubleshooting before assuming the code is at fault** — a failing
migration with a permission error almost always means this, not a bug in
the migration itself.

### Email — invitations do not work without these

| Variable | Value |
|---|---|
| `DJANGO_EMAIL_BACKEND` | `momcare_platform.core.common.email_backends.ResendHTTPEmailBackend` - **see the note below** |
| `DJANGO_EMAIL_HOST` | `smtp.resend.com` |
| `DJANGO_EMAIL_PORT` | `587` |
| `DJANGO_EMAIL_USE_TLS` | `True` |
| `DJANGO_EMAIL_HOST_USER` | `resend` — the literal word |
| `DJANGO_EMAIL_HOST_PASSWORD` | The Resend API key |
| `DJANGO_DEFAULT_FROM_EMAIL` | `MomCare <noreply@momcare.solutions>` |
| `DJANGO_FRONTEND_URL` | `https://momcare.solutions` — **the base for invitation links.** Leave it as localhost and every invitation opens on the sender's own machine. |

#### Why the HTTPS backend, and not SMTP

Railway blocks outbound SMTP on every port - 25, 465, 587 and the alternates -
to stop its platform being used for spam. The symptom is not a rejection but a
`TimeoutError`: every credential is correct, the send simply never completes,
and because `mail.py` logs failures rather than raising, the portal reports an
invitation as created while nothing was ever delivered.

`ResendHTTPEmailBackend` posts to `https://api.resend.com/emails` over port 443,
which no host blocks because blocking it would break the platform itself. It
uses only the standard library.

The SMTP settings can stay as they are - they are ignored while this backend is
selected, and switching back is one variable.

---

### CORS and the refresh cookie

| Variable | Value |
|---|---|
| `DJANGO_CORS_ALLOWED_ORIGINS` | `https://momcare.solutions,https://www.momcare.solutions` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | same |
| `REFRESH_COOKIE_DOMAIN` *(via `DJANGO_REFRESH_COOKIE_DOMAIN`)* | `.momcare.solutions` — **note the leading dot** |

That leading dot is what makes the cookie work across `momcare.solutions` and
`api.momcare.solutions`. Without it the browser will not send the refresh token
to the API, and users are signed out about an hour after logging in — on some
browsers before others, which makes it look intermittent rather than broken.

### Optional

| Variable | Value |
|---|---|
| `DJANGO_DEMO_PASSWORD` | Needed only to run `seed_demo`. Strong and unique — this is a public deployment. |
| `SENTRY_DSN` | Error reporting. Without it, production failures are invisible. |
| `DJANGO_STORAGE_*` | Object storage. **Not required to boot** — the app falls back to local disk and logs a warning. Uploaded licence documents will not survive a redeploy until this is set. |

---

## First deploy, in order

1. **Create the Neon database**, copy `DATABASE_URL`.
2. **Create the Railway service** from the `momcare-backend` repository.
3. **Set every required variable above.** Do this before the first build, or the
   release command fails and the logs are harder to read than they need to be.
4. **Deploy.** Watch the release phase: migrations, cache table, static files.
5. **Check health:** `GET https://<host>/` should return
   `{"status": "ok", "database": "ok"}`. It returns 503 if the database is
   unreachable, so it tests readiness rather than just that the process is up.
6. **Create a superuser** — the admin site is unreachable without one:
   `python manage.py createsuperuser`
7. **Seed the demo data** once the frontend is also up:
   `python manage.py seed_demo`

---

## Scheduled job — required, not optional

Alerts are *raised* inside the request that recorded the reading, so that part
needs nothing scheduled. They only *climb* when this runs:

```
python manage.py escalate_alerts
```

**Every minute.** On Railway, add a second service from the same repository with
a cron schedule of `* * * * *` and that as its start command.

Without it, alerts reach the assigned clinician and stop. An unanswered critical
alert never reaches anyone more senior — which is the feature working exactly
half way, and the half that is missing is the one that matters at 3am.

The command is idempotent and computes the correct tier from the clock, so a
scheduler that misses several runs lands on the right rung rather than advancing
one step per missed run.

---

## Verifying a deploy properly

A 200 from the health check means the process started. It does not mean the
system works. Check these four:

1. **Sign in** through the portal.
2. **Wait past the access-token lifetime (1 hour), then use the app.** If the
   refresh cookie is misconfigured you are signed out here, and nowhere earlier.
   This is the single most likely thing to be wrong.
3. **Sign out.** Confirm it actually clears — a cookie set with a Domain is not
   removed by a delete without one.
4. **Invite someone at a real address**, on a different network, and have them
   accept it. That is the journey this deployment exists to make possible.
