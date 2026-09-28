# cita-watch

Watches Spain's extranjería **cita previa** (icp.administracionelectronica.gob.es) and alerts you the moment slots open. It then leaves the browser sitting on the office/slot page so you can finish booking within the ~5-minute window.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml                    # then edit it
python cita_watch.py --test-alert                     # check notifications work
python cita_watch.py --once                           # one dry pass, watch what it does
python cita_watch.py                                  # start watching
```

Leave the terminal and the browser window open. Keep your phone nearby: the final step sends an SMS/email code.

## When it fires

1. You hear/see **CITA DISPONIBLE** (and get a Telegram message if configured).
2. In the browser: choose office → **Siguiente** → phone + email → pick a date/time → solve the CAPTCHA if shown → confirm → enter the code.
3. Save or print the *justificante*, then press ENTER in the terminal.

## Tips

- **Timing matters more than frequency.** Slots in Barcelona and Madrid are usually released in batches, often early morning on weekdays. Polling every 5 minutes during active hours works better than hammering the site, which gets you WAF-blocked ("The requested URL was rejected"). The script backs off 15 min automatically when that happens.
- If you get blocked often, set `browser_channel: chrome` and raise `interval_seconds`.
- To find your trámite code, open the site, select your province, and inspect the option value in the trámite dropdown.
- Selectors were current as of this writing. If the site changes, check `screenshots/` for the `unknown-*` captures and adjust the IDs in `check_once()`.

## Deliberate limits

The script doesn't solve CAPTCHAs or confirm the appointment itself. It pauses and alerts you when a CAPTCHA appears, and you do the final booking by hand. This keeps it within normal personal use of the site. Fully automated slot-grabbing is what resellers do, and Spanish police have made arrests over it.
