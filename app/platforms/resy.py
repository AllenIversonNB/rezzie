"""Resy client (unofficial — the same endpoints resy.com's web app calls).

Request shapes verified against actively maintained open-source clients as of
Nov 2026 (chrischall/resy-mcp, daylamtayari/cierge). Changes vs the original
2025 Reservation Sniper plan:
  * API key is VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5 (the old plan's key was wrong).
  * Login: /3/auth/password still works; /4/auth/password is the newer twin.
    Login also sets a `production_refresh_token` cookie (≈90 days) that can mint
    new auth tokens via /3/auth/refresh — so we almost never need the password.
  * Auth tokens are JWTs (~45 days). We read `exp` and refresh early.
  * Venue search is POST /3/venuesearch/search with a form field `struct_data`.
  * /4/find and /3/details accept GET (web app) and POST JSON (newer); we try
    GET first and fall back.
  * Resy sits behind a WAF that answers bots with 403s or bare 500s.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import date as Date
from typing import Any

from .. import db, security
from ..config import settings
from ..safety import Governor, governor as default_governor
from .base import (
    AuthError,
    Blocked,
    Booking,
    BookingUnclear,
    NetworkError,
    PaymentRequired,
    PlatformError,
    RateLimited,
    Slot,
    SlotGone,
    Venue,
)
from .http import CurlTransport, Transport

log = logging.getLogger("rezzie.resy")

BASE = "https://api.resy.com"
PLATFORM = "resy"
KV_AUTH = "resy:auth"
KV_CREDS = "resy:creds"

_WAF_MARKERS = ("captcha", "access denied", "request unsuccessful", "incapsula", "<html", "attention required")


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


def venue_link(slug: str, city: str, day: str | None = None, seats: int | None = None) -> str:
    if not slug:
        return "https://resy.com/"
    url = f"https://resy.com/cities/{city or 'new-york-ny'}/venues/{slug}"
    q = []
    if day:
        q.append(f"date={day}")
    if seats:
        q.append(f"seats={seats}")
    return url + ("?" + "&".join(q) if q else "")


class ResyClient:
    def __init__(self, transport: Transport | None = None, gov: Governor | None = None):
        self.t = transport or CurlTransport()
        self.gov = gov or default_governor
        self._consecutive_5xx = 0

    # ------------------------------------------------------------ creds
    @staticmethod
    def credentials() -> tuple[str, str] | None:
        if settings.resy_email and settings.resy_password:
            return settings.resy_email, settings.resy_password
        blob = db.kv_get(KV_CREDS)
        if blob:
            raw = security.decrypt(blob)
            if raw:
                d = json.loads(raw)
                return d["email"], d["password"]
        return None

    @staticmethod
    def save_credentials(email: str, password: str) -> None:
        db.kv_set(KV_CREDS, security.encrypt(json.dumps({"email": email, "password": password})))
        db.kv_delete(KV_AUTH)

    @staticmethod
    def clear_credentials() -> None:
        db.kv_delete(KV_CREDS)
        db.kv_delete(KV_AUTH)

    @staticmethod
    def _auth() -> dict[str, Any]:
        blob = db.kv_get(KV_AUTH)
        if not blob:
            return {}
        raw = security.decrypt(blob)
        return json.loads(raw) if raw else {}

    @staticmethod
    def _save_auth(d: dict[str, Any]) -> None:
        db.kv_set(KV_AUTH, security.encrypt(json.dumps(d)))

    def auth_status(self) -> dict[str, Any]:
        a = self._auth()
        exp = a.get("exp")
        return {
            "has_credentials": self.credentials() is not None,
            "logged_in": bool(a.get("token")),
            "token_expires": exp,
            "has_refresh": bool(a.get("refresh")),
            "payment_method_id": a.get("payment_method_id"),
            "name": a.get("name"),
        }

    # ------------------------------------------------------------ http
    def _headers(self, auth: bool = True, form: bool = False, json_body: bool = False) -> dict[str, str]:
        h = {
            "Authorization": f'ResyAPI api_key="{settings.resy_api_key}"',
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "X-Origin": "https://resy.com",
            "Cache-Control": "no-cache",
        }
        if form:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if json_body:
            h["Content-Type"] = "application/json"
        if auth:
            tok = self._auth().get("token")
            if tok:
                h["X-Resy-Auth-Token"] = tok
                h["X-Resy-Universal-Auth"] = tok
        return h

    async def _call(self, method: str, path: str, *, urgent: bool = False, auth: bool = True,
                    params: dict | None = None, form: dict | None = None, json_body: Any = None,
                    cookies: dict | None = None, timeout: float = 10.0, book: bool = False):
        await self.gov.acquire(PLATFORM, urgent=urgent)
        try:
            r = await self.t.request(
                method, BASE + path,
                headers=self._headers(auth=auth, form=form is not None, json_body=json_body is not None),
                params=params, data=form, json_body=json_body, cookies=cookies, timeout=timeout,
            )
        except NetworkError:
            self.gov.network_error(PLATFORM, urgent=urgent)
            raise
        s = r.status
        body_l = r.text[:400].lower()
        if 200 <= s < 300:
            self._consecutive_5xx = 0
            self.gov.ok(PLATFORM)
            return r
        if s == 429:
            until = self.gov.rate_limited(PLATFORM)
            raise RateLimited(f"Resy rate limited; paused until {time.ctime(until)}", s, r.text)
        if s == 403 or (s >= 500 and any(m in body_l for m in _WAF_MARKERS)):
            until = self.gov.blocked(PLATFORM, f"HTTP {s}")
            raise Blocked(f"Resy blocked the request (HTTP {s}); paused until {time.ctime(until)}", s, r.text)
        if s in (401, 419):
            raise AuthError("Resy auth token rejected", s, r.text)
        if s == 402:
            raise PaymentRequired("Venue requires a payment method/deposit", s, r.text)
        if book and s in (404, 409, 410, 412):
            raise SlotGone(f"Slot no longer available (HTTP {s})", s, r.text)
        if s >= 500:
            self._consecutive_5xx += 1
            if self._consecutive_5xx >= 3:
                # Repeated bare 500s from Resy are how its WAF commonly says no.
                self._consecutive_5xx = 0
                until = self.gov.blocked(PLATFORM, f"repeated HTTP {s}")
                raise Blocked(f"Resy returned repeated {s}s (likely WAF); paused until {time.ctime(until)}", s, r.text)
        raise PlatformError(f"Resy HTTP {s} on {path}", s, r.text)

    # ------------------------------------------------------------ auth
    async def login(self) -> dict[str, Any]:
        creds = self.credentials()
        if not creds:
            raise AuthError("No Resy credentials configured (Settings → Resy)")
        logins = db.usage_get(PLATFORM, "login")
        if logins >= settings.max_password_logins_per_day:
            raise AuthError("Too many password logins today — waiting until tomorrow to protect the account")
        db.usage_incr(PLATFORM, "login")
        email, password = creds
        last_err: Exception | None = None
        for path in ("/3/auth/password", "/4/auth/password"):
            try:
                r = await self._call("POST", path, auth=False, form={"email": email, "password": password}, urgent=True)
                break
            except PlatformError as e:
                last_err = e
                if e.status in (404, 405):
                    continue
                if e.status in (400, 401, 419):
                    raise AuthError("Resy rejected the email/password", e.status, e.body) from e
                raise
        else:
            raise last_err or AuthError("Resy login failed")
        d = r.json()
        token = d.get("token")
        if not token:
            raise AuthError("Resy login returned no token")
        auth = {
            "token": token,
            "refresh": r.cookies.get("production_refresh_token") or self._auth().get("refresh"),
            "exp": _jwt_exp(token) or time.time() + 30 * 86400,
            "payment_method_id": d.get("payment_method_id"),
            "name": " ".join(x for x in (d.get("first_name"), d.get("last_name")) if x),
        }
        self._save_auth(auth)
        db.log_event(None, "Resy: logged in with password")
        return auth

    async def refresh(self) -> dict[str, Any]:
        a = self._auth()
        if not a.get("refresh"):
            return await self.login()
        try:
            r = await self._call("POST", "/3/auth/refresh", auth=False, urgent=True,
                                 cookies={"production_refresh_token": a["refresh"]})
        except (AuthError, PlatformError) as e:
            if isinstance(e, (Blocked, RateLimited)):
                raise
            return await self.login()
        token = r.json().get("token")
        if not token:
            return await self.login()
        a.update(token=token, exp=_jwt_exp(token) or time.time() + 30 * 86400)
        if r.cookies.get("production_refresh_token"):
            a["refresh"] = r.cookies["production_refresh_token"]
        self._save_auth(a)
        db.log_event(None, "Resy: refreshed auth token")
        return a

    async def ensure_auth(self, min_valid_seconds: float = 86400) -> dict[str, Any]:
        a = self._auth()
        if not a.get("token"):
            return await self.login()
        if (a.get("exp") or 0) - time.time() < min_valid_seconds:
            return await self.refresh()
        return a

    async def _authed(self, fn, *args, **kwargs):
        """Run fn; on an auth rejection refresh once and retry once."""
        await self.ensure_auth()
        try:
            return await fn(*args, **kwargs)
        except AuthError:
            await self.refresh()
            return await fn(*args, **kwargs)

    async def load_user(self) -> dict[str, Any]:
        async def _go():
            return (await self._call("GET", "/2/user")).json()

        u = await self._authed(_go)
        a = self._auth()
        pms = u.get("payment_methods") or []
        default = next((p for p in pms if p.get("is_default")), pms[0] if pms else None)
        a["payment_method_id"] = (default or {}).get("id") or u.get("payment_method_id") or a.get("payment_method_id")
        a["name"] = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
        self._save_auth(a)
        return {
            "name": a["name"],
            "email": u.get("em_address"),
            "payment_method_id": a["payment_method_id"],
            "payment_methods": [
                {"id": p.get("id"), "default": p.get("is_default"), "label": f"{p.get('type','card')} …{p.get('display','')}"}
                for p in pms
            ],
            "num_bookings": u.get("num_bookings"),
        }

    # ------------------------------------------------------------ venues
    @staticmethod
    def _venue_from(hit: dict[str, Any]) -> Venue:
        vid = hit.get("id")
        if isinstance(vid, dict):
            vid = vid.get("resy")
        loc = hit.get("location") or {}
        tz = loc.get("time_zone") or hit.get("time_zone") or ""
        if isinstance(tz, dict):
            tz = tz.get("name") or ""
        city = loc.get("url_slug") or hit.get("city_slug") or ""
        if not city and loc.get("code"):
            city = {"ny": "new-york-ny", "la": "los-angeles-ca", "sf": "san-francisco-ca"}.get(loc["code"], "")
        cuisine = hit.get("cuisine") or hit.get("type") or ""
        if isinstance(cuisine, list):
            cuisine = ", ".join(str(c) for c in cuisine[:2])
        slug = hit.get("url_slug") or ""
        return Venue(
            platform=PLATFORM,
            id=str(vid or ""),
            name=hit.get("name", ""),
            slug=slug,
            city=city,
            neighborhood=hit.get("neighborhood") or loc.get("neighborhood") or "",
            timezone=tz,
            url=venue_link(slug, city),
            cuisine=str(cuisine),
            price=hit.get("price_range_id") or hit.get("price_range"),
        )

    async def search(self, query: str, lat: float = 40.7128, lng: float = -74.0060) -> list[Venue]:
        struct = {
            "availability": False,
            "page": 1,
            "per_page": 10,
            "types": ["venue"],
            "geo": {"latitude": lat, "longitude": lng, "radius": 35420},
            "query": query,
        }
        r = await self._call("POST", "/3/venuesearch/search", form={"struct_data": json.dumps(struct)})
        hits = (r.json().get("search") or {}).get("hits") or []
        return [self._venue_from(h) for h in hits if h]

    async def venue_by_slug(self, slug: str, city: str) -> Venue:
        r = await self._call("GET", "/3/venue", params={"url_slug": slug, "location": city})
        v = self._venue_from(r.json())
        v.slug = v.slug or slug
        v.city = v.city or city
        v.url = venue_link(v.slug, v.city)
        return v

    async def lead_time_days(self, venue_id: str) -> int | None:
        try:
            r = await self._call("GET", "/2/config", params={"venue_id": venue_id})
            return int(r.json().get("lead_time_in_days")) if r.json().get("lead_time_in_days") is not None else None
        except PlatformError:
            return None

    async def calendar(self, venue_id: str, party_size: int, start: str, end: str) -> dict[str, str]:
        """{date: 'available' | 'sold-out' | 'closed' | ...} — one call covers a whole range."""
        r = await self._call("GET", "/4/venue/calendar", params={
            "venue_id": venue_id, "num_seats": party_size, "start_date": start, "end_date": end,
        })
        out = {}
        for d in r.json().get("scheduled") or []:
            out[d.get("date")] = ((d.get("inventory") or {}).get("reservation") or "").lower()
        return out

    # ------------------------------------------------------------ slots
    async def find(self, venue_id: str, day: str, party_size: int, *, urgent: bool = False,
                   slug: str = "", city: str = "") -> list[Slot]:
        params = {"lat": "0", "long": "0", "day": day, "party_size": str(party_size), "venue_id": str(venue_id)}

        async def _go():
            try:
                return await self._call("GET", "/4/find", params=params, urgent=urgent)
            except PlatformError as e:
                if e.status in (400, 405) and not isinstance(e, (Blocked, RateLimited)):
                    return await self._call("POST", "/4/find", urgent=urgent, json_body={
                        "lat": 0, "long": 0, "day": day, "party_size": party_size, "venue_id": int(venue_id)})
                raise

        r = await self._authed(_go)
        venues = (r.json().get("results") or {}).get("venues") or []
        slots: list[Slot] = []
        link = venue_link(slug, city, day, party_size)
        for v in venues[:1]:
            for s in v.get("slots") or []:
                cfg = s.get("config") or {}
                start = ((s.get("date") or {}).get("start") or "")  # "2026-11-20 19:00:00"
                if len(start) < 16:
                    continue
                slots.append(Slot(
                    platform=PLATFORM,
                    date=start[:10],
                    time=start[11:16],
                    seating=str(cfg.get("type") or ""),
                    token=str(cfg.get("token") or ""),
                    bookable=bool(cfg.get("token")),
                    link=link,
                    raw={"config_id": cfg.get("id"),
                         # Seconds before the slot when a cancellation fee starts, if Resy says.
                         "cancel_cutoff_secs": (s.get("payment") or {}).get("secs_cancel_cut_off")},
                ))
        return slots

    async def book(self, slot: Slot, party_size: int) -> Booking:
        """details → book. Urgent: bypasses spacing (we're mid-snipe), still budgeted."""
        if settings.dry_run:
            db.log_event(None, f"DRY_RUN: would book Resy {slot.date} {slot.time} ({slot.seating})")
            return Booking(confirmation="DRY-RUN", token="")

        async def _details():
            try:
                return await self._call("GET", "/3/details", urgent=True, book=True, params={
                    "config_id": slot.token, "day": slot.date, "party_size": str(party_size)})
            except PlatformError as e:
                if e.status in (400, 405) and not isinstance(e, (Blocked, RateLimited, SlotGone)):
                    return await self._call("POST", "/3/details", urgent=True, book=True, json_body={
                        "commit": 1, "config_id": slot.token, "day": slot.date, "party_size": party_size})
                raise

        d = (await self._authed(_details)).json()
        book_token = (d.get("book_token") or {}).get("value")
        if not book_token:
            raise SlotGone("Resy returned no book_token (slot likely gone)")

        a = self._auth()
        pm = a.get("payment_method_id")
        form = {"book_token": book_token, "source_id": "resy.com-venue-details"}
        if pm:
            form["struct_payment_method"] = json.dumps({"id": pm})

        async def _book():
            try:
                return await self._call("POST", "/3/book", urgent=True, book=True, form=form, timeout=20.0)
            except NetworkError as e:
                raise BookingUnclear(f"Resy didn't answer the booking request ({e}). It may have gone "
                                     "through: check the Resy app before booking again") from e

        r = (await self._authed(_book)).json()
        conf = r.get("reservation_id") or r.get("resy_token")
        if not conf:
            raise PlatformError(f"Unexpected /3/book response: {str(r)[:200]}")
        return Booking(confirmation=str(conf), token=r.get("resy_token", ""), raw=r)

    async def cancel(self, resy_token: str) -> None:
        async def _go():
            return await self._call("POST", "/3/cancel", form={"resy_token": resy_token}, urgent=True)

        await self._authed(_go)

    @staticmethod
    def default_window(day: str) -> tuple[str, str]:
        d = Date.fromisoformat(day)
        return d.isoformat(), d.isoformat()
