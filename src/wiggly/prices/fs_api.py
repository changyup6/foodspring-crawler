"""식봄 데이터 서버(api.foodspring.co.kr) 호출 — 손님 쿠키(3차 변경, 2026-10-07 사용자 결정).

- 손님 쿠키: 로그인하지 않은 방문자에게 식봄이 자동으로 주는 쿠키(FS_TOKEN 등). 식봄 화면을 브라우저로 한 번 띄워,
  그 브라우저가 데이터 서버로 보낼 쿠키를 그대로 받아 쓴다. 로그인 계정 쿠키는 쓰지 않는다.
- 재발급: 서버가 "유효하지 않은 게스트 접근" 으로 거절하거나 쿠키가 만료되면 화면을 다시 띄워 **한 번만** 새로 받는다.
  그 뒤에도 거절되면 오류로 센다(쿠키를 갈아 끼우며 계속 두드리지 않는다).
- 머리글은 검색·카테고리 화면이 보내는 것과 같다(accept, content-type, origin, referer, x-device-type, cookie).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import requests

from .polite import PoliteClient

BASE = "https://www.foodspring.co.kr"
API_URL = "https://api.foodspring.co.kr/v2/graphql"
GUEST_REJECT_MARK = "유효하지 않은 게스트"

PAGE_HEADERS = {
    "accept": "application/graphql-response+json; charset=utf-8, application/json; charset=utf-8",
    "accept-language": "ko-KR",
    "content-type": "application/json",
    "origin": BASE,
    "referer": BASE + "/",
    "x-device-type": "pc",
}


def _route_filter(route) -> None:
    """이미지·글꼴·남의 도메인·robots 금지 경로(/_next/data/, /API/)는 막는다."""
    req = route.request
    u = urlparse(req.url)
    if req.resource_type in ("image", "font", "media"):
        return route.abort()
    if not u.netloc.endswith("foodspring.co.kr"):
        return route.abort()
    if u.netloc == "www.foodspring.co.kr" and (u.path.startswith("/_next/data/") or u.path.startswith("/API/")):
        return route.abort()
    return route.continue_()


def browser_guest_cookies(client: PoliteClient, url: str = BASE + "/") -> list[dict]:
    """식봄 화면을 브라우저로 한 번 띄워, 그 브라우저가 데이터 서버로 보낼 쿠키 목록을 받는다."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(locale="ko-KR")
            page = ctx.new_page()
            page.route("**/*", _route_filter)
            client.tick(url)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            return ctx.cookies(API_URL)
        finally:
            browser.close()


def cookie_header(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def is_guest_rejected(status: int, text: str) -> bool:
    """서버가 손님 쿠키를 거절했나(만료 포함)."""
    return status in (401,) or GUEST_REJECT_MARK in (text or "")


@dataclass
class GuestSession:
    """손님 쿠키를 들고 데이터 서버에 POST 한다. 재발급은 한 실행에 최대 1번."""

    client: PoliteClient
    fetch_cookies: Callable[[], list[dict]] | None = None  # 시험에서는 가짜를 넣는다
    send: Callable[[str, dict, dict], tuple[int, str]] | None = None  # (url, headers, body) → (HTTP 코드, 본문)
    cookies: list[dict] = field(default_factory=list)
    refreshes: int = 0
    max_refreshes: int = 1
    acquired: int = 0

    def __post_init__(self) -> None:
        if self.fetch_cookies is None:
            self.fetch_cookies = lambda: browser_guest_cookies(self.client)
        if self.send is None:
            def _send(url, headers, body):
                self.client.tick(url)
                r = requests.post(url, headers=headers, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                  timeout=self.client.timeout)
                return r.status_code, r.text
            self.send = _send

    def _ensure(self) -> None:
        if not self.cookies:
            self.cookies = list(self.fetch_cookies())
            self.acquired += 1

    def _expired(self) -> bool:
        now = time.time()
        return any(c.get("name") == "FS_TOKEN" and c.get("expires", -1) not in (-1, None) and 0 < c["expires"] < now
                   for c in self.cookies)

    def _refresh(self) -> bool:
        if self.refreshes >= self.max_refreshes:
            return False
        self.refreshes += 1
        self.cookies = list(self.fetch_cookies())
        self.acquired += 1
        return True

    def headers(self) -> dict:
        h = {"User-Agent": self.client.session.headers["User-Agent"], **PAGE_HEADERS}
        if self.cookies:
            h["cookie"] = cookie_header(self.cookies)
        return h

    def post(self, body: dict) -> tuple[int, str]:
        """한 번 보낸다. 만료·거절이면 (아직 안 했으면) 재발급 1번 후 같은 요청을 한 번 더 보낸다."""
        self._ensure()
        if self._expired():
            self._refresh()
        status, text = self.send(API_URL, self.headers(), body)
        if is_guest_rejected(status, text) and self._refresh():
            status, text = self.send(API_URL, self.headers(), body)
        return status, text
