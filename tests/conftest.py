import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENGINE_DISABLED", "1")

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.platforms.http import Response, Transport  # noqa: E402
from app.safety import Governor  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.reset_for_tests(str(tmp_path))
    settings.secret_key = "test-secret"
    settings.app_password = "pw"
    settings.cookie_secure = False
    settings.resy_email = "me@example.com"
    settings.resy_password = "hunter2"
    settings.min_gap_seconds = 0
    settings.dry_run = False
    settings.ntfy_topic = ""
    from app.safety import governor
    governor._state.clear()
    yield
    db.reset_for_tests(str(tmp_path))


class FakeTransport(Transport):
    """Routes (METHOD, path-without-host) -> list of Responses (popped in order; last one repeats)."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def add(self, method, path, status=200, body=None, cookies=None, text=None):
        import json
        r = Response(status=status, text=text if text is not None else json.dumps(body or {}),
                     headers={}, cookies=cookies or {})
        self.routes.setdefault((method, path), []).append(r)

    async def request(self, method, url, *, headers=None, params=None, data=None, json_body=None,
                      cookies=None, timeout=10.0, multipart=None):
        from urllib.parse import urlparse
        path = urlparse(url).path
        self.calls.append({"method": method, "path": path, "headers": headers or {}, "params": params,
                           "data": data, "json": json_body, "cookies": cookies,
                           "multipart": multipart})
        q = self.routes.get((method, path))
        if not q:
            return Response(404, "{}", {}, {})
        return q.pop(0) if len(q) > 1 else q[0]


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def gov():
    return Governor()


def make_jwt(exp):
    import base64
    import json
    p = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{p}.s"
