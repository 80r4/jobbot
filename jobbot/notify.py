"""Notifications (Telegram / email) and optional cover-letter drafts."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import httpx

from .sources import Job

log = logging.getLogger("jobbot")


def _summary(jobs: list[Job], base_url: str) -> str:
    lines = [f"🔎 {len(jobs)} new matching job(s):", ""]
    for j in jobs[:25]:
        lines.append(f"• {j.title} — {j.company} ({j.location})\n  {j.url}")
    if len(jobs) > 25:
        lines.append(f"…and {len(jobs) - 25} more.")
    if base_url:
        lines += ["", f"Dashboard: {base_url}"]
    return "\n".join(lines)


def notify(jobs: list[Job], cfg: dict):
    if not jobs:
        return
    base_url = f"https://{os.getenv('DOMAIN')}" if os.getenv("DOMAIN") else ""
    text = _summary(jobs, base_url)
    n = cfg.get("notify", {})

    if n.get("telegram"):
        token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if token and chat:
            try:
                httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat, "text": text[:4000],
                                 "disable_web_page_preview": True}, timeout=20).raise_for_status()
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram failed: %s", e)
        else:
            log.warning("Telegram enabled but TELEGRAM_BOT_TOKEN/CHAT_ID missing")

    if n.get("email"):
        try:
            msg = EmailMessage()
            msg["Subject"] = f"JobBot: {len(jobs)} new job(s)"
            msg["From"] = os.environ["SMTP_USER"]
            msg["To"] = os.environ["EMAIL_TO"]
            msg.set_content(text)
            with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"),
                                  int(os.getenv("SMTP_PORT", "465"))) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("Email failed: %s", e)


def draft_cover_letter(job, profile_text: str) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    prompt = (
        "Write a concise, specific cover letter (max ~250 words) for this role, "
        "using only facts from my profile — do not invent experience. "
        "Match the language of the job posting (e.g. Spanish if the posting is in Spanish). "
        "Output only the letter.\n\n"
        f"## My profile\n{profile_text}\n\n"
        f"## Job\nTitle: {job['title']}\nCompany: {job['company']}\n"
        f"Location: {job['location']}\n\n{(job['description'] or '')[:6000]}"
    )
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5"),
              "max_tokens": 1200,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))
