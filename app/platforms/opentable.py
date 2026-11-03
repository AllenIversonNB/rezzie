"""OpenTable: reminder + deep link ONLY. The server never calls opentable.com.

Why (as of Nov 2026): every OpenTable surface — /r/…, /dapi/… GraphQL,
/booking/… — sits behind Akamai Bot Manager with a behavioural challenge.
Non-browser clients (even TLS-impersonating ones, and headless Chrome) get
403s or JS interstitials, and the availability query relies on rotating
persisted-query hashes. Even the best-maintained open-source integrations now
work only by relaying through the user's own logged-in Chrome tab. Hitting it
from a datacenter IP with your account's cookies is the fastest way to get
that account flagged, so we don't.

What we do instead:
  * Snipe → at T-60s and T-0 push a notification whose tap opens the exact
    restaurant/date/party page in the OpenTable app/site, so you book by hand
    in ~10 seconds.
  * Monitor → we recommend OpenTable's own built-in "Notify me" (sanctioned,
    zero risk), and remind you on a cadence you choose.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from .base import Venue

PLATFORM = "opentable"
BASE = "https://www.opentable.com"


def parse_venue(text: str, name: str = "") -> Venue:
    text = text.strip()
    rid = ""
    slug = ""
    m = re.search(r"opentable\.[a-z.]+/r/([A-Za-z0-9\-]+)", text)
    if m:
        slug = m.group(1)
    m = re.search(r"[?&]rid=(\d+)", text) or re.search(r"/restaurant/profile/(\d+)", text)
    if m:
        rid = m.group(1)
    if not slug and not rid:
        if text.isdigit():
            rid = text
        else:
            slug = re.sub(r"[^A-Za-z0-9\-]", "", text.lower().replace(" ", "-"))
    vid = rid or slug
    display = name or (slug.replace("-", " ").title() if slug else f"OpenTable #{rid}")
    return Venue(platform=PLATFORM, id=vid, name=display, slug=slug, url=venue_link(slug, rid))


def venue_link(slug: str = "", rid: str = "", day: str | None = None, time: str | None = None,
               party: int | None = None) -> str:
    path = f"/r/{slug}" if slug else f"/restaurant/profile/{rid}"
    q = []
    if party:
        q.append(f"covers={party}")
    if day:
        q.append("dateTime=" + quote(f"{day}T{time or '19:00'}"))
    return BASE + path + ("?" + "&".join(q) if q else "")
