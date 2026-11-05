import asyncio
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.engine import Engine
from app.platforms.base import Booking, BookingUnclear, NetworkError, Slot
from app.platforms.http import CurlTransport
from app.safety import PlatformPaused


def silent_server():
    """Accepts connections, reads, never answers: what a dead/stale path looks like."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    conns = []

    def loop():
        while True:
            try:
                c, _ = srv.accept()
                conns.append(c)
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


@pytest.mark.asyncio
async def test_real_timeout_becomes_friendly_network_error(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    srv, port = silent_server()
    t = CurlTransport(proxy="")
    with pytest.raises(NetworkError, match=r"No response from 127.0.0.1 within 1s"):
        await t.request("GET", f"http://127.0.0.1:{port}/x", timeout=1)
    srv.close()
    await t.close()


class FlakySession:
    def __init__(self, fails):
        self.fails = fails
        self.calls = 0

    async def request(self, method, url, **kw):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("curl: (28) Operation timed out after 10000 milliseconds with 0 bytes received")

        class R:
            status_code = 200
            text = "{}"
            headers = {}
            cookies = {}
        return R()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_get_retried_once_on_fresh_connection_post_never():
    t = CurlTransport(proxy="")
    sessions = []

    async def fake_get():
        if t._session is None:
            t._session = FlakySession(fails=1 if not sessions else 0)
            sessions.append(t._session)
        return t._session

    t._get = fake_get
    r = await t.request("GET", "https://www.sevenrooms.com/x")
    assert r.status == 200 and len(sessions) == 2           # stale pool dropped, fresh one used
    t._session = None
    sessions.clear()
    t._get = fake_get
    with pytest.raises(NetworkError):
        await t.request("POST", "https://www.sevenrooms.com/book")
    assert len(sessions) == 1 and sessions[0].calls == 1    # writes are never repeated


def test_repeated_background_timeouts_pause_platform(gov):
    for _ in range(3):
        assert gov.network_error("sevenrooms") is None
    assert gov.network_error("sevenrooms", urgent=True) is None   # snipes never trip it
    assert gov.network_error("sevenrooms") is not None
    assert gov.paused_until("sevenrooms") > datetime.now().timestamp() + 800


# ---- engine behaviour ------------------------------------------------------------

from test_engine import FakeResy, base_req, slot  # noqa: E402


@pytest.mark.asyncio
async def test_snipe_survives_timeout_and_odd_errors():
    from app.config import settings
    settings.snipe_min_interval_ms = 10
    settings.snipe_max_interval_ms = 20
    r = base_req(release_at=(datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat())
    resy = FakeResy([NetworkError("No response from api.resy.com within 10s"), RuntimeError("weird"),
                     [slot("19:30")]])
    await Engine(resy=resy)._run_timed(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "booked" and resy.finds == 3
    msgs = [e["message"] for e in db.list_events(r["id"])]
    assert any("No response from api.resy.com" in m and "retrying" in m for m in msgs)


@pytest.mark.asyncio
async def test_unanswered_booking_is_not_retried():
    from app.config import settings
    settings.snipe_min_interval_ms = 10
    settings.snipe_max_interval_ms = 20
    r = base_req(release_at=(datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat())
    resy = FakeResy([[slot("19:30"), slot("20:00")]],
                    [BookingUnclear("didn't answer; may have gone through"), Booking("X")])
    await Engine(resy=resy)._run_timed(r["id"])
    out = db.get_request(r["id"])
    assert out["status"] == "found" and len(resy.booked) == 1   # no second booking attempt
    assert "may have gone through" in out["last_error"]


@pytest.mark.asyncio
async def test_monitor_unexpected_error_keeps_normal_cadence():
    from app.config import settings
    settings.min_poll_seconds = 60
    r = base_req(mode="monitor", status="monitoring", release_at=None)
    await Engine(resy=FakeResy([RuntimeError("boom")])).check_monitor(r["id"])
    out = db.get_request(r["id"])
    nxt = datetime.fromisoformat(out["next_check_at"])
    assert nxt - datetime.now(timezone.utc) > timedelta(seconds=40)
    assert out["status"] == "monitoring" and "boom" in out["last_error"]
