"""The sniper engine: timed-release snipes, cancellation monitors, reminders.

Runs inside the web process as asyncio tasks (one small always-on machine).

Request lifecycle
  snipe     scheduled ──(T-0)──► attempting ──► booked | found | monitoring (fallback) | failed
  monitor   monitoring ──(slot appears)──► booked | found      (or expired)
  reminder  scheduled ──(T-60s, T-0 pushes)──► notified        (OpenTable)
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date as Date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import settings
from .matching import matching_slots
from .notify import push
from .platforms import opentable
from .platforms.base import (
    AuthError,
    Blocked,
    PaymentRequired,
    PlatformError,
    RateLimited,
    Slot,
    SlotGone,
)
from .platforms.resy import ResyClient
from .platforms.sevenrooms import SevenRoomsClient, guest_details
from . import hotness as hotness_mod
from .safety import (
    BudgetExhausted,
    PlatformPaused,
    effective_poll_interval,
    governor,
    jitter,
    monitor_budget,
    seconds_until_local_midnight,
)

log = logging.getLogger("rezzie.engine")

ACTIVE = ("scheduled", "attempting", "monitoring")
PLATFORM_NAMES = {"resy": "Resy", "sevenrooms": "SevenRooms", "opentable": "OpenTable"}
LOOP_SECONDS = 5
PREAUTH_SECONDS = 300      # verify login + payment method 5 min before a snipe
MAX_SLOTS_PER_ATTEMPT = 3  # try at most this many matching slots per find


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def venue_today(req: dict[str, Any]) -> Date:
    tz = ZoneInfo(req.get("venue_tz") or settings.app_timezone)
    return datetime.now(tz).date()


class Engine:
    def __init__(self, resy: ResyClient | None = None, sevenrooms: SevenRoomsClient | None = None):
        self.resy = resy or ResyClient()
        self.sr = sevenrooms or SevenRoomsClient()
        self._timed: dict[str, asyncio.Task] = {}
        self._book_lock = asyncio.Lock()  # one booking in flight, ever
        self._runner: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._loop(), name="engine-loop")

    async def stop(self) -> None:
        self._stopping = True
        for t in list(self._timed.values()):
            t.cancel()
        if self._runner:
            self._runner.cancel()

    def kick(self, rid: str) -> None:
        """Called after create/edit so a timed task is (re)armed promptly."""
        t = self._timed.pop(rid, None)
        if t:
            t.cancel()

    async def _loop(self) -> None:
        # Anything left "attempting" by a crash/restart goes back to its natural state.
        for r in db.list_requests(["attempting"]):
            rel = parse_iso(r.get("release_at"))
            if r["mode"] == "monitor" or (rel and rel < utcnow() - timedelta(seconds=r.get("snipe_window_seconds") or 60)):
                db.update_request(r["id"], status="monitoring" if r.get("fallback_to_monitor") or r["mode"] == "monitor" else "failed")
            else:
                db.update_request(r["id"], status="scheduled")
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive no matter what
                log.exception("engine tick failed")
                db.log_event(None, f"engine error: {e}", "error")
            await asyncio.sleep(LOOP_SECONDS)

    async def tick(self) -> None:
        now = utcnow()
        for req in db.list_requests(list(ACTIVE)):
            rid = req["id"]
            # Expire anything whose dates are all in the past.
            if req["dates"] and max(req["dates"]) < venue_today(req).isoformat():
                db.update_request(rid, status="expired")
                db.log_event(rid, "All requested dates have passed — expired")
                continue
            until = parse_iso(req.get("monitor_until"))
            if req["status"] == "monitoring" and until and until < now:
                db.update_request(rid, status="expired")
                db.log_event(rid, "Monitor end time reached — expired")
                continue

            if req["status"] == "scheduled" and req["mode"] in ("snipe", "reminder"):
                if rid not in self._timed or self._timed[rid].done():
                    self._timed[rid] = asyncio.create_task(self._run_timed(rid), name=f"timed-{rid}")

        # Monitors: run due checks one at a time (sequential = gentle).
        for req in db.list_requests(["monitoring"]):
            nxt = parse_iso(req.get("next_check_at"))
            if nxt and nxt > utcnow():
                continue
            await self.check_monitor(req["id"])

    # ------------------------------------------------------------ helpers
    def _client_find(self, req: dict[str, Any]):
        if req["platform"] == "resy":
            return lambda day, urgent=False: self.resy.find(
                req["venue_id"], day, req["party_size"], urgent=urgent,
                slug=req.get("venue_slug") or "", city=req.get("venue_city") or "")
        if req["platform"] == "sevenrooms":
            return lambda day, urgent=False: self.sr.find(
                req["venue_id"], day, req["party_size"], around=req.get("ideal_time") or req["time_start"], urgent=urgent)
        raise PlatformError(f"{req['platform']} is reminder-only")

    async def _act_on_matches(self, req: dict[str, Any], matches: list[Slot]) -> bool:
        """Book (or notify) the best match. Returns True when the request is resolved."""
        rid = req["id"]
        name = PLATFORM_NAMES.get(req["platform"], req["platform"])
        auto = bool(req.get("auto_book")) and req["platform"] in ("resy", "sevenrooms")
        bookable = [m for m in matches if m.bookable] if auto else []
        if not bookable:
            s = matches[0]
            others = ", ".join(f"{m.time}" for m in matches[1:4])
            note = "Needs a card — finish by hand" if auto else None
            db.update_request(rid, status="found", booked_date=s.date, booked_time=s.time,
                              booked_seating=s.seating, found_link=s.link, last_error=note)
            db.log_event(rid, f"Found {s.date} {s.time} {s.seating}".strip() + (f" ({note})" if note else ""))
            await push(f"Table found: {req['venue_name']}",
                       f"{s.date} at {s.time} for {req['party_size']}"
                       + (f" ({s.seating})" if s.seating else "")
                       + (f". Also: {others}" if others else "")
                       + (". Needs a card, so tap to book." if note else ". Tap to book."),
                       link=s.link, priority=5, tags=["fork_and_knife", "rotating_light"], request_id=rid)
            return True

        client = self.resy if req["platform"] == "resy" else self.sr
        async with self._book_lock:
            for s in bookable[:MAX_SLOTS_PER_ATTEMPT]:
                # Human-ish pause between seeing a slot and clicking it.
                await asyncio.sleep(random.uniform(0.15, 0.45))
                try:
                    b = await client.book(s, req["party_size"])
                except SlotGone as e:
                    db.log_event(rid, f"{s.time} gone before booking ({e}); trying next")
                    continue
                except PaymentRequired as e:  # includes "needs a human" cases
                    db.update_request(rid, status="found", booked_date=s.date, booked_time=s.time,
                                      booked_seating=s.seating, found_link=s.link,
                                      last_error=f"{e} — finish in {name}")
                    db.log_event(rid, f"Found {s.date} {s.time} but can't auto-book: {e}", "warn")
                    await push(f"Almost! {req['venue_name']}",
                               f"{s.date} {s.time} is open. {e}. Tap to finish in {name}.",
                               link=s.link, priority=5, tags=["raised_hand"], request_id=rid)
                    return True
                db.update_request(rid, status="booked", booked_date=s.date, booked_time=s.time,
                                  booked_seating=s.seating, confirmation=b.confirmation,
                                  booking_token=b.token or None, found_link=s.link, last_error=None)
                db.log_event(rid, f"BOOKED {s.date} {s.time} {s.seating} — confirmation {b.confirmation}")
                await push(f"Booked: {req['venue_name']}",
                           f"{s.date} at {s.time} for {req['party_size']}"
                           + (f" ({s.seating})" if s.seating else "") + f". Conf {b.confirmation}.",
                           link=s.link, priority=5, tags=["tada"], request_id=rid)
                return True
        return False

    async def _platform_trouble(self, req: dict[str, Any], e: Exception) -> None:
        """Pause, record, and tell the user once per pause."""
        rid = req["id"]
        platform = req["platform"]
        until = governor.paused_until(platform)
        msg = str(e)
        db.update_request(rid, last_error=msg)
        db.log_event(rid, msg, "error")
        if until and db.kv_get(f"notified_pause:{platform}") != until:
            db.kv_set(f"notified_pause:{platform}", until)
            await push(f"{platform.title()} paused",
                       f"{msg}. All {platform} activity is cooling off to protect your account.",
                       priority=4, tags=["warning"], request_id=rid)

    # --------------------------------------------------------------- timed
    async def _sleep_until(self, target: datetime) -> None:
        """Coarse sleep, then a fine-grained approach to the exact instant."""
        while True:
            remaining = (target - utcnow()).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining - 1.5 if remaining > 3 else max(0.005, remaining / 2))

    async def _run_timed(self, rid: str) -> None:
        try:
            req = db.get_request(rid)
            if not req or req["status"] != "scheduled":
                return
            release = parse_iso(req.get("release_at"))
            if not release:
                db.update_request(rid, status="failed", last_error="No release time set")
                return
            if req["mode"] == "reminder" or req["platform"] == "opentable":
                await self._run_reminder(req, release)
            else:
                await self._run_snipe(req, release)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("timed task failed")
            db.update_request(rid, status="failed", last_error=str(e))
            db.log_event(rid, f"Snipe crashed: {e}", "error")
            await push("Snipe error", f"{e}", priority=4, tags=["warning"], request_id=rid)

    async def _run_reminder(self, req: dict[str, Any], release: datetime) -> None:
        rid = req["id"]
        day = (req["dates"] or [None])[0]
        link = opentable.venue_link(req.get("venue_slug") or "", req["venue_id"] if not req.get("venue_slug") else "",
                                    day, req.get("ideal_time") or req["time_start"], req["party_size"]) \
            if req["platform"] == "opentable" else (req.get("venue_url") or "")
        heads_up = release - timedelta(seconds=60)
        if heads_up > utcnow():
            await self._sleep_until(heads_up)
            await push(f"1 minute: {req['venue_name']}",
                       f"Reservations open in 60s for {day}. Open the page now and be ready.",
                       link=link, priority=4, tags=["alarm_clock"], request_id=rid)
        await self._sleep_until(release)
        await push(f"GO: {req['venue_name']} is open",
                   f"Book {day} {req.get('ideal_time') or req['time_start']} for {req['party_size']} now.",
                   link=link, priority=5, tags=["rotating_light"], request_id=rid)
        db.update_request(rid, status="notified", found_link=link)

    async def _run_snipe(self, req: dict[str, Any], release: datetime) -> None:
        rid = req["id"]
        # 1) Pre-flight a few minutes early: make sure login + card are good,
        #    so the hot path is just find → details → book.
        pre = release - timedelta(seconds=PREAUTH_SECONDS)
        if pre > utcnow():
            await self._sleep_until(pre)
        if req["platform"] == "resy" and req.get("auto_book"):
            try:
                await self.resy.ensure_auth(min_valid_seconds=3 * 86400)
                if not self.resy.auth_status().get("payment_method_id"):
                    await self.resy.load_user()
                db.log_event(rid, "Pre-flight OK: logged in, ready")
            except Exception as e:
                db.log_event(rid, f"Pre-flight problem: {e}", "warn")
                await push("Snipe pre-flight problem", f"{req['venue_name']}: {e}", priority=4,
                           tags=["warning"], request_id=rid)
        if req["platform"] == "sevenrooms" and req.get("auto_book") and not guest_details():
            db.log_event(rid, "Pre-flight: no booking details saved — will notify instead of booking", "warn")
            await push("Add your booking details", f"{req['venue_name']}: SevenRooms auto-book needs your name, "
                       "email and phone (Rezzie → Settings). Until then you'll get a link instead.",
                       priority=4, tags=["warning"], request_id=rid)

        # 2) Fire at T+50ms (firing early just returns the old, empty grid).
        #    Keep monitors off this platform around the burst.
        window = req.get("snipe_window_seconds") or settings.snipe_window_seconds
        lead = max(0.0, (release - utcnow()).total_seconds())
        governor.hold(req["platform"], lead + window + 30)
        await self._sleep_until(release + timedelta(milliseconds=50))
        req = db.get_request(rid)
        if not req or req["status"] != "scheduled":
            return
        db.update_request(rid, status="attempting")
        db.log_event(rid, "Release time — sniping")

        deadline = time.monotonic() + window
        find = self._client_find(req)
        attempts = 0
        day_cycle = req["dates"] or []
        while time.monotonic() < deadline:
            day = day_cycle[attempts % len(day_cycle)]
            attempts += 1
            try:
                slots = await find(day, urgent=True)
                db.update_request(rid, attempt_count=(req.get("attempt_count") or 0) + attempts,
                                  last_checked=db.now_iso())
                matches = matching_slots(req, slots)
                if matches and await self._act_on_matches(db.get_request(rid) or req, matches):
                    return
                if slots and not matches and attempts == 1:
                    db.log_event(rid, f"{len(slots)} slots released but none match your window")
            except (RateLimited, Blocked, PlatformPaused, BudgetExhausted) as e:
                await self._platform_trouble(req, e)
                db.update_request(rid, status="failed")
                return
            except AuthError as e:
                db.log_event(rid, f"Auth problem: {e}", "error")
                db.update_request(rid, status="failed", last_error=str(e))
                await push("Snipe failed: login", f"{req['venue_name']}: {e}", priority=4,
                           tags=["warning"], request_id=rid)
                return
            except PlatformError as e:
                db.log_event(rid, f"attempt {attempts}: {e}", "warn")
            await asyncio.sleep(random.uniform(settings.snipe_min_interval_ms, settings.snipe_max_interval_ms) / 1000)

        db.log_event(rid, f"No matching table in the {window}s release window ({attempts} checks)")
        if req.get("fallback_to_monitor"):
            nxt = utcnow() + timedelta(seconds=effective_poll_interval(req.get("poll_interval_seconds")))
            db.update_request(rid, status="monitoring", next_check_at=nxt.isoformat(timespec="seconds"))
            await push(f"Missed the drop: {req['venue_name']}",
                       "Nothing matched at release. Now watching for cancellations.",
                       priority=3, tags=["eyes"], request_id=rid)
        else:
            db.update_request(rid, status="failed", last_error="No matching table at release")
            await push(f"Missed: {req['venue_name']}", "No matching table at release.", priority=3,
                       request_id=rid)

    # ------------------------------------------------------------- monitor
    async def check_monitor(self, rid: str) -> None:
        req = db.get_request(rid)
        if not req or req["status"] != "monitoring":
            return
        if req["platform"] == "opentable":
            db.update_request(rid, status="notified")
            return
        held = governor.held_for(req["platform"])
        if held > 0:  # a snipe owns this platform right now
            nxt = utcnow() + timedelta(seconds=held + random.uniform(10, 60))
            db.update_request(rid, next_check_at=nxt.isoformat(timespec="seconds"))
            return
        today = venue_today(req).isoformat()
        dates = [d for d in req["dates"] if d >= today]
        interval = self.monitor_interval(req)
        try:
            find = self._client_find(req)
            to_check = dates
            if req["platform"] == "resy" and len(dates) > 1:
                # One calendar call tells us which dates have anything at all.
                cal = await self.resy.calendar(req["venue_id"], req["party_size"], dates[0], dates[-1])
                if cal:
                    to_check = [d for d in dates if cal.get(d) == "available"][:3]
                else:  # unexpected/empty calendar — fall back to rotating finds
                    k = (req.get("attempt_count") or 0) % len(dates)
                    to_check = (dates[k:] + dates[:k])[:2]
            elif len(dates) > 3:
                # Rotate through dates a few per check rather than all at once.
                k = (req.get("attempt_count") or 0) % len(dates)
                to_check = (dates[k:] + dates[:k])[:3]
            all_slots: list[Slot] = []
            checked: dict[str, list[Slot]] = {}
            for d in to_check:
                checked[d] = await find(d)
                all_slots += checked[d]
            try:
                self._learn(req, checked)
            except Exception as e:  # learning must never break monitoring
                log.warning("learn failed: %s", e)
            db.update_request(rid, attempt_count=(req.get("attempt_count") or 0) + 1,
                              last_checked=db.now_iso(), last_error=None)
            matches = matching_slots(req, all_slots)
            if matches:
                db.log_event(rid, f"Opening spotted: {', '.join(m.date + ' ' + m.time for m in matches[:4])}")
                if await self._act_on_matches(db.get_request(rid) or req, matches):
                    return
        except (PlatformPaused,) as e:
            nxt = datetime.fromtimestamp(e.until, timezone.utc) + timedelta(seconds=random.uniform(30, 300))
            db.update_request(rid, next_check_at=nxt.isoformat(timespec="seconds"), last_error=f"Paused: {e.reason}")
            return
        except BudgetExhausted as e:
            nxt = utcnow() + timedelta(seconds=seconds_until_local_midnight() + random.uniform(60, 600))
            db.update_request(rid, next_check_at=nxt.isoformat(timespec="seconds"), last_error=str(e))
            return
        except (RateLimited, Blocked) as e:
            await self._platform_trouble(req, e)
            until = governor.paused_until(req["platform"])
            nxt = datetime.fromtimestamp(max(until, time.time() + interval), timezone.utc)
            db.update_request(rid, next_check_at=nxt.isoformat(timespec="seconds"))
            return
        except AuthError as e:
            db.update_request(rid, status="paused", last_error=str(e))
            db.log_event(rid, f"Paused — login problem: {e}", "error")
            await push("Monitor paused: login", f"{req['venue_name']}: {e}", priority=4,
                       tags=["warning"], request_id=rid)
            return
        except PlatformError as e:
            db.update_request(rid, last_error=str(e))
            db.log_event(rid, f"check failed: {e}", "warn")
        nxt = utcnow() + timedelta(seconds=interval)
        db.update_request(rid, next_check_at=nxt.isoformat(timespec="seconds"))

    # ------------------------------------------------------------ pacing
    @staticmethod
    def calls_per_check(req: dict[str, Any]) -> float:
        n = len(req.get("dates") or []) or 1
        if req["platform"] == "resy":
            return 1.0 if n == 1 else 2.0  # calendar + usually one find
        return float(min(n, 3))

    def plan(self, platform: str, now: datetime | None = None) -> dict[str, float]:
        """Budget plan for the rest of today on one platform.

        Every monitor checks at a rate proportional to its hotness. The
        "pace" P is seconds per unit of hotness, chosen so that all monitors'
        weighted checks fit the remaining monitor budget until local midnight:
            sum_m cost_m * ∫ hotness_m dt / P  <=  remaining calls
        A monitor's interval at time t is then P / hotness_m(t).
        """
        now = now or utcnow()
        remaining = monitor_budget(platform) - db.usage_get(platform)
        if remaining <= 0:
            return {"pace": float("inf"), "remaining": 0}
        end = now + timedelta(seconds=seconds_until_local_midnight(now))
        load = sum(self.calls_per_check(r) * hotness_mod.weight_until(r, now, end)
                   for r in db.list_requests(["monitoring"]) if r["platform"] == platform)
        return {"pace": load / remaining if load else 0.0, "remaining": remaining}

    def monitor_interval(self, req: dict[str, Any], dates: list[str] | None = None, today: str | None = None,
                         now: datetime | None = None) -> float:
        """Seconds until this monitor's next check: the slower of your chosen
        interval and the budget pace, both divided by the current hotness."""
        now = now or utcnow()
        w = max(0.05, hotness_mod.hotness(req, now))
        base = float(max(settings.min_poll_seconds, int(req.get("poll_interval_seconds") or 0) or 120))
        pace = self.plan(req["platform"], now)["pace"]
        interval = max(settings.min_poll_seconds, base / w, pace / w)
        # The budget refills at midnight, so never sleep much past it.
        interval = min(interval, seconds_until_local_midnight(now) + random.uniform(60, 600))
        return jitter(interval, 0.2)

    def _learn(self, req: dict[str, Any], checked: dict[str, list[Slot]]) -> None:
        """Log tables that weren't there on the previous check (= openings),
        and remember the restaurant's cancellation cutoff if Resy reported it."""
        key = f"seen:{req['id']}"
        prev = db.kv_get(key, {}) or {}
        tz = hotness_mod._tz(req.get("venue_tz"))
        now_local = datetime.now(tz)
        for day, slots in checked.items():
            times = sorted({s.time for s in slots})
            if day in prev:
                for t in set(times) - set(prev[day]):
                    y, mo, d = (int(x) for x in day.split("-"))
                    hh, mm = (int(x) for x in t.split(":"))
                    slot_dt = datetime(y, mo, d, hh, mm, tzinfo=tz)
                    hours_before = (slot_dt - now_local).total_seconds() / 3600
                    if hours_before > 0:
                        db.record_opening(req["platform"], req["venue_id"], req["venue_name"], day, t,
                                          now_local.hour, hours_before)
            prev[day] = times
            for s in slots:
                if s.raw.get("cancel_cutoff_secs"):
                    hotness_mod.remember_cutoff(req, s.raw["cancel_cutoff_secs"])
                    break
        today = now_local.date().isoformat()
        db.kv_set(key, {d: v for d, v in prev.items() if d >= today})

    # ------------------------------------------------------------ manual
    async def check_now(self, rid: str) -> list[dict[str, Any]]:
        """Manual 'check now' from the UI — one find per date (max 3), no booking."""
        req = db.get_request(rid)
        if not req or req["platform"] == "opentable":
            return []
        find = self._client_find(req)
        out: list[Slot] = []
        for d in [d for d in req["dates"] if d >= venue_today(req).isoformat()][:3]:
            out += await find(d)
        db.update_request(rid, last_checked=db.now_iso())
        m = matching_slots(req, out)
        db.log_event(rid, f"Manual check: {len(out)} slots, {len(m)} match")
        return [s.public() for s in out]


engine = Engine()
