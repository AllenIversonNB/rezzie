"""FastAPI app: JSON API + the mobile web front end (served from /web)."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import db, security
from .config import settings
from .engine import engine
from .notify import push
from .platforms import opentable
from .platforms.base import PlatformError
from .platforms.resy import ResyClient
from .platforms.sevenrooms import parse_slug as sr_parse_slug
from .safety import BudgetExhausted, PlatformPaused, governor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rezzie")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
HHMM = re.compile(r"^\d{2}:\d{2}$")
YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    if os.getenv("ENGINE_DISABLED") != "1":
        engine.start()
    yield
    await engine.stop()


app = FastAPI(title="Rezzie", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp: Response = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def require_auth(request: Request) -> None:
    if not security.check_session(request.cookies.get(security.SESSION_COOKIE)):
        raise HTTPException(401, "Not logged in")


def _err(e: Exception) -> HTTPException:
    if isinstance(e, PlatformPaused):
        return HTTPException(503, f"Paused to protect your account: {e.reason}")
    if isinstance(e, BudgetExhausted):
        return HTTPException(503, str(e))
    if isinstance(e, PlatformError):
        return HTTPException(502, str(e))
    return HTTPException(500, str(e))


# ----------------------------------------------------------------- auth

class LoginIn(BaseModel):
    password: str


@app.get("/api/me")
async def me(request: Request):
    return {
        "authed": security.check_session(request.cookies.get(security.SESSION_COOKIE)),
        "configured": bool(settings.app_password),
    }


@app.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    ip = request.headers.get("fly-client-ip") or (request.client.host if request.client else "?")
    if not settings.app_password:
        raise HTTPException(503, "APP_PASSWORD is not set on the server")
    if not security.login_allowed(ip):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    if not security.check_password(body.password):
        security.record_failed_login(ip)
        await asyncio.sleep(random.uniform(0.3, 0.8))
        raise HTTPException(401, "Wrong password")
    response.set_cookie(
        security.SESSION_COOKIE, security.make_session(), max_age=security.SESSION_TTL,
        httponly=True, secure=settings.cookie_secure, samesite="strict", path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return {"ok": True}


# --------------------------------------------------------------- status

@app.get("/api/status", dependencies=[Depends(require_auth)])
async def status():
    platforms = {}
    for p in ("resy", "sevenrooms"):
        pu = governor.paused_until(p)
        platforms[p] = {
            "calls_today": db.usage_get(p),
            "budget": settings.daily_call_budget,
            "paused_until": pu if pu > time.time() else None,
            "pause_reason": governor.pause_reason(p) if pu > time.time() else None,
        }
    platforms["resy"]["auth"] = ResyClient().auth_status()
    active = db.list_requests(["monitoring"])
    return {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "app_timezone": settings.app_timezone,
        "dry_run": settings.dry_run,
        "ntfy_topic": settings.ntfy_topic or db.kv_get("ntfy_topic", ""),
        "ntfy_server": settings.ntfy_server,
        "ntfy_from_env": bool(settings.ntfy_topic),
        "resy_from_env": bool(settings.resy_email and settings.resy_password),
        "proxy": bool(settings.outbound_proxy),
        "active_monitors": len(active),
        "max_active_monitors": settings.max_active_monitors,
        "min_poll_seconds": settings.min_poll_seconds,
        "platforms": platforms,
    }


@app.post("/api/platform/{platform}/resume", dependencies=[Depends(require_auth)])
async def resume_platform(platform: Literal["resy", "sevenrooms"]):
    governor.resume(platform)
    db.log_event(None, f"{platform}: pause cleared manually")
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(require_auth)])
async def events(limit: int = 100):
    return db.list_events(None, min(limit, 500))


# -------------------------------------------------------------- settings

class ResyCredsIn(BaseModel):
    email: str
    password: str


@app.post("/api/settings/resy", dependencies=[Depends(require_auth)])
async def save_resy(body: ResyCredsIn):
    ResyClient.save_credentials(body.email.strip(), body.password)
    try:
        c = ResyClient()
        await c.login()
        return {"ok": True, "user": await c.load_user()}
    except Exception as e:
        raise _err(e)


@app.delete("/api/settings/resy", dependencies=[Depends(require_auth)])
async def delete_resy():
    ResyClient.clear_credentials()
    return {"ok": True}


@app.post("/api/settings/resy/test", dependencies=[Depends(require_auth)])
async def test_resy():
    try:
        return {"ok": True, "user": await ResyClient().load_user()}
    except Exception as e:
        raise _err(e)


class NtfyIn(BaseModel):
    topic: str


@app.post("/api/settings/ntfy", dependencies=[Depends(require_auth)])
async def save_ntfy(body: NtfyIn):
    topic = re.sub(r"[^A-Za-z0-9_\-]", "", body.topic)[:64]
    db.kv_set("ntfy_topic", topic)
    return {"ok": True, "topic": topic}


@app.post("/api/settings/ntfy/test", dependencies=[Depends(require_auth)])
async def test_ntfy():
    ok = await push("Rezzie test", "Notifications are working.", priority=3, tags=["white_check_mark"])
    if not ok:
        raise HTTPException(400, "Push failed — set an ntfy topic first")
    return {"ok": True}


# ---------------------------------------------------------------- venues

_RESY_URL = re.compile(r"resy\.com/cities/([^/?#]+)/venues/([^/?#]+)")


@app.get("/api/search", dependencies=[Depends(require_auth)])
async def search(q: str, platform: Literal["resy", "sevenrooms", "opentable"] = "resy",
                 lat: float = 40.7128, lng: float = -74.0060):
    q = q.strip()
    if not q:
        return []
    try:
        if platform == "resy":
            c = ResyClient()
            m = _RESY_URL.search(q)
            if m:
                return [(await c.venue_by_slug(m.group(2), m.group(1))).public()]
            return [v.public() for v in await c.search(q, lat, lng)]
        if platform == "sevenrooms":
            from .platforms.sevenrooms import SevenRoomsClient

            return [(await SevenRoomsClient().venue(q)).public()]
        return [opentable.parse_venue(q).public()]
    except Exception as e:
        raise _err(e)


@app.get("/api/resy/venue/{venue_id}/lead-time", dependencies=[Depends(require_auth)])
async def resy_lead_time(venue_id: str):
    try:
        return {"lead_time_days": await ResyClient().lead_time_days(venue_id)}
    except Exception as e:
        raise _err(e)


# -------------------------------------------------------------- requests

class RequestIn(BaseModel):
    platform: Literal["resy", "sevenrooms", "opentable"]
    mode: Literal["snipe", "monitor", "reminder"]
    venue_id: str
    venue_name: str
    venue_slug: str | None = None
    venue_city: str | None = None
    venue_tz: str | None = None
    venue_url: str | None = None
    party_size: int = Field(ge=1, le=20)
    dates: list[str] = Field(min_length=1, max_length=21)
    time_start: str
    time_end: str
    ideal_time: str | None = None
    seating_exclude: list[str] = []
    auto_book: bool = True
    release_local: str | None = None  # "YYYY-MM-DDTHH:MM" in the venue's timezone
    snipe_window_seconds: int | None = Field(default=None, ge=10, le=180)
    fallback_to_monitor: bool = True
    poll_interval_seconds: int | None = None
    monitor_until: str | None = None  # "YYYY-MM-DDTHH:MM" local
    notes: str | None = None

    @field_validator("dates")
    @classmethod
    def _dates(cls, v: list[str]) -> list[str]:
        for d in v:
            if not YMD.match(d):
                raise ValueError(f"bad date {d}")
            Date.fromisoformat(d)
        return sorted(dict.fromkeys(v))

    @field_validator("venue_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v and not re.match(r"^https://", v):
            raise ValueError("venue_url must be https")
        return v or None

    @field_validator("time_start", "time_end", "ideal_time")
    @classmethod
    def _times(cls, v: str | None) -> str | None:
        if v is not None and v != "" and not HHMM.match(v):
            raise ValueError("times must be HH:MM")
        return v or None


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.app_timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.app_timezone)


def _local_to_utc_iso(local: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(local)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _build(body: RequestIn) -> dict[str, Any]:
    v = body.model_dump()
    v.pop("release_local")
    mode = body.mode
    if body.platform == "opentable":
        mode = "reminder"
    tz = _tz(body.venue_tz)
    v["venue_tz"] = str(tz)
    v["mode"] = mode
    v["release_at"] = None
    if mode in ("snipe", "reminder"):
        if not body.release_local:
            raise HTTPException(400, "Set when reservations open (release time)")
        v["release_at"] = _local_to_utc_iso(body.release_local, tz)
        if datetime.fromisoformat(v["release_at"]) < datetime.now(timezone.utc) + timedelta(seconds=20):
            raise HTTPException(400, "Release time is in the past — use Monitor mode instead")
        v["status"] = "scheduled"
    else:
        v["status"] = "monitoring"
        v["next_check_at"] = (datetime.now(timezone.utc) + timedelta(seconds=random.uniform(5, 30))).isoformat(timespec="seconds")
    if v.get("monitor_until"):
        v["monitor_until"] = _local_to_utc_iso(v["monitor_until"], tz)
    if body.poll_interval_seconds is not None:
        v["poll_interval_seconds"] = max(settings.min_poll_seconds, body.poll_interval_seconds)
    if body.platform != "resy":
        v["auto_book"] = False
    return v


def _check_monitor_cap(extra: int = 1) -> None:
    n = len(db.list_requests(["monitoring"]))
    if n + extra > settings.max_active_monitors:
        raise HTTPException(400, f"Max {settings.max_active_monitors} active monitors (account safety). Cancel one first.")


@app.get("/api/requests", dependencies=[Depends(require_auth)])
async def list_requests(scope: Literal["active", "history", "all"] = "active"):
    active = ["scheduled", "attempting", "monitoring", "paused"]
    if scope == "active":
        return db.list_requests(active)
    if scope == "history":
        return [r for r in db.list_requests() if r["status"] not in active]
    return db.list_requests()


@app.post("/api/requests", dependencies=[Depends(require_auth)])
async def create_request(body: RequestIn):
    v = _build(body)
    if v["status"] == "monitoring":
        _check_monitor_cap()
    r = db.create_request(v)
    db.log_event(r["id"], f"Created {r['mode']} for {r['venue_name']} ({r['platform']})")
    return r


@app.get("/api/requests/{rid}", dependencies=[Depends(require_auth)])
async def get_request(rid: str):
    r = db.get_request(rid)
    if not r:
        raise HTTPException(404, "Not found")
    return {**r, "events": db.list_events(rid, 200)}


@app.put("/api/requests/{rid}", dependencies=[Depends(require_auth)])
async def update_request(rid: str, body: RequestIn):
    old = db.get_request(rid)
    if not old:
        raise HTTPException(404, "Not found")
    if old["status"] == "attempting":
        raise HTTPException(409, "Snipe in progress — try again in a minute")
    v = _build(body)
    if v["status"] == "monitoring" and old["status"] != "monitoring":
        _check_monitor_cap()
    engine.kick(rid)
    r = db.update_request(rid, **v, last_error=None)
    db.log_event(rid, "Edited")
    return r


@app.post("/api/requests/{rid}/cancel", dependencies=[Depends(require_auth)])
async def cancel_request(rid: str):
    r = db.get_request(rid)
    if not r:
        raise HTTPException(404, "Not found")
    engine.kick(rid)
    db.update_request(rid, status="cancelled")
    db.log_event(rid, "Cancelled by you")
    return db.get_request(rid)


@app.post("/api/requests/{rid}/resume", dependencies=[Depends(require_auth)])
async def resume_request(rid: str):
    r = db.get_request(rid)
    if not r:
        raise HTTPException(404, "Not found")
    if r["platform"] == "opentable":
        raise HTTPException(400, "OpenTable requests are reminders — edit the release time instead")
    _check_monitor_cap()
    nxt = (datetime.now(timezone.utc) + timedelta(seconds=random.uniform(5, 30))).isoformat(timespec="seconds")
    db.update_request(rid, status="monitoring", next_check_at=nxt, last_error=None)
    db.log_event(rid, "Resumed watching")
    return db.get_request(rid)


@app.post("/api/requests/{rid}/check", dependencies=[Depends(require_auth)])
async def check_request(rid: str):
    try:
        return {"slots": await engine.check_now(rid)}
    except Exception as e:
        raise _err(e)


@app.post("/api/requests/{rid}/cancel-booking", dependencies=[Depends(require_auth)])
async def cancel_booking(rid: str):
    r = db.get_request(rid)
    if not r or r["status"] != "booked" or r["platform"] != "resy" or not r.get("booking_token"):
        raise HTTPException(400, "Nothing to cancel on Resy for this request")
    try:
        await ResyClient().cancel(r["booking_token"])
    except Exception as e:
        raise _err(e)
    db.update_request(rid, status="cancelled", last_error=None)
    db.log_event(rid, "Reservation cancelled on Resy")
    return db.get_request(rid)


@app.delete("/api/requests/{rid}", dependencies=[Depends(require_auth)])
async def delete_request(rid: str):
    engine.kick(rid)
    db.delete_request(rid)
    return {"ok": True}


# ------------------------------------------------------------ front end

@app.get("/healthz")
async def healthz():
    return {"ok": True}


DEPLOY_GUARD_SECONDS = 15 * 60


@app.get("/api/busy")
async def busy():
    """Unauthenticated deploy guard: is a snipe running or due soon?

    The auto-deploy workflow waits while this says busy so a restart never
    lands in the middle of a snipe. Deliberately reveals nothing else.
    """
    now = datetime.now(timezone.utc)
    for r in db.list_requests(["scheduled", "attempting"]):
        if r["status"] == "attempting":
            return {"busy": True}
        rel = r.get("release_at")
        if rel:
            delta = (datetime.fromisoformat(rel) - now).total_seconds()
            window = r.get("snipe_window_seconds") or settings.snipe_window_seconds
            if -window - 30 <= delta <= DEPLOY_GUARD_SECONDS:
                return {"busy": True}
    return {"busy": False}


@app.get("/sw.js")
async def sw():
    return FileResponse(os.path.join(WEB_DIR, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.exception_handler(HTTPException)
async def http_exc(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
