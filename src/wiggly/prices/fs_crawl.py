"""식봄 전체 상품 원본 수집(3차 변경, 2026-10-07 사용자 결정). 실행은 scripts/crawl_foodspring_full.py.

- 카테고리 목록: www.foodspring.co.kr/sitemap/categories-0.xml (검색엔진용 공개 목록).
- 카테고리마다 카테고리 화면의 "더보기" 가 데이터 서버에 보내는 것과 같은 요청
  (CategoryGoodsDetailListPCConnectionPaginationQuery, 변수 after·first·nid·sort)을 보낸다.
  바꾸는 값은 카테고리 번호(nid)와 페이지 위치(after)뿐. 한 번에 받는 개수(first=80)·정렬(POPULAR_DESC)은 화면 값 그대로.
  페이지 위치는 앞 응답의 pageInfo.endCursor 를 그대로 다음 after 로 쓴다(화면과 같은 방식). 첫 페이지는 after=null.
- 빈 페이지(상품 0개)가 오면 그 카테고리를 끝낸다.
  안전장치(계약 밖, 보고함): 서버는 한 목록에서 앞 10,000개까지만 주고 그 뒤는 마지막 페이지를 되풀이한다 —
  직전 페이지와 상품번호가 똑같은 페이지가 오면 그 카테고리를 끝낸다(capped). 10,000개를 넘는 상위 카테고리의 나머지는
  하위 카테고리로 받는다.
- 저장: data/raw/foodspring_full/YYYYMMDD/
    responses.jsonl.gz — 요청마다 한 줄 {category, page, time, status, response(서버 응답 글자 그대로)}
    products.csv       — 상품번호 기준 한 줄
    progress.json      — 이어하기용(카테고리별 다음 페이지·커서·끝남 여부)
- 멈춤: 실행 한 번에 요청 8,000회 상한. 오류(403·429·5xx·서버 오류 메시지·연결 실패) 10번 연속이면 멈추고 저장.
  오류가 난 페이지는 다시 두드리지 않고 그 카테고리를 미완료로 남긴 채 다음 카테고리로 간다(다음 실행에서 이어받음).
"""
from __future__ import annotations

import csv
import gzip
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

CATEGORY_SITEMAP = "https://www.foodspring.co.kr/sitemap/categories-0.xml"
SITEMAP_INDEX = "https://www.foodspring.co.kr/sitemap/sitemap-index.xml"
QUERY_PATH = Path(__file__).with_name("foodspring_category.graphql")
PAGE_SIZE = 80          # 카테고리 화면이 보내는 first 값
SORT = "POPULAR_DESC"   # 카테고리 화면 기본 정렬
MAX_REQUESTS = 8000
MAX_CONSECUTIVE_ERRORS = 10
PRODUCT_COLUMNS = ["상품번호", "상품명", "판매가", "원래가격", "규격", "판매자", "카테고리", "URL", "수집시각"]


# ---------------------------------------------------------------- 순수 함수
def parse_category_sitemap(xml: str) -> list[int]:
    """categories-0.xml → 카테고리 번호 목록(나온 순서, 중복 제거)."""
    out, seen = [], set()
    for m in re.finditer(r"<loc>\s*[^<]*/category/goods_detail/(\d+)\s*</loc>", xml):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def parse_item_sitemap_ids(xml: str) -> set[str]:
    """items-*.xml → 상품 번호 집합."""
    return set(re.findall(r"/goods/detail/(\d+)", xml))


def category_body(nid: int, after: str | None) -> dict:
    """카테고리 화면의 '더보기' 요청과 같은 본문. 바꾸는 값은 nid 와 after 뿐."""
    return {
        "query": QUERY_PATH.read_text(encoding="utf-8"),
        "variables": {"after": after, "first": PAGE_SIZE, "nid": nid, "sort": SORT},
    }


class PageError(RuntimeError):
    """오류 응답 또는 기대와 다른 형식."""


def is_error_status(status: int | None) -> bool:
    """403·429·5xx 를 포함해 4xx·5xx 전부와 응답 없음(연결 실패)은 오류."""
    return status is None or status >= 400


def parse_category_page(text: str) -> tuple[list[dict], str | None, bool]:
    """응답 글자 → (상품들, 다음 커서, hasNextPage). 서버 오류 메시지·형식 이상이면 PageError."""
    try:
        obj = json.loads(text)
    except ValueError:
        raise PageError("JSON 아님") from None
    if not isinstance(obj, dict):
        raise PageError("JSON 객체 아님")
    if obj.get("errors"):
        errs = obj["errors"]
        msg = errs[0].get("message", "") if isinstance(errs, list) and errs and isinstance(errs[0], dict) else ""
        raise PageError(f"서버 오류 메시지: {str(msg)[:80]}")
    try:
        conn = obj["data"]["goodsCategory"]["goodsListPC"]
        edges = conn["edges"]
    except (KeyError, TypeError):
        raise PageError("data.goodsCategory.goodsListPC.edges 없음") from None
    if not isinstance(edges, list):
        raise PageError("edges 가 목록이 아님")
    info = conn.get("pageInfo") or {}
    products = []
    for e in edges:
        n = (e or {}).get("node") or {}
        if n.get("nid") is None:
            raise PageError("상품 번호 없음")
        price = n.get("price") or {}
        std, unit = n.get("standard") or "", n.get("unit") or ""
        products.append({
            "상품번호": str(n["nid"]),
            "상품명": n.get("name") or "",
            "판매가": price.get("salePrice"),
            "원래가격": price.get("originalPrice"),
            "규격": f"{std} / {unit}" if std and unit else (std or unit),
            "판매자": (n.get("vendor") or {}).get("name") or "",
        })
    return products, info.get("endCursor"), bool(info.get("hasNextPage"))


# ---------------------------------------------------------------- 저장소(이어하기)
@dataclass
class Store:
    folder: Path
    progress: dict = field(default_factory=dict)  # {"<nid>": {"page": int, "after": str|None, "done": bool}}
    seen: set = field(default_factory=set)

    @property
    def raw_path(self) -> Path:
        return self.folder / "responses.jsonl.gz"

    @property
    def csv_path(self) -> Path:
        return self.folder / "products.csv"

    @property
    def progress_path(self) -> Path:
        return self.folder / "progress.json"

    @classmethod
    def open(cls, folder: Path) -> "Store":
        folder.mkdir(parents=True, exist_ok=True)
        st = cls(folder)
        if st.progress_path.exists():
            st.progress = json.loads(st.progress_path.read_text(encoding="utf-8"))
        if st.csv_path.exists():
            with open(st.csv_path, encoding="utf-8-sig", newline="") as f:
                st.seen = {row["상품번호"] for row in csv.DictReader(f)}
        return st

    def state(self, nid: int) -> dict:
        return self.progress.get(str(nid), {"page": 1, "after": None, "done": False})

    def set_state(self, nid: int, **kw) -> None:
        s = self.state(nid)
        s.update(kw)
        self.progress[str(nid)] = s

    def save_progress(self) -> None:
        tmp = self.progress_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.progress, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.progress_path)

    def append_raw(self, record: dict) -> None:
        with gzip.open(self.raw_path, "at", encoding="utf-8") as f:  # 이어 쓰기(gzip 여러 덩어리 — gzip 모듈로 그대로 읽힌다)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_products(self, rows: list[dict]) -> int:
        """새 상품번호만 추가. 추가한 수."""
        new = [r for r in rows if r["상품번호"] not in self.seen]
        if not new:
            return 0
        exists = self.csv_path.exists()
        with open(self.csv_path, "a", encoding="utf-8-sig" if not exists else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=PRODUCT_COLUMNS)
            if not exists:
                w.writeheader()
            for r in new:
                w.writerow(r)
                self.seen.add(r["상품번호"])
        return len(new)


# ---------------------------------------------------------------- 수집기
@dataclass
class CrawlResult:
    categories_done: int = 0
    categories_touched: int = 0
    requests: int = 0
    errors: int = 0
    pages: int = 0
    new_products: int = 0
    stopped: str = ""   # 빈칸이면 완료
    capped: list = field(default_factory=list)  # 같은 페이지 되풀이(10,000개 상한)로 끝낸 카테고리
    seconds: float = 0.0


def crawl(categories: list[int], store: Store, post: Callable[[dict], tuple[int, str]], *,
          max_requests: int = MAX_REQUESTS, max_consecutive_errors: int = MAX_CONSECUTIVE_ERRORS,
          now: Callable[[], str] = lambda: datetime.now().isoformat(timespec="seconds"),
          echo=print) -> CrawlResult:
    """categories 를 차례로 끝까지 받는다. post(body) → (HTTP 코드, 응답 글자). 네트워크는 post 안에만 있다."""
    res = CrawlResult()
    t0 = time.monotonic()
    consecutive = 0
    try:
        for nid in categories:
            st = store.state(nid)
            if st.get("done"):
                continue
            res.categories_touched += 1
            page, after = st["page"], st["after"]
            prev_ids: tuple | None = None
            while True:
                if res.requests >= max_requests:
                    res.stopped = f"요청 상한 {max_requests}회에 닿음"
                    return res
                body = category_body(nid, after)
                try:
                    status, text = post(body)
                except Exception as e:  # 연결 실패도 오류로 센다
                    status, text = None, f"{type(e).__name__}: {e}"
                res.requests += 1
                store.append_raw({"category": nid, "page": page, "time": now(), "status": status, "response": text})
                err = None
                if is_error_status(status):
                    err = f"HTTP {status}"
                else:
                    try:
                        products, cursor, _has_next = parse_category_page(text)
                    except PageError as e:
                        err = str(e)
                if err:
                    res.errors += 1
                    consecutive += 1
                    echo(f"  오류 cat={nid} page={page}: {err} (연속 {consecutive})")
                    store.save_progress()
                    if consecutive >= max_consecutive_errors:
                        res.stopped = f"오류 {consecutive}번 연속(마지막: {err})"
                        return res
                    break  # 같은 페이지를 다시 두드리지 않는다 — 이 카테고리는 미완료로 두고 다음으로
                consecutive = 0
                res.pages += 1
                if not products:  # 빈 페이지 → 이 카테고리 끝
                    store.set_state(nid, page=page, after=after, done=True)
                    store.save_progress()
                    res.categories_done += 1
                    break
                ids = tuple(p["상품번호"] for p in products)
                if ids == prev_ids:
                    # 안전장치: 데이터 서버는 한 목록에서 앞 10,000개(125쪽)까지만 주고, 그 뒤로는 빈 페이지 대신
                    # 같은 마지막 페이지를 되풀이한다(2026-10-07 시험 수집에서 확인). 직전 페이지와 똑같으면 끝으로 본다.
                    store.set_state(nid, page=page, after=after, done=True, capped=True)
                    store.save_progress()
                    res.categories_done += 1
                    res.capped.append(nid)
                    echo(f"  cat={nid}: page {page} 가 직전 페이지와 같음(10,000개 상한) — 끝냄")
                    break
                prev_ids = ids
                stamp = now()
                for p in products:
                    p["카테고리"] = nid
                    p["URL"] = f"https://www.foodspring.co.kr/goods/detail/{p['상품번호']}"
                    p["수집시각"] = stamp
                res.new_products += store.append_products(products)
                if not cursor:  # 다음 위치가 없으면 더 받을 수 없다 — 끝으로 본다
                    store.set_state(nid, page=page + 1, after=None, done=True)
                    store.save_progress()
                    res.categories_done += 1
                    echo(f"  cat={nid}: 커서 없음 — page {page} 에서 끝냄")
                    break
                page, after = page + 1, cursor
                store.set_state(nid, page=page, after=after, done=False)
                store.save_progress()
        return res
    finally:
        store.save_progress()
        res.seconds = time.monotonic() - t0
