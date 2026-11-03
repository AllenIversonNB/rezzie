"""Pick the best slot for a request."""
from __future__ import annotations

from typing import Any, Iterable

from .platforms.base import Slot


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def matching_slots(req: dict[str, Any], slots: Iterable[Slot]) -> list[Slot]:
    dates: list[str] = req.get("dates") or []
    lo, hi = _mins(req["time_start"]), _mins(req["time_end"])
    if hi < lo:  # window crosses midnight, e.g. 22:00-00:30
        hi += 24 * 60
    ideal = _mins(req.get("ideal_time") or req["time_start"])
    excludes = [e.strip().lower() for e in (req.get("seating_exclude") or []) if e.strip()]

    picked = []
    for s in slots:
        if dates and s.date not in dates:
            continue
        t = _mins(s.time)
        if t < lo and hi >= 24 * 60:
            t += 24 * 60
        if not (lo <= t <= hi):
            continue
        if excludes and any(e in (s.seating or "").lower() for e in excludes):
            continue
        picked.append(s)

    date_rank = {d: i for i, d in enumerate(dates)}
    picked.sort(key=lambda s: (date_rank.get(s.date, 99), abs(_mins(s.time) - ideal), _mins(s.time)))
    return picked
