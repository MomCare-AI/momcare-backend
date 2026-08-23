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
python manage.py collectstatic --noinput
```

`createcachetable` matters more than it looks. Production caches to the
database, and the cache holds DRF's throttle counters — so without that table
every rate-limited endpoint errors. It is idempotent, so running it on every
deploy is fine.

**`web`** — the server itself:

```
gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 60
```

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
| `DATABASE_URL` | From Neon. Must include `?sslmode=require`. |
| `DJANGO_NUM_PROXIES` | `1` behind a single load balancer |

### Email — invitations do not work without these

| Variable | Value |
|---|---|
| `DJANGO_EMAIL_HOST` | `smtp.resend.com` |
| `DJANGO_EMAIL_PORT` | `587` |
| `DJANGO_EMAIL_USE_TLS` | `True` |
| `DJANGO_EMAIL_HOST_USER` | `resend` — the literal word |
| `DJANGO_EMAIL_HOST_PASSWORD` | The Resend API key |
| `DJANGO_DEFAULT_FROM_EMAIL` | `MomCare <noreply@momcare.solutions>` |
| `DJANGO_FRONTEND_URL` | `https://momcare.solutions` — **the base for invitation links.** Leave it as localhost and every invitation opens on the sender's own machine. |

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
