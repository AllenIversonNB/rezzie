import time

import pytest

from app import db
from app.platforms.base import Blocked, RateLimited, Slot
from app.platforms.resy import ResyClient
from app.safety import PlatformPaused
from conftest import make_jwt

FIND = {"results": {"venues": [{"venue": {"name": "X"}, "slots": [
    {"config": {"id": 1, "token": "cfg-1930", "type": "Dining Room"}, "date": {"start": "2026-12-01 19:30:00"}},
    {"config": {"id": 2, "token": "cfg-2100", "type": "Bar"}, "date": {"start": "2026-12-01 21:00:00"}},
]}]}}


def login_ok(fake):
    fake.add("POST", "/3/auth/password", body={"token": make_jwt(time.time() + 40 * 86400), "payment_method_id": 77,
                                              "first_name": "J"}, cookies={"production_refresh_token": "R1"})


@pytest.mark.asyncio
async def test_login_stores_token_and_refresh(fake, gov):
    login_ok(fake)
    c = ResyClient(fake, gov)
    a = await c.login()
    assert a["refresh"] == "R1" and a["payment_method_id"] == 77
    call = fake.calls[0]
    assert call["data"] == {"email": "me@example.com", "password": "hunter2"}
    assert call["headers"]["Authorization"] == 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"'
    assert "X-Resy-Auth-Token" not in call["headers"]
    assert c.auth_status()["logged_in"]


@pytest.mark.asyncio
async def test_find_parses_slots_and_sends_auth(fake, gov):
    login_ok(fake)
    fake.add("GET", "/4/find", body=FIND)
    slots = await ResyClient(fake, gov).find("123", "2026-12-01", 2, slug="carbone", city="new-york-ny")
    assert [(s.time, s.seating, s.token) for s in slots] == [("19:30", "Dining Room", "cfg-1930"), ("21:00", "Bar", "cfg-2100")]
    assert "carbone" in slots[0].link and "seats=2" in slots[0].link
    find_call = [c for c in fake.calls if c["path"] == "/4/find"][0]
    assert find_call["headers"]["X-Resy-Auth-Token"]
    assert find_call["params"]["venue_id"] == "123"


@pytest.mark.asyncio
async def test_find_falls_back_to_post_on_405(fake, gov):
    login_ok(fake)
    fake.add("GET", "/4/find", status=405)
    fake.add("POST", "/4/find", body=FIND)
    slots = await ResyClient(fake, gov).find("123", "2026-12-01", 2)
    assert len(slots) == 2
    assert fake.calls[-1]["json"]["venue_id"] == 123


@pytest.mark.asyncio
async def test_book_flow_details_then_book(fake, gov):
    login_ok(fake)
    fake.add("GET", "/3/details", body={"book_token": {"value": "BT", "date_expires": "x"}})
    fake.add("POST", "/3/book", body={"resy_token": "RT", "reservation_id": 999})
    c = ResyClient(fake, gov)
    await c.login()
    b = await c.book(Slot("resy", "2026-12-01", "19:30", token="cfg"), 2)
    assert b.confirmation == "999" and b.token == "RT"
    book = fake.calls[-1]
    assert book["data"]["book_token"] == "BT"
    assert book["data"]["struct_payment_method"] == '{"id": 77}'
    assert book["data"]["source_id"] == "resy.com-venue-details"


@pytest.mark.asyncio
async def test_auth_error_triggers_refresh_then_retry(fake, gov):
    login_ok(fake)
    c = ResyClient(fake, gov)
    await c.login()
    fake.add("GET", "/4/find", status=419)
    fake.add("GET", "/4/find", body=FIND)
    fake.add("POST", "/3/auth/refresh", body={"token": make_jwt(time.time() + 40 * 86400)})
    slots = await c.find("1", "2026-12-01", 2)
    assert len(slots) == 2
    refresh = [x for x in fake.calls if x["path"] == "/3/auth/refresh"][0]
    assert refresh["cookies"] == {"production_refresh_token": "R1"}


@pytest.mark.asyncio
async def test_429_pauses_platform_and_blocks_further_calls(fake, gov):
    login_ok(fake)
    c = ResyClient(fake, gov)
    await c.login()
    fake.add("GET", "/4/find", status=429)
    with pytest.raises(RateLimited):
        await c.find("1", "2026-12-01", 2)
    assert gov.paused_until("resy") > time.time() + 250
    n = len(fake.calls)
    with pytest.raises(PlatformPaused):
        await c.find("1", "2026-12-01", 2)
    assert len(fake.calls) == n  # nothing sent while paused


@pytest.mark.asyncio
async def test_403_is_a_block_with_longer_pause(fake, gov):
    login_ok(fake)
    c = ResyClient(fake, gov)
    await c.login()
    fake.add("GET", "/4/find", status=403, text="<html>Access Denied</html>")
    with pytest.raises(Blocked):
        await c.find("1", "2026-12-01", 2)
    assert gov.paused_until("resy") > time.time() + 1700


@pytest.mark.asyncio
async def test_password_logins_are_capped(fake, gov):
    from app.config import settings
    fake.add("POST", "/3/auth/password", status=401)
    c = ResyClient(fake, gov)
    for _ in range(settings.max_password_logins_per_day):
        with pytest.raises(Exception):
            await c.login()
    with pytest.raises(Exception, match="Too many password logins"):
        await c.login()
    assert db.usage_get("resy", "login") == settings.max_password_logins_per_day


@pytest.mark.asyncio
async def test_dry_run_never_books(fake, gov):
    from app.config import settings
    settings.dry_run = True
    b = await ResyClient(fake, gov).book(Slot("resy", "2026-12-01", "19:30", token="cfg"), 2)
    assert b.confirmation == "DRY-RUN" and fake.calls == []
