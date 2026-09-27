"""Web dashboard + scheduler.  Start with:  uvicorn jobbot.app:app --port 8000"""
from __future__ import annotations

import html
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db
from .bot import load_config, run_search
from .notify import draft_cover_letter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobbot")
scheduler = BackgroundScheduler(timezone="UTC")
LAST_RUN: dict = {}


def _scheduled():
    global LAST_RUN
    try:
        LAST_RUN = {**run_search(), "at": db.now()}
    except Exception as e:  # noqa: BLE001
        log.exception("search failed")
        LAST_RUN = {"at": db.now(), "errors": [str(e)]}


@asynccontextmanager
async def lifespan(_app):
    db.init()
    minutes = int(load_config().get("schedule_minutes", 180))
    scheduler.add_job(_scheduled, "interval", minutes=minutes, id="search",
                      next_run_time=datetime.now(timezone.utc), max_instances=1, coalesce=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    user, pw = os.getenv("DASHBOARD_USER", ""), os.getenv("DASHBOARD_PASS", "")
    if not pw:
        raise HTTPException(500, "Set DASHBOARD_PASS in .env")
    ok = (secrets.compare_digest(creds.username.encode(), user.encode())
          and secrets.compare_digest(creds.password.encode(), pw.encode()))
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Basic"})


e = lambda s: html.escape(str(s or ""))  # noqa: E731

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--mut:#6b6b66;--line:#e4e4df;--acc:#2f5bd3;--ok:#1f8a4c}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--fg:#ededea;--mut:#9a9a94;--line:#2e2e2b;--acc:#7d9bff;--ok:#5fcf8f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px}a{color:var(--acc);text-decoration:none}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:16px}
h1{font-size:20px;margin:0}.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}
.tab{padding:4px 10px;border:1px solid var(--line);border-radius:999px;color:var(--fg);font-size:13px}
.tab.on{background:var(--fg);color:var(--bg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:10px}
.t{font-weight:600}.m{color:var(--mut);font-size:13px}.row{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
button,.btn{font:inherit;font-size:13px;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.primary{background:var(--acc);border-color:var(--acc);color:#fff}
input[type=search]{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
textarea{width:100%;min-height:320px;font:14px/1.5 ui-monospace,monospace;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
.desc{white-space:pre-wrap;font-size:14px}.pill{font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid var(--line)}
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{CSS}</style></head><body><div class=wrap>{body}</div></body></html>""")


def status_buttons(j) -> str:
    out = []
    for s in ("shortlisted", "applied", "interview", "rejected", "dismissed"):
        if s != j["status"]:
            out.append(f'<form method=post action="/job/{j["id"]}/status/{s}">'
                       f'<button>{s.capitalize()}</button></form>')
    return "".join(out)


@app.get("/health")
def health():
    return {"ok": True, "last_run": LAST_RUN}


@app.get("/", dependencies=[Depends(auth)])
def index(status: str = "new", q: str = ""):
    st = None if status == "all" else status
    jobs = db.list_jobs(st, q or None)
    counts = db.counts()
    tabs = "".join(
        f'<a class="tab {"on" if s == status else ""}" href="/?status={s}">'
        f'{s} ({sum(counts.values()) if s == "all" else counts.get(s, 0)})</a>'
        for s in db.STATUSES + ["all"])
    last = LAST_RUN
    last_txt = (f'Last run {e(last.get("at"))}: {last.get("fetched", 0)} fetched, '
                f'{last.get("matched", 0)} matched, {last.get("new", 0)} new'
                if last else "No run yet")
    errs = "".join(f'<div class=m>⚠ {e(x)}</div>' for x in last.get("errors", []))
    cards = "".join(f"""<div class=card>
  <a class=t href="/job/{j['id']}">{e(j['title'])}</a>
  <div class=m>{e(j['company'])} · {e(j['location'])} · {e(j['source'])} · score {j['score']}
   · found {e((j['found_at'] or '')[:10])}</div>
  <div class=row><a class="btn primary" href="{e(j['url'])}" target=_blank rel=noopener>Open &amp; apply ↗</a>
  {status_buttons(j)}</div></div>""" for j in jobs) or "<p class=m>Nothing here.</p>"
    return page("JobBot", f"""
<header><h1>JobBot</h1>
<form method=get style="display:flex;gap:6px"><input type=hidden name=status value="{e(status)}">
<input type=search name=q value="{e(q)}" placeholder="Filter title / company"></form>
<form method=post action="/run"><button>Search now</button></form></header>
<div class=m>{last_txt}</div>{errs}
<div class=tabs>{tabs}</div>{cards}""")


@app.get("/job/{job_id}", dependencies=[Depends(auth)])
def job_detail(job_id: int):
    j = db.get(job_id)
    if not j:
        raise HTTPException(404)
    cl_cfg = load_config().get("cover_letter", {})
    cover = ""
    if cl_cfg.get("enabled"):
        cover = f"""<div class=card><div class=t>Cover letter draft</div>
<form method=post action="/job/{j['id']}/cover"><button>
{"Regenerate" if j['cover_letter'] else "Generate"} draft</button></form>
<textarea readonly onclick="this.select()">{e(j['cover_letter'])}</textarea>
<div class=m>Always read and edit before sending.</div></div>"""
    return page(j["title"], f"""
<p><a href="/">← back</a></p>
<div class=card><div class=t style="font-size:18px">{e(j['title'])}</div>
<div class=m>{e(j['company'])} · {e(j['location'])} · posted {e((j['posted_at'] or '?')[:10])}
 · <span class=pill>{e(j['status'])}</span></div>
<div class=row><a class="btn primary" href="{e(j['url'])}" target=_blank rel=noopener>Open &amp; apply ↗</a>
{status_buttons(j)}</div></div>
{cover}
<div class=card><div class="desc">{e(j['description'])}</div></div>""")


@app.post("/job/{job_id}/status/{new_status}", dependencies=[Depends(auth)])
def change_status(job_id: int, new_status: str, request: Request):
    try:
        db.set_status(job_id, new_status)
    except ValueError:
        raise HTTPException(400, "bad status")
    back = request.headers.get("referer") or "/"
    return RedirectResponse(back, status_code=303)


@app.post("/job/{job_id}/cover", dependencies=[Depends(auth)])
def make_cover(job_id: int):
    j = db.get(job_id)
    if not j:
        raise HTTPException(404)
    path = load_config().get("cover_letter", {}).get("profile_file", "profile.md")
    with open(path, encoding="utf-8") as f:
        profile = f.read()
    try:
        db.set_field(job_id, "cover_letter", draft_cover_letter(j, profile))
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"Draft failed: {ex}")
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/run", dependencies=[Depends(auth)])
def run_now():
    scheduler.modify_job("search", next_run_time=datetime.now(timezone.utc))
    return RedirectResponse("/", status_code=303)
