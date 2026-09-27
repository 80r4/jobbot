# JobBot

A personal job-search bot. Every few hours it pulls postings from job APIs,
filters them by your rules (title, keywords, location, age, exclusions),
stores the matches, pings you on Telegram/email, and gives you a private web
dashboard on your own domain to review them, draft cover letters, and track
what you've applied to.

```
Adzuna API ─┐
Greenhouse ─┤→ filter & score → SQLite → dashboard (https://jobs.yourdomain.com)
Lever      ─┤                        └──→ Telegram / email alerts
Ashby      ─┘
```

## About "apply automatically"

The bot does **not** submit applications on its own, on purpose:

- LinkedIn, Indeed, InfoJobs etc. forbid bots in their terms and ban accounts
  that auto-apply — and they use CAPTCHAs specifically to stop it.
- Company application systems (Greenhouse, Lever, Workday…) only accept
  programmatic applications with the *employer's* API key.
- Mass auto-applications also tend to hurt more than help: recruiters see
  generic, mismatched submissions.

What it does instead is get you to "apply" in about a minute per job:
it finds and ranks matches, opens the application page with one click,
optionally drafts a tailored cover letter from your profile, and tracks
status (new → shortlisted → applied → interview / rejected).

## Files

| File | What it is |
|---|---|
| `config.yaml` | Your filters, sources, schedule — **edit this** |
| `profile.md` | Your CV in plain text (only used for cover-letter drafts) |
| `.env` | Secrets: API keys, dashboard password, domain |
| `jobbot/` | The code (sources, filters, DB, dashboard, notifications) |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile` | Hosting on your domain with HTTPS |

## 1. Get the keys

1. **Adzuna** (main source; covers Spain and most of Europe): sign up at
   developer.adzuna.com → copy *App ID* and *App Key*.
2. **Telegram alerts** (optional): message `@BotFather` → `/newbot` → copy the
   token. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. **Cover letters** (optional): an Anthropic API key from console.anthropic.com,
   and set `cover_letter.enabled: true` in `config.yaml`.

## 2. Run it locally first

```bash
cp .env.example .env          # fill in the values
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m jobbot.bot          # one search run, prints counts
uvicorn jobbot.app:app --port 8000
# open http://localhost:8000  (login = DASHBOARD_USER / DASHBOARD_PASS)
```

Tune `config.yaml` until the matches look right. If you get too few results,
loosen `title_any` or `locations_any`; too many, add to `exclude_title_any`.

**Adding specific companies:** look at a company's careers URL. If it's
`boards.greenhouse.io/acme`, add `acme` under `greenhouse:`; `jobs.lever.co/acme`
→ `lever:`; `jobs.ashbyhq.com/acme` → `ashby:`.

## 3. Host it on your domain

You need a domain and a small always-on server. The simplest reliable setup
is a cheap Linux VPS (Hetzner, DigitalOcean, OVH, Scaleway — the smallest
plan is plenty) running Docker, with Caddy handling HTTPS automatically.

**a. Create the server** — Ubuntu 24.04, smallest size. Note its public IP.

**b. Point your domain at it** — in your domain registrar's DNS settings
(Namecheap, Cloudflare, Porkbun…), add:

| Type | Name | Value |
|---|---|---|
| A | `jobs` | `<server IP>` |

That makes `jobs.yourdomain.com` point at the server (use `@` instead of
`jobs` for the bare domain). If you use Cloudflare, set the record to
"DNS only" (grey cloud) so Caddy can get its certificate.

**c. Install Docker and start the bot** (SSH into the server):

```bash
curl -fsSL https://get.docker.com | sh
# copy this folder to the server, e.g. from your laptop:
#   scp -r jobbot root@<server IP>:/opt/jobbot
cd /opt/jobbot
cp .env.example .env && nano .env      # set DOMAIN=jobs.yourdomain.com + keys + password
docker compose up -d --build
docker compose logs -f app             # watch the first search run
```

Open `https://jobs.yourdomain.com` — Caddy fetches a free HTTPS certificate
on first visit (give it a minute after DNS propagates). Log in with your
dashboard user/password.

**d. Day-to-day**

```bash
nano config.yaml && docker compose restart app   # change filters
docker compose pull && docker compose up -d --build   # update
cp data/jobs.db ~/jobs-backup.db                 # back up your tracker
```

Make sure the firewall allows ports 80 and 443 (`ufw allow 80,443/tcp` if
ufw is on).

### Cheaper / no-server alternatives

- **Render / Railway / Fly.io**: deploy the `Dockerfile` directly, add your
  environment variables in their dashboard, and attach a custom domain in
  their settings (they give you a CNAME to add at your registrar). You must
  attach a persistent disk/volume mounted at `/data`, or the database resets
  on each deploy. Free tiers that sleep when idle will also pause the
  scheduler.
- **No dashboard at all**: run `python -m jobbot.bot` on a schedule (cron,
  or a GitHub Actions workflow) and rely on Telegram/email alerts.

## Security notes

- The dashboard is behind a password; use a long one. Never commit `.env`.
- Only official public APIs are called, at a modest rate (default every 3 h).
