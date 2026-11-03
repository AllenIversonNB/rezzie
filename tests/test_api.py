from fastapi.testclient import TestClient

from app.main import app


def client():
    c = TestClient(app)
    return c


def test_requires_login():
    with client() as c:
        assert c.get("/api/requests").status_code == 401
        assert c.get("/api/me").json() == {"authed": False, "configured": True}
        assert c.post("/api/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/login", json={"password": "pw"}).status_code == 200
        assert c.get("/api/me").json()["authed"] is True


def test_static_front_end_served():
    with client() as c:
        r = c.get("/")
        assert r.status_code == 200 and "Rezzie" in r.text
        assert c.get("/app.js").status_code == 200
        assert c.get("/healthz").json() == {"ok": True}


def _login(c):
    assert c.post("/api/login", json={"password": "pw"}).status_code == 200


BODY = {"platform": "resy", "mode": "snipe", "venue_id": "834", "venue_name": "Carbone", "venue_slug": "carbone",
        "venue_city": "new-york-ny", "venue_tz": "America/New_York", "party_size": 2,
        "dates": ["2099-02-01"], "time_start": "18:00", "time_end": "21:00", "ideal_time": "19:30",
        "release_local": "2099-01-18T10:00"}


def test_create_snipe_converts_release_to_utc():
    with client() as c:
        _login(c)
        r = c.post("/api/requests", json=BODY)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "scheduled"
        assert d["release_at"] == "2099-01-18T15:00:00+00:00"  # 10:00 EST
        assert [x["id"] for x in c.get("/api/requests").json()] == [d["id"]]
        detail = c.get(f"/api/requests/{d['id']}").json()
        assert detail["events"][0]["message"].startswith("Created snipe")


def test_past_release_rejected_and_opentable_forced_to_reminder():
    with client() as c:
        _login(c)
        r = c.post("/api/requests", json={**BODY, "release_local": "2001-01-01T10:00"})
        assert r.status_code == 400
        r = c.post("/api/requests", json={**BODY, "platform": "opentable", "mode": "monitor", "auto_book": True})
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "reminder" and r.json()["auto_book"] is False


def test_monitor_cap_and_poll_floor():
    from app.config import settings
    settings.max_active_monitors = 1
    with client() as c:
        _login(c)
        body = {**BODY, "mode": "monitor", "release_local": None, "poll_interval_seconds": 5}
        r = c.post("/api/requests", json=body)
        assert r.status_code == 200 and r.json()["poll_interval_seconds"] == settings.min_poll_seconds
        assert c.post("/api/requests", json=body).status_code == 400
    settings.max_active_monitors = 8


def test_cancel_and_history():
    with client() as c:
        _login(c)
        rid = c.post("/api/requests", json=BODY).json()["id"]
        assert c.post(f"/api/requests/{rid}/cancel").json()["status"] == "cancelled"
        assert c.get("/api/requests").json() == []
        assert c.get("/api/requests?scope=history").json()[0]["id"] == rid


def test_status_endpoint():
    with client() as c:
        _login(c)
        s = c.get("/api/status").json()
        assert s["platforms"]["resy"]["budget"] > 0
        assert s["platforms"]["resy"]["auth"]["has_credentials"] is True


def test_busy_guard():
    from datetime import datetime, timedelta, timezone
    from app import db
    with client() as c:
        assert c.get("/api/busy").json() == {"busy": False}
        _login(c)
        rid = c.post("/api/requests", json=BODY).json()["id"]
        assert c.get("/api/busy").json() == {"busy": False}  # release is far away
        soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")
        db.update_request(rid, release_at=soon)
        assert c.get("/api/busy").json() == {"busy": True}
        db.update_request(rid, status="attempting")
        assert c.get("/api/busy").json() == {"busy": True}
