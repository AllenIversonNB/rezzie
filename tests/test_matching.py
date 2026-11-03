from app.matching import matching_slots
from app.platforms.base import Slot


def S(d, t, seat=""):
    return Slot(platform="resy", date=d, time=t, seating=seat, token="x")


REQ = {"dates": ["2026-12-01", "2026-12-02"], "time_start": "18:30", "time_end": "21:00",
       "ideal_time": "19:30", "seating_exclude": ["bar"]}


def test_filters_window_and_dates():
    out = matching_slots(REQ, [S("2026-12-01", "17:00"), S("2026-12-01", "22:00"), S("2026-12-05", "19:30"),
                               S("2026-12-01", "20:00")])
    assert [s.time for s in out] == ["20:00"]


def test_excludes_seating_case_insensitive():
    out = matching_slots(REQ, [S("2026-12-01", "19:30", "Bar Counter"), S("2026-12-01", "19:45", "Dining Room")])
    assert [s.time for s in out] == ["19:45"]


def test_ranks_by_date_then_closeness_to_ideal():
    out = matching_slots(REQ, [S("2026-12-02", "19:30"), S("2026-12-01", "21:00"), S("2026-12-01", "19:15")])
    assert [(s.date, s.time) for s in out] == [("2026-12-01", "19:15"), ("2026-12-01", "21:00"), ("2026-12-02", "19:30")]


def test_window_crossing_midnight():
    req = {**REQ, "time_start": "22:00", "time_end": "00:30", "ideal_time": "23:00", "seating_exclude": []}
    out = matching_slots(req, [S("2026-12-01", "23:30"), S("2026-12-01", "00:15"), S("2026-12-01", "20:00")])
    assert {s.time for s in out} == {"23:30", "00:15"}
