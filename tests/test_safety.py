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
