# AMP — Production setup checklist

The code-level hardening is done (bcrypt passwords, locked CORS, optional Sentry).
The items below need your accounts/dashboards — they can't be done from code alone.
Do all of these **before** a paying customer's real data goes in.

## 1. Environment variables (Railway → AMP service → Variables)

| Variable | Set to | Why |
|---|---|---|
| `SECRET_KEY` | a long random string (e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`) | JWT signing. **Change from the default.** |
| `ALLOWED_ORIGINS` | `https://flow-mes.vercel.app` (add your custom domain comma-separated once you have it) | CORS allowlist |
| `GMATS_ADMIN_PASSWORD` | a strong password | creates the `gmats_admin` login |
| `SENTRY_DSN` | your Sentry project DSN (see §3) | turns on error monitoring |

## 2. Database backups (Railway)

Railway Postgres does **not** back up automatically on the free/hobby tier.
Pick one:

- **Easiest:** upgrade the Postgres plugin to a plan with managed backups and enable
  scheduled backups in the Railway dashboard.
- **Free fallback:** run a daily `pg_dump` to off-box storage. A scheduled GitHub
  Action works well:
  1. Add repo secret `DATABASE_PUBLIC_URL` (the Railway public connection string).
  2. Add `.github/workflows/backup.yml` that runs `pg_dump "$DATABASE_PUBLIC_URL"`
     on a cron and uploads the dump as an artifact / to S3 / to Google Drive.
- **Test the restore** at least once. A backup you've never restored is not a backup.

## 3. Error monitoring (Sentry) — free tier is enough

1. Create a free account at sentry.io → new project → **FastAPI**.
2. Copy the **DSN** and set it as `SENTRY_DSN` in Railway.
3. Redeploy. Errors now flow to Sentry (the code is already wired, gated on the DSN).

## 4. Uptime monitoring (free)

1. Create a free monitor at **UptimeRobot** (or BetterStack).
2. Monitor `https://flowmes-production.up.railway.app/` every 5 minutes.
3. Add your email/SMS/WhatsApp for alerts. You want to hear it's down from the
   monitor, not from the customer.

## 5. Custom domain

1. Buy `marx8.com` (or similar).
2. Point it at Vercel (frontend) and add it to the Vercel project domains.
3. Add the apex/`api.` subdomain for the backend in Railway if you want a branded API.
4. Add the new origin(s) to `ALLOWED_ORIGINS` in Railway.

## 6. Still on the backlog (not blocking the first customer)

- Automated tests + CI (none yet) — add as the codebase grows.
- Token refresh / shorter JWT expiry.
- Per-company invoice/branding settings (currently a constant for GMATS).
