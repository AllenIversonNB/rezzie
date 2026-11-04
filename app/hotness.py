"""When are cancellations likely? Score each monitor's "hotness" over time.

The daily call budget is fixed; hotness decides *where in the day* it gets
spent. A monitor checks at a rate proportional to its hotness, so checks bunch
up in the windows where tables tend to open and thin out in dead hours.

Heuristics (anecdotal but consistent across reservation hunters):
  * Fee deadline: many restaurants charge for cancelling inside a cutoff
    (usually 24h, sometimes 48/72h). People cancel just before it  -> 3.0x
    in the ~2.5h before the deadline. Uses the restaurant's real cutoff when
    Resy has reported it, else 24h (plus a smaller 48h bump).
  * Day of, 9am-noon: plans fall apart over morning coffee        -> 2.5x
  * Day of, 3-6pm: last-minute bailouts, unconfirmed tables freed -> 2.0x
  * Within 48h generally                                          -> 1.3x
  * More than 7 days out: little moves until release/close-in     -> 0.6x
  * Overnight quiet hours (01-07 by default)                      -> x0.2
Rezzie also logs every newly appearing table (see engine) so these can be
tuned from real data later.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
from .config import settings
from .safety import in_quiet_hours

FEE_WINDOW_HOURS = 2.5
STEP_SECONDS = 600  # integration step for budget planning


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.app_timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.app_timezone)


def cutoff_hours(req: dict[str, Any]) -> float | None:
    v = db.kv_get(f"cutoff:{req['platform']}:{req['venue_id']}")
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def remember_cutoff(req: dict[str, Any], secs: Any) -> None:
    try:
        hours = int(secs) / 3600
    except (TypeError, ValueError):
        return
    if 1 <= hours <= 24 * 14 and cutoff_hours(req) != hours:
        db.kv_set(f"cutoff:{req['platform']}:{req['venue_id']}", hours)
        db.log_event(req["id"], f"Learned this restaurant's cancellation cutoff: {hours:g}h before")


def slot_times(req: dict[str, Any], now: datetime) -> list[datetime]:
    """The (up to 3 nearest) future reservation times this monitor cares about, as aware datetimes."""
    tz = _tz(req.get("venue_tz"))
    hhmm = req.get("ideal_time") or req["time_start"]
    h, m = (int(x) for x in hhmm.split(":")[:2])
    out = []
    for d in req.get("dates") or []:
        y, mo, da = (int(x) for x in d.split("-"))
        t = datetime(y, mo, da, h, m, tzinfo=tz)
        if t > now - timedelta(hours=1):
            out.append(t)
    return sorted(out)[:3]


def hotness(req: dict[str, Any], now: datetime | None = None, cutoff: float | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    times = slot_times(req, now)
    if not times:
        return 1.0
    c = cutoff if cutoff is not None else (cutoff_hours(req) or 24.0)
    best = 0.0
    for t in times:
        hrs = (t - now).total_seconds() / 3600
        w = 1.0
        if hrs > 168:
            w = 0.6
        elif hrs <= 48:
            w = 1.3
        if c <= hrs <= c + FEE_WINDOW_HOURS:
            w = max(w, 3.0)
        if c != 48 and 48 <= hrs <= 48 + FEE_WINDOW_HOURS:
            w = max(w, 2.0)
        local = now.astimezone(t.tzinfo)
        if local.date() == t.date() and hrs >= 0:
            if 9 <= local.hour < 12:
                w = max(w, 2.5)
            elif 15 <= local.hour < 18:
                w = max(w, 2.0)
        best = max(best, w)
    if in_quiet_hours(now):
        best *= 0.2
    return best


def label(w: float) -> str:
    if w >= 2.5:
        return "Hot window — checking fastest"
    if w >= 1.9:
        return "Warm window — checking faster"
    if w >= 1.2:
        return "Close to the date — checking a bit faster"
    if w >= 0.9:
        return "Normal"
    if w >= 0.4:
        return "Date is far off — checking slower"
    return "Overnight — checking rarely"


def weight_until(req: dict[str, Any], start: datetime, end: datetime) -> float:
    """∫ hotness dt over [start, end), in seconds (10-minute steps)."""
    total = 0.0
    c = cutoff_hours(req) or 24.0
    t = start
    while t < end:
        step = min(STEP_SECONDS, (end - t).total_seconds())
        total += hotness(req, t + timedelta(seconds=step / 2), cutoff=c) * step
        t += timedelta(seconds=step)
    return total
