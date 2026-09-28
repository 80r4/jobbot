#!/usr/bin/env python3
"""
cita_watch — monitor Spain's extranjería cita previa (ICP) for open slots.

What it does
  * Drives a real, visible Chrome/Chromium window through the ICP flow
    (province -> trámite -> your details -> "Solicitar Cita").
  * If the site says "no hay citas disponibles", it waits and tries again.
  * The moment slots appear (office selection / date grid), it STOPS,
    alerts you (sound + desktop notification + optional Telegram), and
    leaves the window open on that page so you can finish booking yourself.

What it deliberately does NOT do
  * Solve CAPTCHAs, confirm the appointment, or enter SMS codes.
    If a CAPTCHA shows up, it pauses and alerts you to solve it.
  * Poll aggressively. Minimum interval is enforced (see MIN_INTERVAL).

Usage
  pip install -r requirements.txt && playwright install chromium
  cp config.example.yaml config.yaml   # fill in your details
  python cita_watch.py                 # or: python cita_watch.py --once
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import platform
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

BASE = "https://icp.administracionelectronica.gob.es"
MIN_INTERVAL = 120          # seconds; be kind to the server (and avoid WAF bans)
BLOCK_BACKOFF = 15 * 60     # wait this long after the WAF rejects us
STEP_TIMEOUT = 30_000       # ms

# Provinces with a non-default URL prefix / trámite select name.
# Everything else uses ("icpplus", "tramiteGrupo[1]").
PROVINCE_ROUTING = {
    "8": ("icpplustieb", "tramiteGrupo[0]"),   # Barcelona
    "28": ("icpplustiem", "tramiteGrupo[1]"),  # Madrid
    "29": ("icpco", "tramiteGrupo[0]"),        # Málaga
    "3": ("icpco", "tramiteGrupo[1]"),         # Alicante
    "7": ("icpco", "tramiteGrupo[1]"),         # Illes Balears
    "35": ("icpco", "tramiteGrupo[1]"),        # Las Palmas
    "38": ("icpco", "tramiteGrupo[1]"),        # S.C. Tenerife
    "41": ("icpplus", "tramiteGrupo[0]"),      # Sevilla
    "52": ("icpplus", "tramiteGrupo[0]"),      # Melilla
}

# Page-text markers (Spanish UI)
TXT_LANDING = "INTERNET CITA PREVIA"
TXT_NONE = "no hay citas disponibles"
TXT_OFFICE = "Seleccione la oficina"
TXT_SLOTS = ("Seleccione una de las siguientes citas disponibles", "DISPONE DE 5 MINUTOS")
TXT_BLOCKED = ("The requested URL was rejected", "Too Many Requests", "Access Denied")

log = logging.getLogger("cita")


# --------------------------------------------------------------------------- config
@dataclass
class Config:
    province: str
    tramite: str
    doc_type: str            # nie | passport | dni
    doc_number: str
    full_name: str
    country: Optional[str] = None        # as shown in the site's dropdown, e.g. "MARRUECOS"
    birth_year: Optional[str] = None     # only some trámites ask for it
    interval_seconds: int = 300
    jitter_seconds: int = 90
    active_hours: Optional[list] = None  # e.g. [8, 23] -> only check 08:00–22:59
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    browser_channel: Optional[str] = None  # "chrome" to use your installed Chrome
    profile_dir: str = ".browser-profile"
    screenshots_dir: str = "screenshots"
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        known = {k: data.pop(k) for k in list(data) if k in cls.__dataclass_fields__}
        cfg = cls(**known, extra=data)
        cfg.province = str(cfg.province)
        cfg.tramite = str(cfg.tramite)
        cfg.doc_type = cfg.doc_type.lower()
        if cfg.doc_type not in {"nie", "passport", "dni"}:
            sys.exit("doc_type must be one of: nie, passport, dni")
        if cfg.interval_seconds < MIN_INTERVAL:
            log.warning("interval_seconds raised to %ss (minimum)", MIN_INTERVAL)
            cfg.interval_seconds = MIN_INTERVAL
        return cfg


# --------------------------------------------------------------------------- alerts
def notify(cfg: Config, title: str, message: str, loud: bool = True) -> None:
    log.info("%s — %s", title, message)
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{message}" with title "{title}" sound name "Glass"'],
                           check=False)
            if loud:
                subprocess.Popen(["say", title])
        elif system == "Linux":
            subprocess.run(["notify-send", "-u", "critical", title, message], check=False)
        elif system == "Windows":
            import winsound  # type: ignore
            for _ in range(5):
                winsound.Beep(1200, 300)
    except FileNotFoundError:
        pass
    if loud:
        for _ in range(5):
            sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.3)
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                data={"chat_id": cfg.telegram_chat_id, "text": f"{title}\n{message}"},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram notification failed: %s", e)


# --------------------------------------------------------------------------- page helpers
def body_text(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=STEP_TIMEOUT)
    except PWTimeout:
        return ""


def classify(text: str) -> str:
    """Map the current page text to a state."""
    if any(m in text for m in TXT_BLOCKED):
        return "blocked"
    if TXT_NONE in text.lower() or TXT_NONE in text:
        return "none"
    if TXT_OFFICE in text or any(m in text for m in TXT_SLOTS):
        return "available"
    return "unknown"


def has_visible_captcha(page: Page) -> bool:
    # Image captcha (text box + picture). reCAPTCHA v3 is invisible and runs
    # on its own in a normal browser, so it isn't treated as a blocker here.
    return page.locator("img.img-thumbnail, #captcha").count() > 0 or \
        page.locator("iframe[src*='recaptcha'][title*='challenge']").count() > 0


def wait_for_human(cfg: Config, page: Page, reason: str) -> None:
    notify(cfg, "Cita watch needs you", reason)
    input(f"\n>>> {reason}\n>>> Do it in the browser window, then press ENTER here to continue... ")


def screenshot(cfg: Config, page: Page, tag: str) -> Path:
    d = Path(cfg.screenshots_dir); d.mkdir(exist_ok=True)
    p = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{tag}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
    except Exception:  # noqa: BLE001
        pass
    return p


def pause(a: float = 1.0, b: float = 2.5) -> None:
    time.sleep(random.uniform(a, b))


# --------------------------------------------------------------------------- the flow
def check_once(cfg: Config, page: Page) -> str:
    prefix, select_name = PROVINCE_ROUTING.get(cfg.province, ("icpplus", "tramiteGrupo[1]"))

    # 1. Province landing (sets the session), then jump to the trámite info page.
    page.goto(f"{BASE}/{prefix}/citar?p={cfg.province}&locale=es", timeout=60_000)
    pause(2, 4)
    page.goto(f"{BASE}/{prefix}/acInfo?{select_name}={cfg.tramite}", timeout=60_000)
    pause()
    text = body_text(page)
    state = classify(text)
    if state == "blocked":
        return "blocked"
    if TXT_LANDING not in text:
        screenshot(cfg, page, "unexpected-landing")
        return "unknown"

    # 2. Instructions page -> "Entrar"
    page.locator("#btnEntrar").click(timeout=STEP_TIMEOUT)
    page.wait_for_selector("#txtIdCitado", timeout=STEP_TIMEOUT)
    pause()

    # 3. Personal details (fields vary by trámite, so fill only what exists)
    radio = {"nie": "#rdbTipoDocNie", "passport": "#rdbTipoDocPas", "dni": "#rdbTipoDocDni"}[cfg.doc_type]
    if page.locator(radio).count():
        page.locator(radio).check()
    page.fill("#txtIdCitado", cfg.doc_number)
    pause(0.4, 1.0)
    page.fill("#txtDesCitado", cfg.full_name)
    if cfg.birth_year and page.locator("#txtAnnoCitado").count():
        page.fill("#txtAnnoCitado", str(cfg.birth_year))
    if page.locator("#txtPaisNac").count():
        if not cfg.country:
            wait_for_human(cfg, page, "This trámite asks for country of nationality — pick it in the browser")
        else:
            page.select_option("#txtPaisNac", label=cfg.country)
    pause()

    if has_visible_captcha(page):
        wait_for_human(cfg, page, "CAPTCHA on the details page — solve it (don't press Aceptar)")

    page.locator("#btnEnviar").click(timeout=STEP_TIMEOUT)
    pause(2, 3)

    # 4. "Solicitar Cita"
    state = classify(body_text(page))
    if state in ("blocked", "none", "available"):
        return state
    solicitar = page.locator("#btnEnviar")
    if solicitar.count():
        solicitar.click(timeout=STEP_TIMEOUT)
    else:
        page.evaluate("typeof enviar === 'function' && enviar('solicitud')")
    page.wait_for_load_state("domcontentloaded", timeout=STEP_TIMEOUT)
    pause(1.5, 2.5)

    # 5. Result
    state = classify(body_text(page))
    if state == "unknown":
        screenshot(cfg, page, "unknown-result")
    return state


def release_session(page: Page) -> None:
    """Click 'Salir' if present so we don't leave a half-open session behind."""
    try:
        btn = page.locator("#btnSalir")
        if btn.count():
            btn.first.click(timeout=5_000)
    except Exception:  # noqa: BLE001
        pass


def within_active_hours(cfg: Config) -> bool:
    if not cfg.active_hours:
        return True
    start, end = cfg.active_hours
    return start <= dt.datetime.now().hour < end


# --------------------------------------------------------------------------- main loop
def run(cfg: Config, once: bool) -> None:
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            cfg.profile_dir,
            headless=False,                 # visible on purpose: you finish the booking
            channel=cfg.browser_channel,
            locale="es-ES",
            timezone_id="Europe/Madrid",
            viewport={"width": 1200, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        attempt = 0
        try:
            while True:
                if not within_active_hours(cfg):
                    log.info("Outside active hours; sleeping 10 min")
                    time.sleep(600)
                    continue

                attempt += 1
                try:
                    state = check_once(cfg, page)
                except PWTimeout:
                    state = "timeout"
                except Exception as e:  # noqa: BLE001
                    log.exception("Unexpected error: %s", e)
                    screenshot(cfg, page, "error")
                    state = "error"

                log.info("Attempt %d → %s", attempt, state)

                if state == "available":
                    shot = screenshot(cfg, page, "AVAILABLE")
                    notify(cfg, "CITA DISPONIBLE!",
                           "Slots are open — finish the booking in the browser now "
                           f"(you usually have ~5 min). Screenshot: {shot}")
                    input("\n>>> Slots found. Complete the booking in the browser "
                          "(office → contact → date → confirm/SMS).\n"
                          ">>> Press ENTER here to close the watcher once you're done... ")
                    return

                if state == "blocked":
                    notify(cfg, "Cita watch: rate-limited",
                           f"The site rejected the request; backing off {BLOCK_BACKOFF // 60} min.",
                           loud=False)
                    time.sleep(BLOCK_BACKOFF)
                    continue

                release_session(page)
                if once:
                    return
                wait = cfg.interval_seconds + random.randint(0, cfg.jitter_seconds)
                log.info("Next check in %ds", wait)
                time.sleep(wait)
        finally:
            ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch Spain's cita previa (extranjería) for open slots.")
    ap.add_argument("-c", "--config", default="config.yaml", type=Path)
    ap.add_argument("--once", action="store_true", help="check a single time and exit")
    ap.add_argument("--test-alert", action="store_true", help="fire a test notification and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    if not args.config.exists():
        sys.exit(f"Config not found: {args.config} (copy config.example.yaml)")
    cfg = Config.load(args.config)

    if args.test_alert:
        notify(cfg, "Cita watch test", "If you can see/hear this, alerts work.")
        return
    run(cfg, args.once)


if __name__ == "__main__":
    main()
