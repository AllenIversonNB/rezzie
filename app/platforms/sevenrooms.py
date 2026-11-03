"""SevenRooms: watch availability via the public reservation-widget API.

No account or login is involved, so there is nothing tied to *you* that can be
flagged; we still pace calls through the Governor. Booking itself is left to a
human tap on the deep link: SevenRooms checkout often needs a card/deposit and
venue-specific questions, and automated checkout is the riskiest part.

Endpoint (public widget, unauthenticated):
  GET https://www.sevenrooms.com/api-yoa/availability/widget/range
      ?venue=<slug>&time_slot=HH:MM&party_size=N&halo_size_interval=16
      &start_date=MM-DD-YYYY&num_days=N&channel=SEVENROOMS_WIDGET
Response: data.availability[YYYY-MM-DD] -> [ {shift, times:[{time_iso, access_persistent_id, ...}]} ]
A time with a non-null access_persistent_id is bookable.
"""
from __future__ import annotations

import re
from datetime import date as Date
from typing import Any

from ..safety import Governor, governor as default_governor
from .base import Blocked, PlatformError, RateLimited, Slot, Venue
from .http import CurlTransport, Transport

PLATFORM = "sevenrooms"
BASE = "https://www.sevenrooms.com"


def venue_link(slug: str, day: str | None = None, party: int | None = None) -> str:
    url = f"{BASE}/reservations/{slug}"
    q = []
    if day:
        q.append(f"date={day}")
    if party:
        q.append(f"party_size={party}")
    return url + ("?" + "&".join(q) if q else "")


def parse_slug(text: str) -> str:
    """Accept a slug or any sevenrooms URL (…/reservations/<slug>, …/explore/<slug>/…)."""
    text = text.strip()
    m = re.search(r"sevenrooms\.com/(?:reservations|explore)/([A-Za-z0-9_\-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]venues?=([A-Za-z0-9_\-]+)", text)
    if m:
        return m.group(1)
    return re.sub(r"[^A-Za-z0-9_\-]", "", text)


class SevenRoomsClient:
    def __init__(self, transport: Transport | None = None, gov: Governor | None = None):
        self.t = transport or CurlTransport()
        self.gov = gov or default_governor

    async def _get(self, path: str, params: dict[str, Any], urgent: bool = False):
        await self.gov.acquire(PLATFORM, urgent=urgent)
        r = await self.t.request("GET", BASE + path, params=params, headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE}/reservations/{params.get('venue', '')}",
        })
        if r.status == 429:
            self.gov.rate_limited(PLATFORM)
            raise RateLimited("SevenRooms rate limited", r.status, r.text)
        if r.status == 403 or (r.status >= 500 and "<html" in r.text[:200].lower()):
            self.gov.blocked(PLATFORM, f"HTTP {r.status}")
            raise Blocked(f"SevenRooms blocked the request (HTTP {r.status})", r.status, r.text)
        if r.status >= 400:
            raise PlatformError(f"SevenRooms HTTP {r.status}", r.status, r.text)
        self.gov.ok(PLATFORM)
        return r.json()

    async def venue(self, slug_or_url: str) -> Venue:
        slug = parse_slug(slug_or_url)
        if not slug:
            raise PlatformError("Couldn't read a SevenRooms venue from that text")
        # One cheap availability probe validates the slug and gets the name.
        today = Date.today()
        data = await self._get("/api-yoa/availability/widget/range", {
            "venue": slug, "time_slot": "19:00", "party_size": 2, "halo_size_interval": 16,
            "start_date": today.strftime("%m-%d-%Y"), "num_days": 1, "channel": "SEVENROOMS_WIDGET",
        })
        if not isinstance(data, dict) or "data" not in data:
            raise PlatformError("SevenRooms didn't recognise that venue")
        name = ((data.get("data") or {}).get("venue_name") or slug.replace("-", " ").replace("_", " ").title())
        return Venue(platform=PLATFORM, id=slug, name=name, slug=slug, url=venue_link(slug))

    async def find(self, slug: str, day: str, party_size: int, around: str = "19:00", *,
                   urgent: bool = False) -> list[Slot]:
        d = Date.fromisoformat(day)
        data = await self._get("/api-yoa/availability/widget/range", {
            "venue": slug, "time_slot": around, "party_size": party_size, "halo_size_interval": 64,
            "start_date": d.strftime("%m-%d-%Y"), "num_days": 1, "channel": "SEVENROOMS_WIDGET",
        }, urgent=urgent)
        shifts = ((data.get("data") or {}).get("availability") or {}).get(day) or []
        out: list[Slot] = []
        seen = set()
        for shift in shifts:
            for t in shift.get("times") or []:
                iso = t.get("time_iso") or ""  # "2026-11-20 19:00:00"
                if len(iso) < 16 or not t.get("access_persistent_id"):
                    continue
                hhmm = iso[11:16]
                if hhmm in seen:
                    continue
                seen.add(hhmm)
                out.append(Slot(
                    platform=PLATFORM,
                    date=iso[:10],
                    time=hhmm,
                    seating=t.get("public_time_slot_description") or shift.get("shift_category") or "",
                    bookable=False,  # human finishes checkout
                    link=venue_link(slug, day, party_size),
                ))
        return out
