import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.config import settings
from app.engine import Engine
from app.platforms.base import Booking, RateLimited, Slot, SlotGone


class FakeResy:
    def __init__(self, find_results, book_results=None):
        self.find_results = list(find_results)
        self.book_results = list(book_results or [])
        self.finds = 0
        self.booked = []

    async def ensure_auth(self, **kw):
        return {"token": "t"}

    def auth_status(self):
        return {"payment_method_id": 1}

    async def load_user(self):
        return {}

    async def find(self, venue_id, day, party, urgent=False, slug="", city=""):
        self.finds += 1
        r = self.find_results.pop(0) if len(self.find_results) > 1 else self.find_results[0]
        if isinstance(r, Exception):
            raise r
        return r

    async def calendar(self, *a):
        return {}

    async def book(self, slot, party):
        self.booked.append(slot)
        r = self.book_results.pop(0) if self.book_results else Booking("CONF1", "RT")
        if isinstance(r, Exception):
            raise r
        return r


def base_req(**kw):
    v = dict(platform="resy", mode="snipe", status="scheduled", venue_id="1", venue_name="Testaurant",
             venue_tz="America/New_York", party_size=2, dates=["2099-01-01"], time_start="18:00",
             time_end="21:00", ideal_time="19:30", seating_exclude=[], auto_book=True,
             snipe_window_seconds=10, fallback_to_monitor=True, poll_interval_seconds=120)
    v.update(kw)
    return db.create_request(v)


def slot(t, seat="Dining Room"):
    return Slot("resy", "2099-01-01", t, seat, token="cfg-" + t, link="https://resy.com/x")


@pytest.mark.asyncio
async def test_snipe_books_best_slot_after_release():
    settings.snipe_min_interval_ms = 10
    settings.snipe_max_interval_ms = 20
    release = datetime.now(timezone.utc) + timedelta(seconds=1)
    r = base_req(release_at=release.isoformat())
    resy = FakeResy([[], [slot("17:00"), slot("20:30"), slot("19:15")]])
    eng = Engine(resy=resy)
    await eng._run_timed(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "booked"
    assert out["booked_time"] == "19:15" and out["confirmation"] == "CONF1"
    assert resy.finds == 2
    assert datetime.now(timezone.utc) >= release


@pytest.mark.asyncio
async def test_snipe_tries_next_slot_when_first_is_gone():
    settings.snipe_min_interval_ms = 10
    settings.snipe_max_interval_ms = 20
    r = base_req(release_at=(datetime.now(timezone.utc) + timedelta(seconds=0.5)).isoformat())
    resy = FakeResy([[slot("19:30"), slot("20:00")]], [SlotGone("gone"), Booking("C2", "RT2")])
    await Engine(resy=resy)._run_timed(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "booked" and out["booked_time"] == "20:00"


@pytest.mark.asyncio
async def test_snipe_falls_back_to_monitor_when_nothing_matches():
    settings.snipe_min_interval_ms = 10
    settings.snipe_max_interval_ms = 20
    r = base_req(release_at=(datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat(), snipe_window_seconds=1)
    resy = FakeResy([[slot("23:00")]])
    await Engine(resy=resy)._run_timed(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "monitoring" and out["next_check_at"]
    assert resy.booked == []


@pytest.mark.asyncio
async def test_snipe_stops_immediately_on_rate_limit():
    settings.snipe_min_interval_ms = 10
    settings.snipe_max_interval_ms = 20
    r = base_req(release_at=(datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat())
    resy = FakeResy([RateLimited("429", 429)])
    await Engine(resy=resy)._run_timed(r["id"])
    assert db.get_request(r["id"])["status"] == "failed"
    assert resy.finds == 1


@pytest.mark.asyncio
async def test_monitor_notify_only_marks_found():
    r = base_req(mode="monitor", status="monitoring", auto_book=False, release_at=None)
    resy = FakeResy([[slot("19:00")]])
    await Engine(resy=resy).check_monitor(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "found" and out["booked_time"] == "19:00"
    assert resy.booked == []


@pytest.mark.asyncio
async def test_monitor_reschedules_with_floor_interval():
    settings.min_poll_seconds = 60
    settings.quiet_hours = ""
    r = base_req(mode="monitor", status="monitoring", release_at=None, poll_interval_seconds=5)
    await Engine(resy=FakeResy([[]])).check_monitor(r["id"])
    out = db.get_request(r["id"])
    nxt = datetime.fromisoformat(out["next_check_at"])
    assert nxt - datetime.now(timezone.utc) > timedelta(seconds=40)
    assert out["status"] == "monitoring" and out["attempt_count"] == 1


@pytest.mark.asyncio
async def test_opentable_reminder_marks_notified():
    r = base_req(platform="opentable", mode="reminder", venue_id="carbone-new-york", venue_slug="carbone-new-york",
                 auto_book=False, release_at=(datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat())
    await Engine(resy=FakeResy([[]]))._run_timed(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "notified"
    assert out["found_link"].startswith("https://www.opentable.com/r/carbone-new-york?covers=2&dateTime=2099-01-01T19")


@pytest.mark.asyncio
async def test_tick_expires_past_dates():
    r = base_req(mode="monitor", status="monitoring", dates=["2001-01-01"], release_at=None)
    await Engine(resy=FakeResy([[]])).tick()
    assert db.get_request(r["id"])["status"] == "expired"


class FakeSR(FakeResy):
    async def find(self, slug, day, party, around="19:00", urgent=False):
        return await FakeResy.find(self, slug, day, party, urgent=urgent)


def sr_slot(t, bookable=True):
    return Slot("sevenrooms", "2099-01-01", t, "Dining Room", token="ACC", bookable=bookable,
                link="https://www.sevenrooms.com/explore/x/reservations/create/search/")


@pytest.mark.asyncio
async def test_sevenrooms_monitor_auto_books_no_card_slot():
    r = base_req(platform="sevenrooms", mode="monitor", status="monitoring", release_at=None, auto_book=True)
    sr = FakeSR([[sr_slot("19:30", bookable=False), sr_slot("20:00")]], [Booking("SR1")])
    await Engine(resy=FakeResy([[]]), sevenrooms=sr).check_monitor(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "booked" and out["booked_time"] == "20:00" and out["confirmation"] == "SR1"
    assert [s.time for s in sr.booked] == ["20:00"]


@pytest.mark.asyncio
async def test_sevenrooms_card_only_slots_notify():
    r = base_req(platform="sevenrooms", mode="monitor", status="monitoring", release_at=None, auto_book=True)
    sr = FakeSR([[sr_slot("19:30", bookable=False)]])
    await Engine(resy=FakeResy([[]]), sevenrooms=sr).check_monitor(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "found" and "card" in out["last_error"] and sr.booked == []


def test_usage_day_is_local():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert db.today_key() == datetime.now(ZoneInfo(settings.app_timezone)).strftime("%Y-%m-%d")
