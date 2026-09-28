#!/usr/bin/env python3
"""
cita_watch — monitor Spain's extranjería cita previa (ICP) for open slots.

What it does
  * Drives a real, visible Chrome/Chromium window through the ICP flow
    (province -> trámite -> your details -> "Solicitar Cita").
  * If the site says "no hay citas disponibles", it waits and tries again.
  * The moment slots appear (office selection / date grid), it STOPS,
    alerts you (sound + desktop notification + Telegram with screenshot),
    pre-fills your phone/email on the contact page, and leaves the window
    on that page so you can finish booking yourself.
  * If you don't respond within a few minutes, it goes back to watching.

What it deliberately does NOT do
  * Solve CAPTCHAs, confirm the appointment, or enter SMS codes.
    If a CAPTCHA shows up, it pauses and alerts you to solve it.
  * Poll aggressively. Minimum interval is enforced (see MIN_INTERVAL).

Usage
  pip install -r requirements.txt && playwright install chromium
  cp config.example.yaml config.yaml   # fill in your details
  python cita_watch.py                 # or: --once, --test-alert, --stats
  python cita_watch.py --server        # headless-server mode with web control panel
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import logging
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

BASE = os.environ.get("CITA_BASE_URL", "https://icp.administracionelectronica.gob.es")
MADRID = ZoneInfo("Europe/Madrid")
MIN_INTERVAL = 120          # seconds; be kind to the server (and avoid WAF bans)
BLOCK_BACKOFF = 15 * 60     # first wait after the WAF rejects us; doubles on repeats
MAX_BACKOFF = 2 * 60 * 60   # ... up to this
BROKEN_ALERT_AFTER = 3      # alert after this many unknown/error/timeout results in a row
BROKEN_ALERT_REPEAT = 12    # ... and again every this many more
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

# Contact-page fields that get pre-filled while you book (only if empty)
CONTACT_FIELDS = (("#txtTelefonoCitado", "phone"), ("#emailUNO", "email"), ("#emailDOS", "email"))

NIE_RE = re.compile(r"^([XYZ])(\d{7})([A-Z])$")
DNI_RE = re.compile(r"^(\d{8})([A-Z])$")
PASSPORT_RE = re.compile(r"^[A-Z0-9]{5,20}$")
ID_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"

log = logging.getLogger("cita")


def now_madrid() -> dt.datetime:
    return dt.datetime.now(MADRID)


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
    phone: Optional[str] = None          # pre-filled on the contact page
    email: Optional[str] = None          # pre-filled on the contact page
    interval_seconds: int = 300
    jitter_seconds: int = 90
    active_hours: Optional[list] = None  # e.g. [8, 23] -> only check 08:00–22:59 Madrid time
    peak_windows: list = field(default_factory=list)
    available_wait_minutes: int = 10     # how long to hold the slot page for you
    human_wait_minutes: int = 10         # how long to wait for you to solve a CAPTCHA etc.
    heartbeat_hour: Optional[int] = 9    # daily "still alive" message (Madrid hour), null = off
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    browser_channel: Optional[str] = None  # "chrome" to use your installed Chrome
    profile_dir: str = ".browser-profile"
    screenshots_dir: str = "screenshots"
    history_file: str = "history.csv"
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        known = {k: data.pop(k) for k in list(data) if k in cls.__dataclass_fields__ and k != "extra"}
        missing = [k for k in ("province", "tramite", "doc_type", "doc_number", "full_name") if k not in known]
        if missing:
            sys.exit(f"Config error: missing {', '.join(missing)}")
        cfg = cls(**known, extra=data)
        errors = cfg.normalize_and_validate()
        if errors:
            sys.exit("Config errors:\n  - " + "\n  - ".join(errors))
        # Relative paths live next to the config file (so /data/config.yaml -> /data/...)
        base = path.resolve().parent
        for attr in ("profile_dir", "screenshots_dir", "history_file"):
            p = Path(getattr(cfg, attr))
            setattr(cfg, attr, str(p if p.is_absolute() else base / p))
        return cfg

    def normalize_and_validate(self) -> list[str]:
        errors: list[str] = []
        self.province = str(self.province).strip()
        self.tramite = str(self.tramite).strip()
        self.doc_type = str(self.doc_type).lower().strip()
        self.doc_number = str(self.doc_number).upper().replace(" ", "").replace("-", "")
        self.full_name = str(self.full_name or "").strip().upper()
        if self.country:
            self.country = str(self.country).strip().upper()

        if not self.province.isdigit():
            errors.append(f"province must be a number (got {self.province!r})")
        if not self.tramite.isdigit():
            errors.append(f"tramite must be a number (got {self.tramite!r})")
        if not self.full_name:
            errors.append("full_name is empty")

        if self.doc_type not in {"nie", "passport", "dni"}:
            errors.append("doc_type must be one of: nie, passport, dni")
        else:
            err = validate_document(self.doc_type, self.doc_number)
            if err:
                errors.append(err)

        if self.birth_year is not None:
            by = str(self.birth_year)
            if not (by.isdigit() and 1900 <= int(by) <= now_madrid().year):
                errors.append(f"birth_year looks wrong: {by!r}")
            self.birth_year = by

        if self.phone is not None:
            self.phone = re.sub(r"[\s-]", "", str(self.phone))
            if not re.fullmatch(r"[6-9]\d{8}", self.phone):
                errors.append("phone must be a 9-digit Spanish number without +34 (e.g. 612345678)")
        if self.email is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(self.email)):
            errors.append(f"email looks wrong: {self.email!r}")

        if self.active_hours is not None:
            if not _valid_hours(self.active_hours):
                errors.append("active_hours must be [start, end] with 0 <= start < end <= 24, or null")

        if not isinstance(self.peak_windows, list):
            errors.append("peak_windows must be a list")
        else:
            for i, w in enumerate(self.peak_windows):
                if not isinstance(w, dict) or not _valid_hours(w.get("hours")):
                    errors.append(f"peak_windows[{i}].hours must be [start, end] with 0 <= start < end <= 24")
                    continue
                days = w.get("days", [0, 1, 2, 3, 4])
                if not (isinstance(days, list) and days and all(isinstance(d, int) and 0 <= d <= 6 for d in days)):
                    errors.append(f"peak_windows[{i}].days must be a list of 0 (Mon) .. 6 (Sun)")
                w["days"] = days
                iv = int(w.get("interval_seconds", self.interval_seconds))
                if iv < MIN_INTERVAL:
                    log.warning("peak_windows[%d].interval_seconds raised to %ss (minimum)", i, MIN_INTERVAL)
                    iv = MIN_INTERVAL
                w["interval_seconds"] = iv

        if self.interval_seconds < MIN_INTERVAL:
            log.warning("interval_seconds raised to %ss (minimum)", MIN_INTERVAL)
            self.interval_seconds = MIN_INTERVAL
        if self.jitter_seconds < 0:
            errors.append("jitter_seconds can't be negative")
        if self.heartbeat_hour is not None and not (0 <= int(self.heartbeat_hour) <= 23):
            errors.append("heartbeat_hour must be 0..23 or null")
        for name in ("available_wait_minutes", "human_wait_minutes"):
            if int(getattr(self, name)) < 1:
                errors.append(f"{name} must be at least 1")
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            errors.append("set both telegram_bot_token and telegram_chat_id, or neither")
        return errors


def _valid_hours(h) -> bool:
    return (isinstance(h, (list, tuple)) and len(h) == 2 and all(isinstance(x, int) for x in h)
            and 0 <= h[0] < h[1] <= 24)


def validate_document(doc_type: str, number: str) -> Optional[str]:
    """Return an error message, or None if the document number looks valid."""
    if doc_type == "nie":
        m = NIE_RE.match(number)
        if not m:
            return f"doc_number {number!r} isn't a NIE (format: X/Y/Z + 7 digits + letter)"
        digits = "XYZ".index(m.group(1)) * 10_000_000 + int(m.group(2))
        if ID_LETTERS[digits % 23] != m.group(3):
            return f"NIE {number!r}: the final letter doesn't match the digits — check for a typo"
    elif doc_type == "dni":
        m = DNI_RE.match(number)
        if not m:
            return f"doc_number {number!r} isn't a DNI (format: 8 digits + letter)"
        if ID_LETTERS[int(m.group(1)) % 23] != m.group(2):
            return f"DNI {number!r}: the final letter doesn't match the digits — check for a typo"
    elif not PASSPORT_RE.match(number):
        return f"passport number {number!r} should be 5-20 letters/digits"
    return None


# --------------------------------------------------------------------------- alerts
def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(cfg: Config, title: str, message: str, loud: bool = True,
           photo: Optional[Path] = None) -> None:
    log.info("%s — %s", title, message)
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e",
                            f"display notification {_applescript_str(message)} "
                            f"with title {_applescript_str(title)} sound name \"Glass\""],
                           check=False)
            if loud:
                subprocess.Popen(["say", title])
        elif system == "Linux":
            subprocess.run(["notify-send", "-u", "critical", title, message], check=False,
                           stderr=subprocess.DEVNULL)
        elif system == "Windows":
            import winsound  # type: ignore
            for _ in range(5):
                winsound.Beep(1200, 300)
    except (FileNotFoundError, OSError):
        pass
    if loud and sys.stdout.isatty():
        for _ in range(5):
            sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.3)
    send_telegram(cfg, f"{title}\n{message}", photo)


def send_telegram(cfg: Config, text: str, photo: Optional[Path] = None) -> None:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return
    import requests
    api = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    try:
        if photo and Path(photo).exists():
            with open(photo, "rb") as fh:
                r = requests.post(f"{api}/sendPhoto",
                                  data={"chat_id": cfg.telegram_chat_id, "caption": text[:1024]},
                                  files={"photo": fh}, timeout=30)
            if r.ok:
                return
            log.warning("Telegram photo failed (%s); sending text only", r.status_code)
        requests.post(f"{api}/sendMessage",
                      data={"chat_id": cfg.telegram_chat_id, "text": text}, timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram notification failed: %s", e)


# --------------------------------------------------------------------------- control (terminal + web)
class Control:
    """Shared state between the watcher loop, the terminal and the web panel."""

    ACTIONS = ("done", "pause", "unpause", "check_now")

    def __init__(self, server_mode: bool):
        self.server_mode = server_mode
        self.lock = threading.Lock()
        self.done = threading.Event()     # "I've handled it" (ENTER / Done button)
        self.wake = threading.Event()     # interrupt a sleep (check now / unpause)
        self.paused = False
        self.status = "starting"
        self.detail = ""
        self.attempt = 0
        self.last_state: Optional[str] = None
        self.last_check: Optional[str] = None
        self.next_check: Optional[str] = None
        self.wait_deadline: Optional[str] = None
        self.last_screenshot: Optional[str] = None
        self.recent: collections.deque = collections.deque(maxlen=30)
        self.today = collections.Counter()
        self.today_date = now_madrid().date()

    def set(self, status: str, detail: str = "", **kw) -> None:
        with self.lock:
            self.status, self.detail = status, detail
            for k, v in kw.items():
                setattr(self, k, v)

    def record(self, state: str) -> None:
        with self.lock:
            now = now_madrid()
            if now.date() != self.today_date:
                self.today_date, self.today = now.date(), collections.Counter()
            self.today[state] += 1
            self.last_state, self.last_check = state, now.isoformat(timespec="seconds")
            self.recent.appendleft({"time": self.last_check, "attempt": self.attempt, "state": state})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": "paused" if self.paused and self.status != "waiting" else self.status,
                "detail": self.detail, "paused": self.paused, "attempt": self.attempt,
                "server_mode": self.server_mode,
                "last_state": self.last_state, "last_check": self.last_check,
                "next_check": self.next_check, "wait_deadline": self.wait_deadline,
                "has_screenshot": bool(self.last_screenshot),
                "today": dict(self.today), "recent": list(self.recent),
            }

    def act(self, action: str) -> None:
        if action == "done":
            self.done.set()
        elif action == "pause":
            with self.lock:
                self.paused = True
        elif action == "unpause":
            with self.lock:
                self.paused = False
            self.wake.set()
        elif action == "check_now":
            with self.lock:
                self.paused = False
            self.wake.set()

    def sleep(self, seconds: float) -> None:
        """Sleep, but wake early on 'check now' / 'unpause'."""
        self.wake.clear()
        self.wake.wait(seconds)

    def start_stdin_reader(self) -> None:
        if not sys.stdin or not sys.stdin.isatty():
            return

        def reader():
            for _ in sys.stdin:
                self.done.set()
        threading.Thread(target=reader, daemon=True, name="stdin").start()


PANEL_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cita watch</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#5b6470;--line:#e2e5ea;--accent:#0b63ce;--ok:#1a7f37;--warn:#b35900;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f25;--fg:#e8eaed;--muted:#9aa3ad;--line:#2a3038;--accent:#5aa2ff;--ok:#4cc26a;--warn:#f0a040;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:20px;margin:0 0 14px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.status{font-size:22px;font-weight:650}.detail{color:var(--muted);margin-top:4px;overflow-wrap:anywhere}
.s-available,.s-waiting{color:var(--ok)}.s-blocked,.s-paused{color:var(--warn)}.s-error,.s-broken{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:12px}
.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.v{font-variant-numeric:tabular-nums}
.btns{display:flex;flex-wrap:wrap;gap:8px}button,.btn{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--fg);padding:10px 14px;border-radius:8px;font:inherit;cursor:pointer;text-decoration:none}
.primary{background:var(--accent);border-color:var(--accent);color:#fff}
table{width:100%;border-collapse:collapse;font-size:14px}td{padding:6px 4px;border-top:1px solid var(--line)}td:last-child{text-align:right}
img{max-width:100%;border-radius:6px;border:1px solid var(--line)}
</style></head><body><main>
<h1>Cita watch</h1>
<div class="card"><div id="status" class="status">…</div><div id="detail" class="detail"></div>
<div class="grid">
<div><div class="k">Last check</div><div class="v" id="last"></div></div>
<div><div class="k">Next check</div><div class="v" id="next"></div></div>
<div><div class="k">Attempts</div><div class="v" id="attempt"></div></div>
<div><div class="k">Today</div><div class="v" id="today"></div></div>
</div></div>
<div class="card btns">
<a class="btn primary" id="vnc" hidden href="vnc/vnc.html?path=vnc/websockify&autoconnect=true&resize=scale" target="_blank">Open browser</a>
<button data-a="done">Done / I've handled it</button>
<button data-a="check_now">Check now</button>
<button data-a="pause" id="pause">Pause</button>
<button data-a="unpause" id="unpause">Resume</button>
</div>
<div class="card" id="shotcard" hidden><div class="k" style="margin-bottom:8px">Latest screenshot</div><a id="shotlink" target="_blank"><img id="shot" alt="Latest screenshot"></a></div>
<div class="card"><div class="k" style="margin-bottom:6px">Recent checks</div><table id="recent"></table></div>
</main><script>
const H={'X-Requested-With':'cita'};
const t=s=>s?new Date(s).toLocaleString([], {dateStyle:'short',timeStyle:'medium'}):'—';
async function refresh(){try{const r=await fetch('api/status',{headers:H,cache:'no-store'});const s=await r.json();
const el=document.getElementById('status');el.textContent=s.status.replace(/_/g,' ');el.className='status s-'+s.status;
document.getElementById('detail').textContent=s.detail||'';
document.getElementById('last').textContent=s.last_check?t(s.last_check)+' · '+s.last_state:'—';
document.getElementById('next').textContent=s.wait_deadline?'waiting for you until '+t(s.wait_deadline):t(s.next_check);
document.getElementById('attempt').textContent=s.attempt;
document.getElementById('today').textContent=Object.entries(s.today).map(([k,v])=>k+' '+v).join(', ')||'—';
document.getElementById('vnc').hidden=!s.server_mode;document.getElementById('pause').hidden=s.paused;document.getElementById('unpause').hidden=!s.paused;
const sc=document.getElementById('shotcard');sc.hidden=!s.has_screenshot;
if(s.has_screenshot){const u='api/screenshot?'+encodeURIComponent(s.last_check||'');const img=document.getElementById('shot');if(img.dataset.u!==u){img.src=u;img.dataset.u=u;document.getElementById('shotlink').href=u;}}
const tb=document.getElementById('recent');tb.replaceChildren(...s.recent.map(x=>{const tr=document.createElement('tr');
const a=document.createElement('td');a.textContent=t(x.time);const b=document.createElement('td');b.textContent=x.state;tr.append(a,b);return tr;}));
}catch(e){document.getElementById('detail').textContent='Panel lost contact with the watcher: '+e;}}
document.querySelectorAll('button[data-a]').forEach(b=>b.onclick=async()=>{await fetch('api/'+b.dataset.a,{method:'POST',headers:H});refresh();});
refresh();setInterval(refresh,5000);
</script></body></html>"""


def start_control_server(ctl: Control, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, PANEL_HTML.encode(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._send(200, json.dumps(ctl.snapshot()).encode(), "application/json")
            elif path == "/api/screenshot" and ctl.last_screenshot and Path(ctl.last_screenshot).exists():
                self._send(200, Path(ctl.last_screenshot).read_bytes(), "image/png")
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            action = self.path.split("?", 1)[0].removeprefix("/api/")
            # A custom header can't be sent cross-site without a CORS preflight we never
            # answer, so this blocks other websites from clicking buttons for you.
            if self.headers.get("X-Requested-With") != "cita":
                return self._send(403, b"forbidden", "text/plain")
            if action not in Control.ACTIONS:
                return self._send(404, b"unknown action", "text/plain")
            ctl.act(action)
            log.info("Control panel: %s", action)
            self._send(200, b'{"ok":true}', "application/json")

    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="control").start()
    log.info("Control panel on http://%s:%d", host, port)
    return srv


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
    if TXT_NONE in text.lower():
        return "none"
    if TXT_OFFICE in text or any(m in text for m in TXT_SLOTS):
        return "available"
    return "unknown"


def has_visible_captcha(page: Page) -> bool:
    # Image captcha (text box + picture). reCAPTCHA v3 is invisible and runs
    # on its own in a normal browser, so it isn't treated as a blocker here.
    return page.locator("img.img-thumbnail, #captcha").count() > 0 or \
        page.locator("iframe[src*='recaptcha'][title*='challenge']").count() > 0


def prefill_contact(cfg: Config, page: Page) -> None:
    """Fill phone/email on the contact page if it's showing and the fields are empty."""
    for selector, attr in CONTACT_FIELDS:
        value = getattr(cfg, attr)
        if not value:
            continue
        try:
            loc = page.locator(selector)
            if loc.count() and not loc.first.input_value(timeout=1_000):
                loc.first.fill(value, timeout=2_000)
                log.info("Pre-filled %s", selector)
        except Exception:  # noqa: BLE001 — page may be navigating; try again next tick
            pass


def wait_for_human(cfg: Config, page: Page, ctl: Control, reason: str, minutes: int,
                   alert: bool = True, prefill: bool = False) -> bool:
    """Wait until the user signals 'done' (ENTER / web button). False on timeout."""
    ctl.done.clear()
    deadline = now_madrid() + dt.timedelta(minutes=minutes)
    ctl.set("waiting", reason, wait_deadline=deadline.isoformat(timespec="seconds"))
    if alert:
        notify(cfg, "Cita watch needs you", reason)
    how = "press 'Done' in the control panel" if ctl.server_mode else "press ENTER here"
    print(f"\n>>> {reason}\n>>> When finished, {how} (waiting up to {minutes} min)...", flush=True)
    end = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < end:
            if ctl.done.wait(2):
                return True
            if prefill:
                prefill_contact(cfg, page)
        return False
    finally:
        ctl.set(ctl.status, ctl.detail, wait_deadline=None)


def screenshot(cfg: Config, page: Page, tag: str, ctl: Optional[Control] = None) -> Path:
    d = Path(cfg.screenshots_dir); d.mkdir(parents=True, exist_ok=True)
    p = d / f"{now_madrid():%Y%m%d-%H%M%S}-{tag}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        if ctl:
            ctl.last_screenshot = str(p)
    except Exception:  # noqa: BLE001
        pass
    return p


def pause(a: float = 1.0, b: float = 2.5) -> None:
    time.sleep(random.uniform(a, b))


# --------------------------------------------------------------------------- the flow
def check_once(cfg: Config, page: Page, ctl: Control) -> str:
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
        screenshot(cfg, page, "unexpected-landing", ctl)
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
        if cfg.country:
            page.select_option("#txtPaisNac", label=cfg.country)
        elif not wait_for_human(cfg, page, ctl, "This trámite asks for country of nationality — "
                                "pick it in the browser (and add `country:` to config)",
                                cfg.human_wait_minutes):
            return "needs_input"
    pause()

    if has_visible_captcha(page):
        screenshot(cfg, page, "captcha", ctl)
        if not wait_for_human(cfg, page, ctl, "CAPTCHA on the details page — solve it "
                              "(don't press Aceptar)", cfg.human_wait_minutes):
            return "captcha_timeout"

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
        screenshot(cfg, page, "unknown-result", ctl)
    return state


def release_session(page: Page) -> None:
    """Click 'Salir' if present so we don't leave a half-open session behind."""
    try:
        btn = page.locator("#btnSalir")
        if btn.count():
            btn.first.click(timeout=5_000)
    except Exception:  # noqa: BLE001
        pass


def within_active_hours(cfg: Config, now: Optional[dt.datetime] = None) -> bool:
    if not cfg.active_hours:
        return True
    now = now or now_madrid()
    start, end = cfg.active_hours
    return start <= now.hour < end


def current_interval(cfg: Config, now: Optional[dt.datetime] = None) -> int:
    """Base polling interval for this moment: a peak window's, else the default."""
    now = now or now_madrid()
    for w in cfg.peak_windows:
        start, end = w["hours"]
        if now.weekday() in w["days"] and start <= now.hour < end:
            return w["interval_seconds"]
    return cfg.interval_seconds


def block_backoff(consecutive_blocks: int) -> int:
    return min(BLOCK_BACKOFF * 2 ** max(consecutive_blocks - 1, 0), MAX_BACKOFF)


# --------------------------------------------------------------------------- history / stats
HISTORY_FIELDS = ("time_madrid", "weekday", "hour", "attempt", "state")


def append_history(cfg: Config, attempt: int, state: str) -> None:
    path = Path(cfg.history_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    now = now_madrid()
    try:
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(HISTORY_FIELDS)
            w.writerow([now.isoformat(timespec="seconds"), now.strftime("%a"), now.hour, attempt, state])
    except OSError as e:
        log.warning("Couldn't write history: %s", e)


def print_stats(cfg: Config) -> None:
    path = Path(cfg.history_file)
    if not path.exists():
        sys.exit(f"No history yet ({path})")
    checks, found = collections.Counter(), collections.Counter()
    states = collections.Counter()
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["weekday"], int(row["hour"]))
            checks[key] += 1
            states[row["state"]] += 1
            if row["state"] == "available":
                found[key] += 1
    print(f"{sum(checks.values())} checks: " + ", ".join(f"{k} {v}" for k, v in states.most_common()))
    if not found:
        print("No slots seen yet — keep it running a few days to learn the release times.")
        return
    print("\nWhen slots were seen (Madrid time):")
    print(f"{'day':<5}{'hour':<7}{'found':>6}{'checks':>8}{'rate':>7}")
    for key, n in sorted(found.items(), key=lambda kv: (-kv[1], kv[0])):
        day, hour = key
        print(f"{day:<5}{hour:02d}:00{n:>7}{checks[key]:>8}{n / checks[key]:>7.0%}")


# --------------------------------------------------------------------------- main loop
def run(cfg: Config, once: bool, ctl: Control, headless: bool = False) -> None:
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            cfg.profile_dir,
            headless=headless,              # visible on purpose: you finish the booking
            channel=cfg.browser_channel,
            locale="es-ES",
            timezone_id="Europe/Madrid",
            viewport={"width": 1200, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        blocks = broken = 0
        now = now_madrid()
        last_heartbeat = now.date() if cfg.heartbeat_hour is not None and now.hour >= cfg.heartbeat_hour else None
        try:
            while True:
                now = now_madrid()
                if (cfg.heartbeat_hour is not None and now.hour >= cfg.heartbeat_hour
                        and last_heartbeat != now.date()):
                    last_heartbeat = now.date()
                    snap = ctl.snapshot()
                    summary = ", ".join(f"{k} {v}" for k, v in snap["today"].items()) or "no checks yet today"
                    send_telegram(cfg, f"Cita watch is still running. {summary}. "
                                       f"Last result: {snap['last_state'] or '—'}.")

                if ctl.paused:
                    ctl.set("paused", "Paused from the control panel", next_check=None)
                    ctl.sleep(30)
                    continue

                if not within_active_hours(cfg, now):
                    ctl.set("outside_hours", f"Active {cfg.active_hours[0]:02d}:00–{cfg.active_hours[1]:02d}:00 "
                            "Madrid time", next_check=None)
                    log.info("Outside active hours; sleeping 10 min")
                    ctl.sleep(600)
                    continue

                ctl.attempt += 1
                ctl.set("checking", f"Attempt {ctl.attempt}", next_check=None)
                try:
                    state = check_once(cfg, page, ctl)
                except PWTimeout:
                    state = "timeout"
                    screenshot(cfg, page, "timeout", ctl)
                except Exception as e:  # noqa: BLE001
                    log.exception("Unexpected error: %s", e)
                    screenshot(cfg, page, "error", ctl)
                    state = "error"

                log.info("Attempt %d → %s", ctl.attempt, state)
                ctl.record(state)
                append_history(cfg, ctl.attempt, state)

                if state == "available":
                    blocks = broken = 0
                    shot = screenshot(cfg, page, "AVAILABLE", ctl)
                    where = "the control panel's 'Open browser'" if ctl.server_mode else "the browser window"
                    notify(cfg, "CITA DISPONIBLE!",
                           f"Slots are open — finish the booking in {where} now "
                           f"(you usually have ~5 min).", photo=shot)
                    done = wait_for_human(
                        cfg, page, ctl,
                        "Slots found. Book in the browser: office → Siguiente → contact "
                        "(pre-filled) → date → confirm/SMS. Save the justificante.",
                        cfg.available_wait_minutes, alert=False, prefill=True)
                    if done:
                        notify(cfg, "Cita watch", "Marked as handled. Watching is paused — "
                               "press Resume if you still need a cita.", loud=False)
                        if not ctl.server_mode:
                            return
                        ctl.act("pause")
                        continue
                    notify(cfg, "Cita watch: resuming",
                           f"No response within {cfg.available_wait_minutes} min; "
                           "back to watching.", loud=False)

                elif state == "blocked":
                    blocks += 1
                    wait = block_backoff(blocks)
                    notify(cfg, "Cita watch: rate-limited",
                           f"The site rejected the request ({blocks} in a row); "
                           f"backing off {wait // 60} min.", loud=False)
                    ctl.set("blocked", f"Rate-limited; retrying in {wait // 60} min",
                            next_check=(now_madrid() + dt.timedelta(seconds=wait)).isoformat(timespec="seconds"))
                    ctl.sleep(wait)
                    continue

                else:
                    blocks = 0
                    if state in ("unknown", "error", "timeout"):
                        broken += 1
                        if broken >= BROKEN_ALERT_AFTER and (broken - BROKEN_ALERT_AFTER) % BROKEN_ALERT_REPEAT == 0:
                            notify(cfg, "Cita watch may be broken",
                                   f"{broken} checks in a row ended as unknown/error/timeout. "
                                   "The site may have changed — look at the screenshots.",
                                   loud=False, photo=Path(ctl.last_screenshot) if ctl.last_screenshot else None)
                    else:
                        broken = 0

                release_session(page)
                if once:
                    return
                wait = current_interval(cfg) + random.randint(0, cfg.jitter_seconds)
                nxt = now_madrid() + dt.timedelta(seconds=wait)
                ctl.set("sleeping", f"Last result: {state}", next_check=nxt.isoformat(timespec="seconds"))
                log.info("Next check in %ds", wait)
                ctl.sleep(wait)
        finally:
            ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch Spain's cita previa (extranjería) for open slots.")
    ap.add_argument("-c", "--config", default="config.yaml", type=Path)
    ap.add_argument("--once", action="store_true", help="check a single time and exit")
    ap.add_argument("--test-alert", action="store_true", help="fire a test notification and exit")
    ap.add_argument("--stats", action="store_true", help="show when slots have been seen, from history")
    ap.add_argument("--server", action="store_true",
                    help="server mode: web control panel, no terminal prompts, 'Done' pauses instead of exiting")
    ap.add_argument("--panel", default=None, metavar="HOST:PORT",
                    help="serve the control panel (default 127.0.0.1:8080; 0.0.0.0:8080 with --server)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    if not args.config.exists():
        sys.exit(f"Config not found: {args.config} (copy config.example.yaml)")
    cfg = Config.load(args.config)

    if args.test_alert:
        notify(cfg, "Cita watch test", "If you can see/hear this, alerts work.")
        return
    if args.stats:
        print_stats(cfg)
        return

    ctl = Control(server_mode=args.server)
    panel = args.panel or ("0.0.0.0:8080" if args.server else None)
    if panel:
        host, _, port = panel.rpartition(":")
        start_control_server(ctl, host or "127.0.0.1", int(port))
    if not args.server:
        ctl.start_stdin_reader()
    run(cfg, args.once, ctl)


if __name__ == "__main__":
    main()
