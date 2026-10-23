"""식봄(foodspring.co.kr) 수집기.

품목마다 검색 1회. 두 방식이 있다:
  1순위 "직접" — 식봄 검색 화면이 가격을 받아 올 때 데이터 서버(api.foodspring.co.kr/v2/graphql)에 보내는 것과
     **같은 요청**(같은 질의 글자 foodspring_search.graphql, 같은 변수 keyword·isMobile·searchInput·areaId)을 보낸다.
     3차(2026-10-07 사용자 결정): 화면이 보내는 손님 쿠키(FS_TOKEN 등)를 붙인다 — 화면을 브라우저로 한 번 띄워 받는다(fs_api).
     로그인 계정 쿠키는 쓰지 않고, 화면이 보내지 않는 값은 넣지 않는다. 결과 중 상위 40개만 쓴다.
     근거(2026-10-07 사용자 결정): api.foodspring.co.kr/robots.txt 없음(404), 이용약관 제12조에 자동 수집 금지 조항 없음,
     제9조(시스템 과부하 시 중단 가능) → 1초 간격·사이트당 100회 제한을 지킨다.
  2순위 "화면" — 직접 호출이 오류 응답이거나 형식(상품명·가격·규격)이 기대와 다르면 그 품목만
     실제 브라우저(Playwright 크로미움)로 /search/all?key=<검색어> 를 열고 화면(DOM)의 상품 카드를 읽는다.
     브라우저는 화면 프로그램 파일(/_next/static/)을 받는다 — robots.txt 는 /_next/ 를 막지만 사용자가 이 방식을 쓰기로 정했다.
     robots 가 막은 /_next/data/, /API/ 는 브라우저 안에서도 막는다. 이미지·글꼴·다른 회사 요청도 막는다.
  상품 상세 /goods/detail/<번호> 는 서버 HTML 의 JSON-LD 에 판매가가 있어 일반 요청으로 읽는다(표본 대조용).

파싱 함수(parse_search_html, parse_direct_json, parse_detail_html)는 네트워크와 분리돼 있어
저장한 조각·가짜 응답으로 시험한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from . import units
from .model import (
    STATUS_LOGIN, STATUS_NO_RESULT, ChannelResult, ChannelVerdict, PriceRow, make_row,
)
from .fs_api import GuestSession
from .polite import BudgetExceeded, PoliteClient
from .rules import ItemRule

CHANNEL = "식봄"
BASE = "https://www.foodspring.co.kr"
API_URL = "https://api.foodspring.co.kr/v2/graphql"
TOP_N = 40  # 2차: 첫 화면 결과 전부에서 고르되 최대 40개
AREA_ID = "area_412"  # 로그인 안 한 화면이 보내는 기본 배송지역 값 그대로
QUERY_PATH = Path(__file__).with_name("foodspring_search.graphql")
METHOD_DIRECT = "직접"
METHOD_SCREEN = "화면"


def search_url(query: str) -> str:
    return f"{BASE}/search/all?key={quote(query)}"


def detail_url(goods_id: str | int) -> str:
    return f"{BASE}/goods/detail/{goods_id}"


# ---------------------------------------------------------------- 순수 파싱
def parse_search_html(html: str, limit: int = TOP_N) -> list[dict]:
    """브라우저가 그린 검색 결과 HTML → 상위 limit 개 상품 [{id, name, price, spec, url, login}].

    상품 카드는 <a data-testid="preset-item-<번호>" href="/goods/detail/<번호>?...e_slot=goods_in_search_results...">.
    판매가는 할인 중이면 data-testid="sale-price", 아니면 "original-price" 에 있다.
    광고·기획전 카드(e_slot 이 다른 것)는 빼고 검색 결과 칸만 쓴다.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('a[data-testid^="preset-item-"]')
    in_results = [a for a in cards if "goods_in_search_results" in (a.get("href") or "")]
    if in_results:
        cards = in_results
    out: list[dict] = []
    seen: set[str] = set()
    for a in cards:
        gid = a["data-testid"].removeprefix("preset-item-")
        if gid in seen:
            continue
        seen.add(gid)

        def text(tid: str) -> str:
            el = a.select_one(f'[data-testid="{tid}"]')
            return el.get_text(" ", strip=True) if el else ""

        price_text = text("sale-price") or text("original-price")
        price = units.parse_price(price_text)
        out.append({
            "id": gid,
            "name": text("item-name"),
            "seller": text("seller-name"),
            "price": price,
            "spec": text("standard-unit"),
            "url": detail_url(gid),
            # 가격 자리가 비고 '로그인' 글자가 있으면 로그인해야 가격이 보이는 상품
            "login": price is None and "로그인" in a.get_text(" ", strip=True),
        })
        if len(out) >= limit:
            break
    return out


def parse_detail_html(html: str) -> dict | None:
    """상품 상세 HTML → {id, name, price}. 서버가 보낸 JSON-LD(@type Product)의 offers.price 를 쓴다."""
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            offers = d.get("offers") or {}
            price = offers.get("price")
            return {
                "id": str(d.get("sku") or ""),
                "name": d.get("name") or "",
                "price": int(float(price)) if price not in (None, "") else None,
            }
    return None


# ---------------------------------------------------------------- 직접 호출 (요청 만들기·응답 읽기는 순수 함수)
class DirectError(RuntimeError):
    """직접 호출 실패(오류 응답·기대와 다른 형식). 이 품목은 화면 방식으로 넘어간다."""


def direct_request(keyword: str) -> tuple[dict, dict]:
    """검색 화면이 보내는 것과 같은 (머리글, 본문). 로그인 토큰·쿠키는 넣지 않는다."""
    headers = {
        "accept": "application/graphql-response+json; charset=utf-8, application/json; charset=utf-8",
        "accept-language": "ko-KR",
        "content-type": "application/json",
        "origin": BASE,
        "referer": BASE + "/",
        "x-device-type": "pc",
    }
    body = {
        "query": QUERY_PATH.read_text(encoding="utf-8"),
        "variables": {
            "keyword": keyword,
            "isMobile": False,
            "searchInput": {"keyword": keyword, "sort": "ACCURACY_DESC"},
            "areaId": AREA_ID,
        },
    }
    return headers, body


def parse_direct_json(obj, limit: int = TOP_N) -> list[dict]:
    """데이터 서버 응답 → 상품 [{id, name, price, spec, url, login}]. 형식이 기대와 다르면 DirectError."""
    if not isinstance(obj, dict):
        raise DirectError("응답이 JSON 객체가 아님")
    if obj.get("errors"):
        errs = obj["errors"]
        msg = str(errs[0].get("message", ""))[:80] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else ""
        raise DirectError(f"서버 오류 응답: {msg}")
    try:
        edges = obj["data"]["pcGoodsList"]["edges"]
    except (KeyError, TypeError):
        raise DirectError("data.pcGoodsList.edges 없음") from None
    if not isinstance(edges, list) or not edges:
        raise DirectError("상품 목록이 비었음")
    out = []
    for e in edges[:limit]:
        n = (e or {}).get("node") or {}
        name, std, nid = n.get("name"), n.get("standard"), n.get("nid")
        price = (n.get("price") or {}).get("salePrice")
        if not isinstance(name, str) or not name or not isinstance(price, int) or not isinstance(std, str) or nid is None:
            raise DirectError("상품명·가격·규격 중 빠진 것이 있음")
        unit = n.get("unit")
        out.append({
            "id": str(nid), "name": name, "seller": (n.get("vendor") or {}).get("name") or "",
            "price": price, "spec": f"{std} / {unit}" if unit else std,
            "url": detail_url(nid), "login": False,
        })
    return out


def fetch_direct_guest(keyword: str, session: "GuestSession") -> list[dict]:
    """손님 쿠키를 붙인 직접 호출 1회(3차). 쿠키 만료·거절이면 세션이 한 번만 재발급한다. 실패하면 DirectError."""
    _, body = direct_request(keyword)
    try:
        status, text = session.post(body)
    except BudgetExceeded:
        raise
    except Exception as e:
        raise DirectError(f"요청 실패: {type(e).__name__}") from None
    if status != 200:
        raise DirectError(f"HTTP {status}")
    try:
        obj = json.loads(text)
    except ValueError:
        raise DirectError("JSON 아님") from None
    return parse_direct_json(obj)


def fetch_direct(keyword: str, post: Callable) -> list[dict]:
    """직접 호출 1회. 실패하면 DirectError."""
    headers, body = direct_request(keyword)
    try:
        r = post(API_URL, headers, body)
    except BudgetExceeded:
        raise
    except Exception as e:
        raise DirectError(f"요청 실패: {type(e).__name__}") from None
    if r.status_code != 200:
        raise DirectError(f"HTTP {r.status_code}")
    try:
        obj = r.json()
    except ValueError:
        raise DirectError("JSON 아님") from None
    return parse_direct_json(obj)


# ---------------------------------------------------------------- 화면 방식 (브라우저)
def _route_filter(route) -> None:
    """이미지·글꼴·남의 도메인·robots 금지 경로는 막고 나머지만 통과."""
    req = route.request
    u = urlparse(req.url)
    if req.resource_type in ("image", "font", "media"):
        return route.abort()
    if not u.netloc.endswith("foodspring.co.kr"):
        return route.abort()
    if u.netloc == "www.foodspring.co.kr" and (u.path.startswith("/_next/data/") or u.path.startswith("/API/")):
        return route.abort()
    return route.continue_()


class ScreenFetcher:
    """브라우저를 처음 필요할 때 한 번만 띄우고, 품목마다 검색 화면을 연다."""

    def __init__(self, client: PoliteClient, shot: Path | None = None):
        self.client, self.shot = client, shot
        self._pw = self._browser = self._page = None
        self.statuses: list[int | None] = []

    def __call__(self, keyword: str) -> list[dict]:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page(locale="ko-KR")
            self._page.route("**/*", _route_filter)
        url = search_url(keyword)
        self.client.tick(url)
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        status = resp.status if resp else None
        self.statuses.append(status)
        if status and status >= 400:
            raise RuntimeError(f"HTTP {status}")
        try:
            self._page.wait_for_selector('a[data-testid^="preset-item-"]', timeout=20000)
        except PWTimeout:
            pass  # 결과 없음일 수 있다
        if self.shot is not None and not self.shot.exists():
            self._page.screenshot(path=str(self.shot))
        return parse_search_html(self._page.content(), limit=TOP_N)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._page = None


# ---------------------------------------------------------------- 수집 (직접 → 실패 시 화면)
def fetch_item(keyword: str, direct: Callable[[str], list[dict]] | None,
               screen: Callable[[str], list[dict]]) -> tuple[list[dict], str, str]:
    """한 품목 → (상품들, 수집방식, 직접 호출 실패 사유). 직접이 실패하면 화면으로 되돌아간다."""
    why = ""
    if direct is not None:
        try:
            return direct(keyword), METHOD_DIRECT, ""
        except DirectError as e:
            why = str(e)
    return screen(keyword), METHOD_SCREEN, why


TERMS_LINE = ("약관(2026-10-07 확인): 제12조 금지행위에 자동 수집 금지 조항 없음, 제9조 시스템 과부하 시 서비스 중단 가능"
              " → 1초 간격·사이트당 100회 제한 유지")


def collect(items: list[dict], client: PoliteClient, evidence_dir: Path, echo=print,
            use_direct: bool = True, direct: Callable | None = None,
            screen: Callable | None = None) -> ChannelResult:
    """items: [{품목, 검색어, 필수낱말, 제외낱말, 낱개규격, ...}]. 실패는 상태 칸에 적고 다음 품목으로.

    direct·screen 을 넘기면 그것을 쓴다(시험용 가짜). 안 넘기면 실제 네트워크.
    """
    evidence: list[str] = []
    rows: list[PriceRow] = []
    robots = client.robots(BASE)
    evidence.append(f"GET {robots.url} → {robots.status}")
    if robots.allows(search_url("test")) is False:
        v = ChannelVerdict(CHANNEL, "robots 금지", "robots.txt 가 /search/all 을 금지", evidence)
        return ChannelResult(v, [PriceRow(CHANNEL, it["품목"], "robots 금지") for it in items])

    evidence_dir.mkdir(parents=True, exist_ok=True)
    shot = evidence_dir / "foodspring_search.png"
    own_screen = screen is None
    if own_screen:
        if shot.exists():
            shot.unlink()
        screen = ScreenFetcher(client, shot)
    if use_direct and direct is None:
        session = GuestSession(client)  # 손님 쿠키: 화면을 브라우저로 한 번 띄워 받는다
        direct = lambda kw: fetch_direct_guest(kw, session)  # noqa: E731
    if not use_direct:
        direct = None

    methods: dict[str, str] = {}
    direct_fail: dict[str, str] = {}
    try:
        for it in items:
            rule = ItemRule.from_item(it)
            try:
                cards, method, why = fetch_item(it["검색어"], direct, screen)
            except BudgetExceeded as e:
                rows.append(PriceRow(CHANNEL, it["품목"], f"요청 상한: {e}"))
                continue
            except Exception as e:  # 화면 방식까지 실패 — 한 품목 실패가 전체를 멈추지 않게
                rows.append(PriceRow(CHANNEL, it["품목"], f"오류: {type(e).__name__}", URL=search_url(it["검색어"])))
                continue
            methods[it["품목"]] = method
            if why:
                direct_fail[it["품목"]] = why
            if not cards:
                rows.append(PriceRow(CHANNEL, it["품목"], STATUS_NO_RESULT, URL=search_url(it["검색어"]), 수집방식=method))
                continue
            for c in cards:
                if c["login"]:
                    rows.append(PriceRow(CHANNEL, it["품목"], STATUS_LOGIN, 상품명=c["name"], 규격=c["spec"],
                                         URL=c["url"], 수집방식=method))
                    continue
                rows.append(make_row(CHANNEL, rule, name=c["name"], price=c["price"], spec=c["spec"],
                                     url=c["url"], method=method))
            echo(f"  식봄 {it['품목']}: {len(cards)}개 ({method}{', 직접 실패: ' + why if why else ''})")
    finally:
        if own_screen:
            screen.close()

    n_direct = sum(1 for m in methods.values() if m == METHOD_DIRECT)
    n_screen = sum(1 for m in methods.values() if m == METHOD_SCREEN)
    if use_direct:
        fails = "; ".join(f"{k}: {v}" for k, v in direct_fail.items())
        evidence.append(f"POST {API_URL} (직접) — 성공 {n_direct}품목, 실패→화면 {len(direct_fail)}품목"
                        + (f" ({fails})" if fails else ""))
    if own_screen and screen.statuses:
        evidence.append(f"브라우저 GET {BASE}/search/all?key=… ×{len(screen.statuses)} → "
                        f"{sorted(set(screen.statuses), key=str)}")
    if shot.exists():
        evidence.append(f"화면 캡처: {shot}")
    priced = [r for r in rows if r.가격 is not None]
    logins = [r for r in rows if r.상태 == STATUS_LOGIN]
    method_line = f"수집 방식: 직접 {n_direct}품목 · 화면 {n_screen}품목"
    if priced:
        v = ChannelVerdict(CHANNEL, "가능", f"로그인 없이 판매가를 받는다. {TERMS_LINE}. {method_line}", evidence)
    elif logins:
        v = ChannelVerdict(CHANNEL, "로그인 필요", "검색 결과에 가격 대신 로그인 안내가 보인다", evidence)
    else:
        v = ChannelVerdict(CHANNEL, "오류", "검색 결과에서 가격을 하나도 읽지 못함", evidence)
    res = ChannelResult(v, rows)
    res.methods = methods
    return res


def compare_methods(keywords: dict[str, str], rows: list[PriceRow], screen: Callable[[str], list[dict]],
                    n: int = 3) -> list[dict]:
    """직접으로 받은 품목 n 개를 화면 방식으로 다시 받아 같은 상품(같은 번호)의 가격을 비교한다."""
    out = []
    seen: set[str] = set()
    for r in rows:
        if r.채널 != CHANNEL or r.수집방식 != METHOD_DIRECT or r.가격 is None or r.품목 in seen:
            continue
        seen.add(r.품목)
        gid = r.URL.rsplit("/", 1)[-1]
        try:
            cards = screen(keywords[r.품목])
            match = next((c for c in cards if c["id"] == gid), None)
            sp = match["price"] if match else None
            note = "" if match else "화면 상위 40개에 같은 상품 없음"
        except Exception as e:
            sp, note = None, f"화면 실패: {type(e).__name__}"
        out.append({"품목": r.품목, "상품명": r.상품명, "직접가격": r.가격, "화면가격": sp,
                    "일치": "일치" if sp == r.가격 else ("확인 불가" if sp is None else "불일치"),
                    "비고": note, "URL": r.URL})
        if len(out) >= n:
            break
    return out


def verify_samples(rows: list[PriceRow], client: PoliteClient, n: int = 3) -> list[dict]:
    """품목이 서로 다른 정상 행 n 개를 골라 상품 상세 페이지를 다시 열어 가격을 비교한다."""
    picked: list[PriceRow] = []
    seen: set[str] = set()
    for r in rows:
        if r.채널 == CHANNEL and r.가격 is not None and r.품목 not in seen and "/goods/detail/" in r.URL:
            picked.append(r)
            seen.add(r.품목)
        if len(picked) >= n:
            break
    out = []
    for r in picked:
        detail = None
        status = None
        try:
            resp = client.get(r.URL)
            status = resp.status_code
            if status == 200:
                detail = parse_detail_html(resp.text)
        except Exception as e:  # 대조 실패도 표에 남긴다
            status = type(e).__name__
        dp = detail["price"] if detail else None
        out.append({
            "품목": r.품목, "상품명": r.상품명, "수집가격": r.가격, "상세페이지가격": dp,
            "일치": "일치" if dp == r.가격 else ("확인 불가" if dp is None else "불일치"),
            "HTTP": status, "URL": r.URL,
        })
    return out
