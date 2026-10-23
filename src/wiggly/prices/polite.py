"""요청 예의 지키기: 같은 사이트 요청 사이 최소 1초, 한 번 실행에 사이트당 100회 이하, robots.txt 확인.

- User-Agent 는 평범한 데스크톱 크롬 문자열 하나를 그대로 쓴다(바꿔 가며 위장하지 않는다).
- 브라우저(Playwright)로 연 페이지도 "탐색 1회" 를 요청 1회로 센다. 페이지가 스스로 불러오는 부속 파일은
  세지 않지만, 이미지·글꼴·다른 회사 추적 스크립트는 막아서 부하를 줄인다(각 수집기 참고).
"""
from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
MIN_INTERVAL_S = 1.0
MAX_REQUESTS_PER_SITE = 100


class BudgetExceeded(RuntimeError):
    """사이트당 요청 상한(100회)을 넘기려 할 때."""


@dataclass
class Robots:
    """robots.txt 읽은 결과. status 가 200 이 아니면 parser 는 None."""

    url: str
    status: int | None
    parser: urllib.robotparser.RobotFileParser | None
    note: str = ""

    def allows(self, url: str) -> bool | None:
        """허용이면 True, 금지면 False, 못 읽어서 모르면 None."""
        if self.parser is None:
            return None
        return self.parser.can_fetch("*", url)


def parse_robots(text: str, url: str = "") -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser(url)
    rp.parse(text.splitlines())
    return rp


@dataclass
class PoliteClient:
    """사이트(호스트)별 간격·횟수를 지키는 요청기. 브라우저 탐색도 tick() 으로 같이 센다."""

    min_interval: float = MIN_INTERVAL_S
    max_per_site: int = MAX_REQUESTS_PER_SITE
    timeout: float = 20.0
    _last: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.headers["Accept-Language"] = "ko-KR,ko;q=0.9"

    def tick(self, url: str) -> None:
        """요청 직전에 부른다: 간격을 기다리고 횟수를 센다."""
        host = urlparse(url).netloc
        n = self.counts.get(host, 0)
        if n >= self.max_per_site:
            raise BudgetExceeded(f"{host} 요청 {n}회 — 상한 {self.max_per_site}회")
        wait = self.min_interval - (time.monotonic() - self._last.get(host, -1e9))
        if wait > 0:
            time.sleep(wait)
        self.counts[host] = n + 1
        self._last[host] = time.monotonic()

    def get(self, url: str, **kw) -> requests.Response:
        self.tick(url)
        kw.setdefault("timeout", self.timeout)
        return self.session.get(url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        self.tick(url)
        kw.setdefault("timeout", self.timeout)
        return self.session.post(url, **kw)

    def robots(self, base: str) -> Robots:
        url = base.rstrip("/") + "/robots.txt"
        try:
            r = self.get(url)
        except requests.RequestException as e:
            return Robots(url, None, None, f"요청 실패: {type(e).__name__}")
        if r.status_code != 200:
            return Robots(url, r.status_code, None, f"HTTP {r.status_code}")
        return Robots(url, 200, parse_robots(r.text, url))
