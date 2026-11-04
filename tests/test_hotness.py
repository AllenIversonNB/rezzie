from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.config import settings
from app.engine import Engine
from app.hotness import hotness, remember_cutoff
from app.platforms.base import Slot

NY = ZoneInfo("America/New_York")


def req(**kw):
    v = dict(platform="resy", mode="monitor", status="monitoring", venue_id="77", venue_name="Hot Spot",
             venue_tz="America/New_York", party_size=2, dates=["2099-06-10"], time_start="18:00",
             time_end="21:00", ideal_time="19:30", seating_exclude=[], auto_book=True,
             poll_interval_seconds=120)
    v.update(kw)
    return db.create_request(v)


@pytest.fixture(autouse=True)
def quiet():
    settings.quiet_hours = "01-07"
    settings.min_poll_seconds = 60
    yield
    settings.quiet_hours = "01-07"


def at(y, m, d, h, mi=0):
    return datetime(2099, m, d, h, mi, tzinfo=NY) if y is None else datetime(y, m, d, h, mi, tzinfo=NY)


def test_windows():
    r = req()
    assert hotness(r, at(None, 6, 9, 18)) == 3.0          # 25.5h before 7:30pm: fee-deadline rush
    assert hotness(r, at(None, 6, 10, 10)) == 2.5         # day-of late morning
    assert hotness(r, at(None, 6, 10, 16)) == 2.0         # day-of afternoon
    assert hotness(r, at(None, 6, 9, 12)) == 1.3          # within 48h, nothing special
    assert hotness(r, at(None, 5, 25, 12)) == 0.6         # 2+ weeks out
    assert hotness(r, at(None, 5, 25, 3)) == pytest.approx(0.12)  # far + overnight
    assert hotness(r, at(None, 6, 8, 18)) == 2.0          # 48h secondary bump


def test_learned_cutoff_moves_the_rush():
    r = req()
    remember_cutoff(r, 48 * 3600)
    assert hotness(r, at(None, 6, 8, 18)) == 3.0          # 49.5h before: new deadline
    assert hotness(r, at(None, 6, 9, 18)) == 1.3          # old 24h window no longer special


def test_intervals_follow_hotness_and_share_budget():
    eng = Engine()
    r = req()
    hot = eng.monitor_interval(r, now=at(None, 6, 9, 18))
    normal = eng.monitor_interval(r, now=at(None, 6, 9, 12))
    night = eng.monitor_interval(r, now=at(None, 6, 9, 3))
    assert hot < normal < night
    assert hot <= 60 * 1.2 + 1                                # 120s/3 floors at 60s (±20% jitter)
    for _ in range(7):
        req(venue_id="x")
    crowded = eng.monitor_interval(r, now=at(None, 6, 9, 12))
    assert crowded > normal * 2                               # 8 monitors share the budget


def test_budget_lasts_until_midnight():
    eng = Engine()
    rs = [req(venue_id=str(i)) for i in range(5)]
    now = at(None, 6, 9, 8)
    plan = eng.plan("resy", now)
    # Simulate the rest of the day in 10-minute steps: expected calls must fit.
    t, expected = now, 0.0
    end = (now + timedelta(days=1)).replace(hour=0, minute=0)
    while t < end:
        for r in rs:
            expected += 600 / max(60, 120 / hotness(r, t), plan["pace"] / hotness(r, t))
        t += timedelta(minutes=10)
    assert expected <= plan["remaining"] * 1.05


def test_learn_logs_new_tables_and_cutoff():
    eng = Engine()
    r = req(dates=["2099-06-10"])
    s1 = [Slot("resy", "2099-06-10", "17:00", raw={"cancel_cutoff_secs": 86400})]
    eng._learn(r, {"2099-06-10": s1})
    assert db.opening_stats()["total"] == 0                   # first sighting is the baseline
    eng._learn(r, {"2099-06-10": s1 + [Slot("resy", "2099-06-10", "19:30")]})
    st = db.opening_stats()
    assert st["total"] == 1 and sum(st["by_hour"]) == 1
    assert sum(b["count"] for b in st["by_hours_before"]) == 1
    assert db.kv_get("cutoff:resy:77") == 24.0
