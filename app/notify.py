"""Push notifications via ntfy.sh (free iOS app: subscribe to your topic)."""
from __future__ import annotations

import logging

import httpx

from . import db
from .config import settings

log = logging.getLogger("rezzie.notify")


async def push(title: str, message: str, *, link: str | None = None, priority: int = 3,
               tags: list[str] | None = None, request_id: str | None = None) -> bool:
    db.log_event(request_id, f"NOTIFY: {title} — {message}", "notify")
    topic = settings.ntfy_topic or db.kv_get("ntfy_topic", "")
    if not topic:
        return False
    body: dict = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
    }
    actions = []
    if link:
        body["click"] = link
        actions.append({"action": "view", "label": "Open", "url": link})
    if settings.public_url and request_id:
        actions.append({"action": "view", "label": "Rezzie", "url": f"{settings.public_url.rstrip('/')}/#/r/{request_id}"})
    if actions:
        body["actions"] = actions
    headers = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(settings.ntfy_server.rstrip("/"), json=body, headers=headers)
            r.raise_for_status()
        return True
    except Exception as e:  # never let a notification failure break a booking
        log.warning("ntfy push failed: %s", e)
        db.log_event(request_id, f"ntfy push failed: {e}", "warn")
        return False
