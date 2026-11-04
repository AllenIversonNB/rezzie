"""The Governor: every platform call asks permission here first.

Goals (in priority order):
  1. Never look like a bot hammering an account.
  2. Stop immediately and cool off when a platform pushes back (429 / 403 / WAF).
  3. Keep enough budget in reserve that a scheduled snipe always gets to fire.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .config import settings


class BudgetExhausted(Exception):
    pass


class PlatformPaused(Exception):
    def __init__(self, until: float, reason: str):
        super().__init__(reason)
        self.until = until
        self.reason = reason


@dataclass
class _State:
    next_free: float = 0.0
    hold_until: float = 0.0
    strikes: int = 0


# Calls held back each day for snipes (a snipe uses ~20-40). Monitors get the rest.
SNIPE_RESERVE = 100
BUDGET_MIN, BUDGET_MAX = 200, 3000


def daily_budget(platform: str) -> int:
    """Per-platform daily call ceiling: Settings override, else DAILY_CALL_BUDGET."""
    v = db.kv_get(f"budget:{platform}")
    try:
        return int(v) if v else settings.daily_call_budget
    except (TypeError, ValueError):
        return settings.daily_call_budget


def monitor_budget(platform: str) -> int:
    b = daily_budget(platform)
    return max(b // 2, b - SNIPE_RESERVE)


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.app_timezone))


def awake_seconds_left_today(now: datetime | None = None) -> float:
    """Seconds until local midnight, counting quiet hours at 1/4 weight (monitors run 4x slower then)."""
    now = now or _local_now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    t = now
    while t < midnight:
        nxt = min(midnight, (t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
        span = (nxt - t).total_seconds()
        total += span * (0.25 if in_quiet_hours(t) else 1.0)
        t = nxt
    return max(60.0, total)


def seconds_until_local_midnight() -> float:
    now = _local_now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


class Governor:
    def __init__(self) -> None:
        self._state: dict[str, _State] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _st(self, platform: str) -> _State:
        return self._state.setdefault(platform, _State())

    def _lock(self, platform: str) -> asyncio.Lock:
        return self._locks.setdefault(platform, asyncio.Lock())

    # ------------------------------------------------------------ pausing
    def paused_until(self, platform: str) -> float:
        return float(db.kv_get(f"pause:{platform}", 0) or 0)

    def pause_reason(self, platform: str) -> str:
        return db.kv_get(f"pause_reason:{platform}", "") or ""

    def pause(self, platform: str, seconds: float, reason: str) -> float:
        until = time.time() + seconds
        if until > self.paused_until(platform):
            db.kv_set(f"pause:{platform}", until)
            db.kv_set(f"pause_reason:{platform}", reason)
        return until

    def resume(self, platform: str) -> None:
        db.kv_delete(f"pause:{platform}")
        db.kv_delete(f"pause_reason:{platform}")
        self._st(platform).strikes = 0

    # ------------------------------------------------------------- acquire
    async def acquire(self, platform: str, *, urgent: bool = False) -> None:
        """Wait for permission to make one call. Raises if paused/out of budget."""
        pu = self.paused_until(platform)
        if pu > time.time():
            raise PlatformPaused(pu, self.pause_reason(platform))

        used = db.usage_get(platform)
        cap = daily_budget(platform) if urgent else monitor_budget(platform)
        if used >= cap:
            raise BudgetExhausted(f"{platform}: daily call budget reached ({used}/{cap})")

        # Reserve a time slot under the lock, but sleep OUTSIDE it, so an urgent
        # snipe call is never stuck behind a monitor waiting out its gap.
        async with self._lock(platform):
            st = self._st(platform)
            now = time.monotonic()
            if urgent:
                start = now
                st.next_free = max(st.next_free, now + settings.min_gap_seconds)
            else:
                start = max(now, st.next_free, st.hold_until)
                st.next_free = start + settings.min_gap_seconds * random.uniform(1.0, 1.6)
        if start > now:
            await asyncio.sleep(start - now)
        db.usage_incr(platform)

    def held_for(self, platform: str) -> float:
        return max(0.0, self._st(platform).hold_until - time.monotonic())

    def hold(self, platform: str, seconds: float) -> None:
        """Keep background monitors off a platform (e.g. around a snipe burst)."""
        st = self._st(platform)
        st.hold_until = max(st.hold_until, time.monotonic() + seconds)

    def pacing_interval(self, platform: str, calls_per_round: float) -> float:
        """Smallest awake-hours interval at which all monitors on this platform can
        keep checking until midnight without running out of budget.

        calls_per_round: total calls one check of every active monitor costs.
        """
        remaining = monitor_budget(platform) - db.usage_get(platform)
        if remaining <= 0:
            return float("inf")
        return awake_seconds_left_today() * max(1.0, calls_per_round) / remaining

    # -------------------------------------------------------------- report
    def ok(self, platform: str) -> None:
        self._st(platform).strikes = 0

    def rate_limited(self, platform: str) -> float:
        st = self._st(platform)
        st.strikes += 1
        # 5m, 10m, 20m, 40m ... capped at 3h
        secs = min(300 * 2 ** (st.strikes - 1), 3 * 3600) * random.uniform(1.0, 1.3)
        return self.pause(platform, secs, f"Rate limited (429), strike {st.strikes}")

    def blocked(self, platform: str, detail: str = "") -> float:
        st = self._st(platform)
        st.strikes += 1
        # Blocks are more serious than 429s: 30m, 1h, 2h ... capped at 12h.
        secs = min(1800 * 2 ** (st.strikes - 1), 12 * 3600) * random.uniform(1.0, 1.3)
        return self.pause(platform, secs, f"Blocked/challenged ({detail or 'WAF'}), strike {st.strikes}")


governor = Governor()


def jitter(seconds: float, pct: float = 0.25) -> float:
    return max(1.0, seconds * random.uniform(1 - pct, 1 + pct))


def in_quiet_hours(now: datetime | None = None) -> bool:
    rng = settings.quiet_range()
    if not rng:
        return False
    tz = ZoneInfo(settings.app_timezone)
    h = (now or datetime.now(tz)).astimezone(tz).hour
    a, b = rng
    return a <= h < b if a <= b else (h >= a or h < b)


def effective_poll_interval(requested: int | None) -> float:
    base = max(settings.min_poll_seconds, int(requested or 0) or 120)
    if in_quiet_hours():
        base *= 4
    return jitter(base)
