# AGENTS.md — how work is done in this repository

For any AI agent or new contributor. **Read `CLAUDE.md` first** — it holds the
architecture, the tenancy model, and the rules about the domain. This file holds
the *method*: how changes are made, verified and committed here.

Both files are binding. Where they disagree, the more specific one wins.

---

## Where things live

| | |
|---|---|
| `CLAUDE.md` | Architecture, tenancy, clinical rules, scoping paths |
| `DEPLOY.md` | Every environment variable, and which failures are silent |
| `../docs/PLAN.md` | Status, the ordered to-do list, decisions not to revisit |
| `../backups/` | Database dumps and how to restore one |

**`../docs/` and `../backups/` are outside this repository and are not in git.**
They sit beside it on the author's machine and in Google Drive. If you cannot
see them, ask before assuming a task is undone — the plan may already cover it.

---

## The method

### Verify; do not assume

Every claim in this project is expected to be checked before it is stated. This
is the single most important habit here, and most of the real bugs were found
this way rather than by reasoning.

- Read the code before describing what it does. Several times a confident
  description of this codebase turned out to be wrong, and reading took less
  time than the correction.
- After a deployment change, check the thing itself: the certificate, the exact
  origin in the response header, the address the compiled bundle actually calls,
  the domain attribute on the cookie in a real browser. Two of those checks
  failed on the first attempt.
- Say plainly when something was **not** verified. "I have not seen it rendered"
  is useful; implying you have is not.

### Prove a test can fail

Any test covering a protection must be shown to fail when the protection is
removed. Inject the fault, watch the test fail, restore, watch it pass.

This is not ceremony. Two tests in this repository passed for the wrong reason
and were only caught this way — one asserted a session was revoked when it was
really observing a cookie being cleared.

### One source of truth

A value that exists in two places will disagree eventually, and the wrong copy
will look exactly as convincing as the right one.

- Derived values are computed, never stored — gestational age from the EDD,
  model region from the country. See `CLAUDE.md`.
- Where duplication is genuinely unavoidable (the region map is mirrored in the
  frontend so the form can react as a country is picked), a test parses both
  and fails if one entry disagrees.

### Absent is not zero

Never invent a clinical value to avoid a null. A country the model was not
trained on returns `None`, not a nearest guess. A patient with no readings is
"not currently monitored", not "stable". The interface shows "No reading", never
a normal-looking default.

### Explain why, not what

Comments and commit messages record the reasoning that is not recoverable from
the diff — what was tried, what failed, what would break if this were changed
back. The code already says what it does.

---

## Working agreements — do not break these

These are the author's explicit instructions.

- **Never commit or push unless authorised in that same message.** Not "you said
  push earlier". Each commit and each push needs its own go-ahead.
- **Never add an AI tool as author, co-author or contributor.** Commits are
  authored by the repository owner alone. No `Co-Authored-By`, no
  "Generated with" footer.
- **Never change the author's Django or superuser password**, or run anything
  that would. A management command once reset fifteen accounts including the
  author's own. Ask; let them run it.
- **Never touch the `feature/folder structure` branch.**
- **Never delete branches.**
- **Backend and frontend are separate repositories.** Never mix their changes in
  one commit. `momcare-backend` and `momcare_web`.
- **Do not commit secrets.** `.env` is git-ignored and must stay so. Check the
  staged diff before every commit.

### Scope

Do what was asked. If something else is wrong, say so and let the author decide
rather than widening the change. Unfinished work stays uncommitted rather than
being pushed to a live backend — `core/organization/api/dashboard.py` has sat
uncommitted for days for exactly this reason, and that is the correct state for
it until it has tests and something calls it.

---

## Before a commit

```bash
uv run ruff check .                    # must be clean on files you touched
uv run pytest momcare_platform/core -q # 335 tests, ~20s on a local database
```

`DATABASE_URL` in `.env` must point at **local** Postgres. If it points at Neon,
the suite takes ~53 minutes and produces failures that are network artefacts
rather than bugs. This has happened; do not debug those failures, fix the URL.

Commit messages: a short imperative subject, then prose explaining why. Look at
`git log` for the register — the existing messages are the specification.

---

## Current state, in one paragraph

The platform is **live at https://momcare.solutions** — Vercel for the portal,
Railway for the API at `api.momcare.solutions`, Neon for PostgreSQL, Resend for
email over its HTTPS interface because the host blocks outbound SMTP. Alert
escalation runs as a second Railway service on a five-minute cron. Seven
capabilities are complete and tested: hospital registration with a review gate,
six roles, staff invitations, patients and pregnancy, vitals and devices, risk
assessment, and alerts with a three-tier escalation ladder. Passwords can now be
changed and reset. Postgres Row-Level Security is written, tested, **and (1 Sep)
actually active in production** — `DATABASE_URL` connects as a restricted,
non-bypassing role; see `CLAUDE.md`'s Tenancy section. Care-team assignment
(`CareTeamMembership`, additive to `Pregnancy.assigned_staff`) and its API
are built and tested; the Patients and Alerts pages are now role-scoped by
it too (Provider and Nurse workspaces of the dashboard master plan) —
Care Manager's workspace is the remaining piece, its write side blocked on
an open decision, see `../docs/PLAN.md` §3 item 13. **335 backend tests,
30 frontend.**

What remains is listed in order in `../docs/PLAN.md`. The short version: an
obstetrician's review of the clinical thresholds, and the written report. The
NGO portal is cut.
