"""Job sources. Every function returns a list[Job].

Only official/public JSON APIs are used — no scraping of LinkedIn/Indeed,
which forbid bots and ban accounts that use them.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

HEADERS = {"User-Agent": "jobbot/1.0 (personal job search)"}
_TAG = re.compile(r"<[^>]+>")


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html.unescape(s or ""))).strip()


@dataclass
class Job:
    source: str
    ext_id: str
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    posted_at: str | None = None  # ISO 8601

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.ext_id}"


def adzuna(cfg: dict, app_id: str, app_key: str, client: httpx.Client) -> list[Job]:
    out: list[Job] = []
    country = cfg.get("country", "es")
    for q in cfg.get("queries", []):
        for page in range(1, int(cfg.get("pages", 1)) + 1):
            r = client.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": q,
                    "where": cfg.get("where", ""),
                    "results_per_page": 50,
                    "content-type": "application/json",
                },
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            for j in results:
                out.append(Job(
                    source="adzuna",
                    ext_id=str(j.get("id")),
                    company=(j.get("company") or {}).get("display_name", ""),
                    title=clean(j.get("title")),
                    location=(j.get("location") or {}).get("display_name", ""),
                    url=j.get("redirect_url", ""),
                    description=clean(j.get("description")),
                    posted_at=j.get("created"),
                ))
            if len(results) < 50:
                break
    return out


def greenhouse(token: str, client: httpx.Client) -> list[Job]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                   params={"content": "true"})
    r.raise_for_status()
    return [Job(
        source="greenhouse",
        ext_id=f"{token}-{j['id']}",
        company=token,
        title=j.get("title", ""),
        location=(j.get("location") or {}).get("name", ""),
        url=j.get("absolute_url", ""),
        description=clean(j.get("content")),
        posted_at=j.get("updated_at"),
    ) for j in r.json().get("jobs", [])]


def lever(token: str, client: httpx.Client) -> list[Job]:
    host = "api.lever.co"
    if token.startswith("eu:"):
        host, token = "api.eu.lever.co", token[3:]
    r = client.get(f"https://{host}/v0/postings/{token}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        created = j.get("createdAt")
        posted = (datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
                  if created else None)
        out.append(Job(
            source="lever",
            ext_id=j["id"],
            company=token,
            title=j.get("text", ""),
            location=(j.get("categories") or {}).get("location", "") or "",
            url=j.get("hostedUrl", ""),
            description=j.get("descriptionPlain", "") or clean(j.get("description")),
            posted_at=posted,
        ))
    return out


def ashby(token: str, client: httpx.Client) -> list[Job]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("location", "") or ""
        if j.get("isRemote"):
            loc = f"{loc} (Remote)".strip()
        out.append(Job(
            source="ashby",
            ext_id=j["id"],
            company=token,
            title=j.get("title", ""),
            location=loc,
            url=j.get("jobUrl", ""),
            description=j.get("descriptionPlain", "") or clean(j.get("descriptionHtml")),
            posted_at=j.get("publishedAt"),
        ))
    return out
