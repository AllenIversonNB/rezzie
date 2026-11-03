import pytest

from app import db
from app.config import settings
from app.safety import BudgetExhausted, PlatformPaused


@pytest.mark.asyncio
async def test_monitor_budget_reserves_headroom_for_snipes(gov):
    settings.daily_call_budget = 8
    for _ in range(6):  # 75% of 8
        await gov.acquire("resy")
    with pytest.raises(BudgetExhausted):
        await gov.acquire("resy")
    await gov.acquire("resy", urgent=True)  # snipes still allowed
    await gov.acquire("resy", urgent=True)
    with pytest.raises(BudgetExhausted):
        await gov.acquire("resy", urgent=True)
    settings.daily_call_budget = 900


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
