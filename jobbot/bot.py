"""The search run: fetch all sources → filter → store → notify.

Run once from the command line:   python -m jobbot.bot
"""
from __future__ import annotations

import logging
import os

import httpx
import yaml

from . import db, sources
from .filters import score
from .notify import notify

log = logging.getLogger("jobbot")
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_search() -> dict:
    cfg = load_config()
    src = cfg.get("sources", {})
    fetched: list[sources.Job] = []
    errors: list[str] = []

    with httpx.Client(headers=sources.HEADERS, timeout=30, follow_redirects=True) as c:
        tasks = []
        az = src.get("adzuna") or {}
        if az.get("enabled"):
            aid, akey = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
            if aid and akey:
                tasks.append(("adzuna", lambda: sources.adzuna(az, aid, akey, c)))
            else:
                errors.append("adzuna: ADZUNA_APP_ID/ADZUNA_APP_KEY not set")
        for name, fn in (("greenhouse", sources.greenhouse),
                         ("lever", sources.lever),
                         ("ashby", sources.ashby)):
            for token in src.get(name) or []:
                tasks.append((f"{name}:{token}", lambda fn=fn, t=token: fn(t, c)))

        for label, task in tasks:
            try:
                got = task()
                log.info("%s → %d postings", label, len(got))
                fetched += got
            except Exception as e:  # noqa: BLE001
                errors.append(f"{label}: {e}")
                log.warning("%s failed: %s", label, e)

    new: list[sources.Job] = []
    matched = 0
    for job in fetched:
        s = score(job, cfg.get("filters", {}))
        if s is None:
            continue
        matched += 1
        if db.insert(job, s):
            new.append(job)

    notify(new, cfg)
    result = {"fetched": len(fetched), "matched": matched, "new": len(new), "errors": errors}
    log.info("run done: %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init()
    print(run_search())
