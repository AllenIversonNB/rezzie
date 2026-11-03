import pytest

from app.platforms.base import PaymentRequired, Slot, SlotGone
from app.platforms.sevenrooms import (
    NeedsHuman,
    SevenRoomsClient,
    guest_details,
    parse_slug,
    save_guest_details,
    to_hhmm,
    venue_link,
)

RANGE = {"status": 200, "data": {"availability": {"2099-03-01": [
    {"shift_persistent_id": "SHIFT-DINNER", "shift_category": "DINNER", "times": [
        {"time": "6:30 PM", "type": "request", "is_requestable": True},
        {"time": "7:00 PM", "type": "book", "access_persistent_id": "ACC-1900",
         "public_time_slot_description": "Dining Room"},
        {"time": "7:30 PM", "type": "book", "access_persistent_id": "ACC-1930", "require_credit_card": True},
        {"time": "8:00 PM", "type": "book", "access_persistent_id": None},
    ]},
    {"is_closed": True, "times": [{"time": "11:00 AM", "type": "book", "access_persistent_id": "X"}]},
]}}}


def test_helpers():
    assert to_hhmm("7:00 PM") == "19:00" and to_hhmm("12:15 am") == "00:15" and to_hhmm("19:30:00") == "19:30"
    assert parse_slug("https://www.sevenrooms.com/explore/carbonemiami/reservations/create/search/") == "carbonemiami"
    assert parse_slug("https://www.sevenrooms.com/reservations/thegrill?x=1") == "thegrill"
    assert venue_link("thegrill", "2099-03-01", 2, "19:00") == \
        "https://www.sevenrooms.com/explore/thegrill/reservations/create/search/?date=2099-03-01&party_size=2&start_time=19:00"


@pytest.mark.asyncio
async def test_find_uses_ng_endpoint_and_skips_request_times(fake, gov):
    fake.add("GET", "/api-yoa/availability/ng/widget/range", body=RANGE)
    slots = await SevenRoomsClient(fake, gov).find("thegrill", "2099-03-01", 2, "19:00")
    assert [(s.time, s.bookable) for s in slots] == [("19:00", True), ("19:30", False)]
    assert slots[0].raw["shift_persistent_id"] == "SHIFT-DINNER"
    assert slots[0].seating == "Dining Room"
    assert "start_time=19:00" in slots[0].link
    p = fake.calls[0]["params"]
    assert p["start_date"] == "2099-03-01" and p["num_days"] == 1 and p["channel"] == "SEVENROOMS_WIDGET"


@pytest.mark.asyncio
async def test_venue_info(fake, gov):
    fake.add("GET", "/api-yoa/dining/venue_info", body={"data": {"venue_info": {
        "name": "The Grill", "url_key": "thegrill", "timezone": "America/New_York", "city": "New York"},
        "widget_settings": {"maxDaysOut": 30}}})
    v = await SevenRoomsClient(fake, gov).venue("https://www.sevenrooms.com/reservations/TheGrill")
    assert (v.id, v.name, v.timezone, v.lead_time_days) == ("thegrill", "The Grill", "America/New_York", 30)
    assert fake.calls[0]["params"] == {"venue_url_key": "thegrill", "widget_type": "res_widget"}


def test_guest_details_validation():
    with pytest.raises(ValueError):
        save_guest_details("J", "", "x@y.com", "2125551212")
    d = save_guest_details("Joe", "F", "j@example.com", "+1 (212) 555-1212")
    assert d["phone"] == "2125551212" and d["dial_code"] == "1"
    assert guest_details()["email"] == "j@example.com"


async def _slots(fake, gov):
    fake.add("GET", "/api-yoa/availability/ng/widget/range", body=RANGE)
    return await SevenRoomsClient(fake, gov).find("thegrill", "2099-03-01", 4)


@pytest.mark.asyncio
async def test_book_holds_then_submits_guest_form(fake, gov):
    save_guest_details("Joe", "F", "j@example.com", "2125551212")
    slot = (await _slots(fake, gov))[0]
    fake.add("POST", "/api-yoa/dining/hold/add", body={"status": 200, "data": {
        "reservation_hold_id": "HOLD-1", "hold_duration_sec": 300}})
    fake.add("POST", "/booking/dining/widget/thegrill/book", body={"status": 200, "data": {
        "reference_code": "ABC123"}})
    b = await SevenRoomsClient(fake, gov).book(slot, 4)
    assert b.confirmation == "ABC123"
    hold = fake.calls[-2]["json"]
    assert hold == {"venue": "thegrill", "date": "2099-03-01", "time": "7:00 PM", "party_size": 4,
                    "access_persistent_id": "ACC-1900", "shift_persistent_id": "SHIFT-DINNER",
                    "channel": "SEVENROOMS_WIDGET"}
    form = fake.calls[-1]["multipart"]
    assert form["reservation_hold_id"] == "HOLD-1" and form["date"] == "03-01-2099"
    assert form["first_name"] == "Joe" and form["phone_number"] == "2125551212" and form["party_size"] == "4"
    assert all(form[k] == "false" for k in form if k.endswith("opt_in"))


@pytest.mark.asyncio
async def test_card_slots_and_missing_details_are_never_booked(fake, gov):
    slots = await _slots(fake, gov)
    with pytest.raises(NeedsHuman):
        await SevenRoomsClient(fake, gov).book(slots[0], 2)  # no guest details yet
    save_guest_details("Joe", "F", "j@example.com", "2125551212")
    with pytest.raises(PaymentRequired):
        await SevenRoomsClient(fake, gov).book(slots[1], 2)  # require_credit_card
    assert not [c for c in fake.calls if c["method"] == "POST"]


@pytest.mark.asyncio
async def test_failed_hold_is_slot_gone(fake, gov):
    save_guest_details("Joe", "F", "j@example.com", "2125551212")
    slot = (await _slots(fake, gov))[0]
    fake.add("POST", "/api-yoa/dining/hold/add", body={"status": 400, "msg": "no longer available"})
    with pytest.raises(SlotGone):
        await SevenRoomsClient(fake, gov).book(slot, 2)
    assert not [c for c in fake.calls if "/book" in c["path"]]


@pytest.mark.asyncio
async def test_captcha_on_book_needs_human(fake, gov):
    save_guest_details("Joe", "F", "j@example.com", "2125551212")
    slot = (await _slots(fake, gov))[0]
    fake.add("POST", "/api-yoa/dining/hold/add", body={"status": 200, "data": {"reservation_hold_id": "H"}})
    fake.add("POST", "/booking/dining/widget/thegrill/book", status=400, text='{"msg": "recaptcha required"}')
    with pytest.raises(NeedsHuman):
        await SevenRoomsClient(fake, gov).book(slot, 2)
