"""SevenRooms (now owned by DoorDash): availability + guest auto-booking.

SevenRooms reservations are a guest checkout — name, email, phone; there is no
account to log into, so nothing of yours can be "flagged". DoorDash's in-app
Reservations tab sells this same SevenRooms inventory.

Request shapes (public reservation widget, verified against open-source
captures from Oct–Nov 2026):

  Venue    GET  /api-yoa/dining/venue_info?venue_url_key=<slug>&widget_type=res_widget
  Search   GET  /api-yoa/availability/ng/widget/range
                ?venue=<slug>&party_size=N&start_date=YYYY-MM-DD&num_days=1|3
                &time_slot=HH:MM&halo_size_interval=100&channel=SEVENROOMS_WIDGET&exclude_pdr=true
           data.availability[YYYY-MM-DD] -> [shift{times:[...]}]
           Only entries with type == "book" AND an access_persistent_id are real
           tables; type == "request" is a request the venue may decline.
           require_credit_card marks venues that need a card (we never automate those).
  Hold     POST /api-yoa/dining/hold/add   JSON {venue, date, time, party_size,
                access_persistent_id, shift_persistent_id, channel}
           -> data.reservation_hold_id, data.hold_duration_sec (~5 min)
  Book     POST /booking/dining/widget/<slug>/book   multipart form with the hold
                ids, guest name/email/phone, date MM-DD-YYYY, all opt-ins "false"
  Link     /explore/<slug>/reservations/create/search/?date&party_size&start_time
"""
from __future__ import annotations

import json
import re
from datetime import date as Date
from typing import Any

from .. import db, security
from ..config import settings
from ..safety import Governor, governor as default_governor
from .base import Blocked, Booking, PaymentRequired, PlatformError, RateLimited, Slot, SlotGone, Venue
from .http import CurlTransport, Transport

PLATFORM = "sevenrooms"
BASE = "https://www.sevenrooms.com"
CHANNEL = "SEVENROOMS_WIDGET"
KV_GUEST = "guest:details"


class NeedsHuman(PaymentRequired):
    """Captcha / extra verification / unexpected checkout step: finish by hand."""


def venue_link(slug: str, day: str | None = None, party: int | None = None, time: str | None = None) -> str:
    url = f"{BASE}/explore/{slug}/reservations/create/search/"
    q = []
    if day:
        q.append(f"date={day}")
    if party:
        q.append(f"party_size={party}")
    if time:
        q.append(f"start_time={time}")
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


def to_hhmm(value: str) -> str | None:
    """'19:00', '19:00:00', '7:00 PM' -> '19:00'."""
    value = (value or "").strip()
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)(?::\d\d)?$", value)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.match(r"^(0?[1-9]|1[0-2]):([0-5]\d)\s*([AaPp][Mm])$", value)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        return f"{h:02d}:{m.group(2)}"
    return None


# ------------------------------------------------------------ guest details

def guest_details() -> dict[str, str] | None:
    blob = db.kv_get(KV_GUEST)
    if not blob:
        return None
    raw = security.decrypt(blob)
    return json.loads(raw) if raw else None


def save_guest_details(first: str, last: str, email: str, phone: str, dial_code: str = "1",
                       country_code: str = "us") -> dict[str, str]:
    digits = re.sub(r"\D", "", phone)
    dial = re.sub(r"\D", "", dial_code) or "1"
    if digits.startswith(dial) and len(digits) > 10:
        digits = digits[len(dial):]
    d = {"first_name": first.strip(), "last_name": last.strip(), "email": email.strip(),
         "phone": digits, "dial_code": dial, "country_code": (country_code or "us").lower()[:2]}
    if not (d["first_name"] and d["last_name"] and "@" in d["email"] and len(digits) >= 7):
        raise ValueError("First name, last name, a valid email and phone number are required")
    db.kv_set(KV_GUEST, security.encrypt(json.dumps(d)))
    return d


class SevenRoomsClient:
    def __init__(self, transport: Transport | None = None, gov: Governor | None = None):
        self.t = transport or CurlTransport()
        self.gov = gov or default_governor

    async def _req(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
                   multipart: dict | None = None, urgent: bool = False, referer_slug: str = "",
                   booking: bool = False):
        await self.gov.acquire(PLATFORM, urgent=urgent)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": venue_link(referer_slug) if referer_slug else BASE + "/",
        }
        if method == "POST":
            headers["Origin"] = BASE
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        r = await self.t.request(method, BASE + path, params=params, json_body=json_body,
                                 multipart=multipart, headers=headers, timeout=20.0 if booking else 10.0)
        low = r.text[:500].lower()
        if r.status == 429:
            self.gov.rate_limited(PLATFORM)
            raise RateLimited("SevenRooms rate limited", r.status, r.text)
        if r.status == 403 or (r.status >= 500 and "<html" in low):
            self.gov.blocked(PLATFORM, f"HTTP {r.status}")
            raise Blocked(f"SevenRooms blocked the request (HTTP {r.status})", r.status, r.text)
        if booking and ("captcha" in low or "verify you are human" in low):
            raise NeedsHuman("SevenRooms asked for human verification", r.status, r.text)
        if r.status >= 400:
            raise PlatformError(f"SevenRooms HTTP {r.status} on {path}", r.status, r.text)
        self.gov.ok(PLATFORM)
        return r

    # ------------------------------------------------------------- venue
    async def venue(self, slug_or_url: str) -> Venue:
        slug = parse_slug(slug_or_url).lower()
        if not slug:
            raise PlatformError("Couldn't read a SevenRooms venue from that text")
        try:
            r = await self._req("GET", "/api-yoa/dining/venue_info",
                                params={"venue_url_key": slug, "widget_type": "res_widget"}, referer_slug=slug)
        except PlatformError as e:
            if e.status == 404:
                raise PlatformError("SevenRooms doesn't know that venue — check the link") from e
            raise
        data = (r.json().get("data") or {})
        info = data.get("venue_info") or {}
        if not info.get("name"):
            raise PlatformError("SevenRooms didn't recognise that venue")
        key = info.get("url_key") or slug
        return Venue(
            platform=PLATFORM, id=key, name=info["name"], slug=key,
            city=info.get("city") or "", timezone=info.get("timezone") or "",
            url=venue_link(key),
            lead_time_days=((data.get("widget_settings") or {}).get("maxDaysOut") or None),
        )

    # ------------------------------------------------------------- search
    async def find(self, slug: str, day: str, party_size: int, around: str = "19:00", *,
                   urgent: bool = False) -> list[Slot]:
        r = await self._req("GET", "/api-yoa/availability/ng/widget/range", params={
            "venue": slug, "party_size": party_size, "start_date": day, "num_days": 1,
            "time_slot": around, "halo_size_interval": 100, "channel": CHANNEL, "exclude_pdr": "true",
        }, urgent=urgent, referer_slug=slug)
        body = r.json()
        shifts = ((body.get("data") or {}).get("availability") or {}).get(day) or []
        out: list[Slot] = []
        seen = set()
        for shift in shifts:
            if not isinstance(shift, dict) or shift.get("is_closed") or shift.get("is_forced_empty_availability"):
                continue
            for t in shift.get("times") or []:
                if not isinstance(t, dict):
                    continue
                if t.get("type") != "book" or not t.get("access_persistent_id"):
                    continue  # "request" times are not tables
                hhmm = to_hhmm(str(t.get("time") or "")) or to_hhmm(str(t.get("time_iso") or "")[11:16])
                if not hhmm or hhmm in seen:
                    continue
                seen.add(hhmm)
                needs_card = bool(t.get("require_credit_card"))
                out.append(Slot(
                    platform=PLATFORM,
                    date=day,
                    time=hhmm,
                    seating=t.get("public_time_slot_description") or t.get("public_description_title")
                    or shift.get("shift_category") or "",
                    token=t["access_persistent_id"],
                    bookable=not needs_card,
                    link=venue_link(slug, day, party_size, hhmm),
                    raw={
                        "slug": slug,
                        "venue": t.get("venue_id") or shift.get("venue_id") or slug,
                        "time_label": t.get("time") or hhmm,
                        "access_persistent_id": t["access_persistent_id"],
                        "shift_persistent_id": t.get("shift_persistent_id") or shift.get("shift_persistent_id") or "",
                        "needs_card": needs_card,
                    },
                ))
        return out

    # -------------------------------------------------------------- book
    async def book(self, slot: Slot, party_size: int) -> Booking:
        """hold → book with your guest details. Never used for card-required slots."""
        if slot.raw.get("needs_card"):
            raise PaymentRequired("This SevenRooms slot needs a credit card")
        guest = guest_details()
        if not guest:
            raise NeedsHuman("Add your booking details in Settings to auto-book SevenRooms")
        if not slot.raw.get("shift_persistent_id"):
            raise NeedsHuman("SevenRooms didn't return the ids needed to book this slot")
        if settings.dry_run:
            db.log_event(None, f"DRY_RUN: would book SevenRooms {slot.date} {slot.time} ({slot.seating})")
            return Booking(confirmation="DRY-RUN")

        slug = slot.raw["slug"]
        venue = slot.raw["venue"]
        hold_payload = {
            "venue": venue,
            "date": slot.date,
            "time": slot.raw["time_label"],
            "party_size": party_size,
            "access_persistent_id": slot.raw["access_persistent_id"],
            "shift_persistent_id": slot.raw["shift_persistent_id"],
            "channel": CHANNEL,
        }
        try:
            h = (await self._req("POST", "/api-yoa/dining/hold/add", json_body=hold_payload, urgent=True,
                                 referer_slug=slug, booking=True)).json()
        except PlatformError as e:
            if isinstance(e, (Blocked, RateLimited, NeedsHuman)):
                raise
            raise SlotGone(f"Couldn't hold {slot.time} (HTTP {e.status})", e.status, e.body) from e
        hold_id = ((h.get("data") or {}).get("reservation_hold_id"))
        if not hold_id:
            raise SlotGone(f"SevenRooms wouldn't hold {slot.time}: {str(h.get('msg') or h)[:150]}")

        y, m, d = slot.date.split("-")
        form = {
            "venue_id": venue,
            "widget_primary_venue_id": venue,
            "shift_persistent_id": slot.raw["shift_persistent_id"],
            "access_persistent_id": slot.raw["access_persistent_id"],
            "reservation_hold_id": hold_id,
            "date": f"{m}-{d}-{y}",
            "time": slot.raw["time_label"],
            "party_size": str(party_size),
            "first_name": guest["first_name"],
            "last_name": guest["last_name"],
            "email": guest["email"],
            "phone_number": guest["phone"],
            "dial_code": guest["dial_code"],
            "country_code": guest["country_code"],
            "channel": CHANNEL,
            "new_widget_used": "true",
            "preferred_language_code": "en",
            "client_tags": "[]",
            "reservation_tags": "[]",
            "venue_group_marketing_opt_in": "false",
            "venue_specific_marketing_opt_in": "false",
            "venue_sms_marketing_opt_in": "false",
            "tailored_communication_opt_in": "false",
            "reservation_sms_opt_in": "false",
        }
        try:
            r = await self._req("POST", f"/booking/dining/widget/{slug}/book", multipart=form, urgent=True,
                                referer_slug=slug, booking=True)
        except PlatformError as e:
            if isinstance(e, (Blocked, RateLimited, NeedsHuman)):
                raise
            text = (e.body or "").lower()
            if any(k in text for k in ("credit card", "payment", "deposit", "stripe")):
                raise PaymentRequired("SevenRooms wants a card for this booking", e.status, e.body) from e
            raise SlotGone(f"SevenRooms booking failed (HTTP {e.status})", e.status, e.body) from e
        body = r.json() if r.text.strip().startswith("{") else {}
        if body.get("status") not in (None, 200, "200", "success") or body.get("errors"):
            msg = str(body.get("msg") or body.get("errors") or body)[:200]
            if any(k in msg.lower() for k in ("credit", "payment", "deposit", "card")):
                raise PaymentRequired(f"SevenRooms wants a card: {msg}")
            raise SlotGone(f"SevenRooms didn't confirm: {msg}")
        data = body.get("data") or {}
        conf = ""
        for key in ("reference_code", "reservation_reference_code", "confirmation_number", "conf_num",
                    "reservation_id", "actual_id", "id"):
            v = data.get(key) or (data.get("actual") or {}).get(key) if isinstance(data, dict) else None
            if v:
                conf = str(v)
                break
        return Booking(confirmation=conf or "check your email", raw=body if isinstance(body, dict) else {})
