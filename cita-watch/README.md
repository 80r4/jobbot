# cita-watch

Watches Spain's extranjería **cita previa** (icp.administracionelectronica.gob.es) and alerts you the moment slots open. It then holds the browser on the office/slot page, pre-fills your phone and email, and lets you finish booking within the ~5-minute window.

It runs in two ways:

- **On your computer.** A visible Chrome window, with alerts in the terminal, on the desktop and in Telegram.
- **On a server with your own domain.** It runs 24/7. A password-protected web page shows its status, and an **Open browser** button lets you finish booking from your phone.

## Setup (your computer)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml                    # then edit it
python cita_watch.py --test-alert                     # check notifications work
python cita_watch.py --once                           # one dry pass, watch what it does
python cita_watch.py                                  # start watching
python cita_watch.py --panel 127.0.0.1:8080           # ...optionally with the web panel
```

The config is checked at startup. A typo in your NIE/DNI (wrong final letter), a bad phone number or impossible hours stops it right away, with a message saying what's wrong.

## Setup (server + your domain)

You need:

- A small Linux VPS with Docker (1 GB RAM is enough).
- A domain or subdomain whose DNS **A record** points at the server's IP, e.g. `cita.yourdomain.com`.

```bash
git clone <this repo> && cd jobbot/cita-watch
mkdir -p data && cp config.example.yaml data/config.yaml     # fill in your details
cp .env.example .env                                          # DOMAIN + login
docker run --rm caddy:2 caddy hash-password --plaintext 'a-long-password'
#   → paste the hash into .env as CITA_PASS_HASH='...'  (keep the single quotes)
docker compose up -d --build
docker compose logs -f watcher                                # watch it work
```

Open `https://cita.yourdomain.com` and log in. Caddy gets the HTTPS certificate automatically. The page shows the current status, the next check, today's results and the latest screenshot. It has these buttons:

| Button | What it does |
|---|---|
| **Open browser** | The watcher's real browser, in your phone or laptop browser (noVNC). Book here. |
| **Done / I've handled it** | Tells it you've booked, or dealt with a CAPTCHA. After a booking it pauses watching. |
| **Check now** | Skips the wait and checks immediately. |
| **Pause / Resume** | Stops or restarts watching. |

**Before you pick a server:** the site's firewall blocks many datacenter IPs outright. Run `docker compose logs watcher` after the first few checks. If they all come back `blocked`, run it on a machine at home instead (an old laptop or a Raspberry Pi with Docker). Then either forward ports 80/443 on your router, or use a **Cloudflare Tunnel**: point the tunnel at `http://localhost:80`, set `DOMAIN=:80` in `.env`, and drop the `443` port. Cloudflare then handles HTTPS.

**Already running JobBot on the same server?** Only one Caddy can use ports 80/443. Don't start this project's `caddy` service (`docker compose up -d watcher`). Instead, put both projects on the same Docker network and add the `{$DOMAIN} { ... }` block from this `Caddyfile` to JobBot's `Caddyfile`, with your cita domain.

## When it fires

1. You get **CITA DISPONIBLE** as a sound, a desktop notification and a Telegram message with a screenshot of the offices.
2. In the browser (or **Open browser** on the panel): choose an office → **Siguiente**. Your phone and email are already filled in. Then pick a date/time, solve the CAPTCHA if one appears, confirm and enter the SMS code.
3. Save the *justificante*, then press ENTER in the terminal or **Done** on the panel.

If you don't respond within `available_wait_minutes` (default 10), it goes back to watching instead of waiting forever. CAPTCHA prompts time out the same way (`human_wait_minutes`).

## Alerts you'll get

- **CITA DISPONIBLE**: slots are open.
- **Needs you**: a CAPTCHA, or a field only you can fill in.
- **Rate-limited**: the site blocked a request. The wait doubles each time it happens in a row: 15 → 30 → 60 → 120 min.
- **May be broken**: 3 checks in a row ended as unknown, error or timeout, which usually means the site changed. Comes with a screenshot.
- **Still running**: one message a day at `heartbeat_hour`, with that day's counts.

## Tips

- **Timing matters more than frequency.** Slots are usually released in batches, often on weekday mornings. `peak_windows` checks more often at those times (not below 2 min) and less often the rest of the day.
- **Learn your province's pattern.** Every check is logged to `history.csv`. After a few days, `python cita_watch.py --stats` shows which weekday and hour slots appeared, so you can adjust `peak_windows`.
- All hours in the config are **Spanish time**, whatever time zone your computer or server uses.
- If you get blocked often on your own computer, set `browser_channel: chrome` and raise `interval_seconds`.
- To find your trámite code, open the site, select your province, and inspect the option value in the trámite dropdown.
- Selectors were current as of this writing. If the site changes, check `screenshots/` for the `unknown-*` captures and adjust the IDs in `check_once()`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q tests          # includes a full run against a fake cita site (needs Chromium)
```

## Deliberate limits

The script doesn't solve CAPTCHAs, confirm the appointment itself, or try to evade the site's rate limiting. It pauses and alerts you when a CAPTCHA appears, and you do the final booking by hand. This keeps it within normal personal use of the site. Fully automated slot-grabbing is what resellers do, and Spanish police have made arrests over it.
