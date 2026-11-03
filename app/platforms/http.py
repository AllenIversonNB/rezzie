"""HTTP transport.

Uses curl_cffi so the TLS/HTTP2 fingerprint matches a real Chrome build, with
ONE long-lived session per platform (cookies + keep-alive, like a browser tab).

Deliberate choices for account safety:
  * The User-Agent is whatever the impersonated browser sends and never
    rotates. Rotating UAs on one logged-in account is a classic bot tell.
  * Every call is counted against a per-platform daily budget and spaced by a
    global minimum gap (see safety.py) — callers go through Governor first.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from ..config import settings

log = logging.getLogger("rezzie.http")


@dataclass
class Response:
    status: int
    text: str
    headers: dict[str, str]
    cookies: dict[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.text) if self.text else {}
        except json.JSONDecodeError:
            return {}


class Transport:
    """Interface — tests substitute a fake."""

    async def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                      params: dict[str, Any] | None = None, data: Any = None, json_body: Any = None,
                      cookies: dict[str, str] | None = None, timeout: float = 10.0,
                      multipart: dict[str, str] | None = None) -> Response:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class CurlTransport(Transport):
    def __init__(self, impersonate: str | None = None, proxy: str | None = None):
        self._impersonate = impersonate or settings.impersonate
        self._proxy = proxy if proxy is not None else (settings.outbound_proxy or None)
        self._session = None
        self._lock = asyncio.Lock()

    async def _get(self):
        async with self._lock:
            if self._session is None:
                from curl_cffi.requests import AsyncSession

                kwargs: dict[str, Any] = {"impersonate": self._impersonate}
                if self._proxy:
                    kwargs["proxy"] = self._proxy
                self._session = AsyncSession(**kwargs)
            return self._session

    async def request(self, method, url, *, headers=None, params=None, data=None, json_body=None,
                      cookies=None, timeout=10.0, multipart=None) -> Response:
        s = await self._get()
        kwargs: dict[str, Any] = {"headers": headers or {}, "timeout": timeout}
        mime = None
        if multipart:
            from curl_cffi import CurlMime

            mime = CurlMime()
            for k, v in multipart.items():
                mime.addpart(name=k, data=str(v).encode())
            kwargs["multipart"] = mime
        if params:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        if json_body is not None:
            kwargs["json"] = json_body
        if cookies:
            kwargs["cookies"] = cookies
        try:
            r = await s.request(method, url, **kwargs)
        finally:
            if mime is not None:
                mime.close()
        try:
            ck = {c.name: c.value for c in r.cookies.jar}  # type: ignore[attr-defined]
        except Exception:
            ck = dict(r.cookies) if r.cookies else {}
        return Response(status=r.status_code, text=r.text, headers=dict(r.headers), cookies=ck)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
