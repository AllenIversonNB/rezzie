"""Shared types for platform integrations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Slot:
    platform: str
    date: str               # YYYY-MM-DD
    time: str               # HH:MM (venue local)
    seating: str = ""       # e.g. "Dining Room", "Bar", "Patio"
    token: str = ""         # platform booking handle (Resy config token)
    bookable: bool = True   # False = request-only / needs human
    link: str = ""          # deep link for a human to finish
    raw: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "date": self.date,
            "time": self.time,
            "seating": self.seating,
            "bookable": self.bookable,
            "link": self.link,
        }


@dataclass
class Venue:
    platform: str
    id: str
    name: str
    slug: str = ""
    city: str = ""
    neighborhood: str = ""
    timezone: str = ""
    url: str = ""
    lead_time_days: int | None = None
    cuisine: str = ""
    price: int | None = None

    def public(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Booking:
    confirmation: str
    token: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PlatformError(Exception):
    """Generic failure talking to a platform."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:500]


class AuthError(PlatformError):
    """Token invalid/expired (Resy answers 401/419)."""


class RateLimited(PlatformError):
    """429 — stop and back off."""


class Blocked(PlatformError):
    """403 / WAF 5xx / challenge page — stop hard, this is how accounts get flagged."""


class SlotGone(PlatformError):
    """Slot was taken between find and book (404/409/412)."""


class PaymentRequired(PlatformError):
    """Venue needs a card on file / deposit (402)."""


class NetworkError(PlatformError):
    """No usable response: timeout, connection reset, DNS... Retryable."""


class BookingUnclear(PaymentRequired):
    """The final booking request got no answer: it may or may not have gone
    through. Never retry automatically (that risks a double booking); a human
    checks email / the app and finishes if needed."""
