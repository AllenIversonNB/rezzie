import pytest

from app import db
from app.config import settings
from app.safety import BudgetExhausted, PlatformPaused


@pytest.mark.asyncio
async def test_monitor_budget_reserves_headroom_for_snipes(gov):
    settings.daily_call_budget = 8  # tiny budget: monitors get half
    for _ in range(4):
        await gov.acquire("resy")
    with pytest.raises(BudgetExhausted):
        await gov.acquire("resy")
    for _ in range(4):  # snipes can still use the rest
        await gov.acquire("resy", urgent=True)
    with pytest.raises(BudgetExhausted):
        await gov.acquire("resy", urgent=True)
    settings.daily_call_budget = 900


def test_fixed_snipe_reserve_and_settings_override():
    from app.safety import daily_budget, monitor_budget
    assert (daily_budget("resy"), monitor_budget("resy")) == (900, 800)
    db.kv_set("budget:resy", 1500)
    assert (daily_budget("resy"), monitor_budget("resy")) == (1500, 1400)


def test_pacing_spreads_remaining_budget_until_midnight(gov):
    from app.safety import awake_seconds_left_today
    one = gov.pacing_interval("resy", 1)
    three = gov.pacing_interval("resy", 3)
    assert three == pytest.approx(one * 3, rel=0.01)
    assert one == pytest.approx(awake_seconds_left_today() / 800, rel=0.05)
    db.usage_incr("resy", n=700)  # most of today's budget gone -> much slower
    assert gov.pacing_interval("resy", 1) > one * 5
    db.usage_incr("resy", n=100)
    assert gov.pacing_interval("resy", 1) == float("inf")


def test_quiet_hours_count_quarter_weight():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.safety import awake_seconds_left_today
    settings.quiet_hours = "01-07"
    tz = ZoneInfo(settings.app_timezone)
    # From 00:00: 1h awake + 6h quiet (=1.5h) + 17h awake = 19.5h
    assert awake_seconds_left_today(datetime(2026, 11, 4, 0, 0, tzinfo=tz)) == pytest.approx(19.5 * 3600)


@pytest.mark.asyncio
async def test_pause_and_resume(gov):
    gov.rate_limited("sevenrooms")
    with pytest.raises(PlatformPaused):
        await gov.acquire("sevenrooms")
    gov.resume("sevenrooms")
    await gov.acquire("sevenrooms")
    assert db.usage_get("sevenrooms") == 1


@pytest.mark.asyncio
async def test_backoff_escalates(gov):
    import time
    a = gov.rate_limited("resy") - time.time()
    gov.resume("resy")
    gov._st("resy").strikes = 2
    b = gov.rate_limited("resy") - time.time()
    assert b > a * 1.5
