"""식봄 전체 상품 원본 수집(3차): python scripts/crawl_foodspring_full.py [--limit-categories N] [--date YYYYMMDD]
                                       [--check-sitemap] [--show-files]

쓰는 범위: 팀 내부 분석만. 원본은 data/raw/ 아래(git 제외)에만 두고 팀 밖으로 내보내지 않는다. 발표에는 집계 숫자만.

- 기본: 카테고리 목록(sitemap/categories-0.xml)의 카테고리마다 끝 페이지(빈 페이지)까지 받는다. 끊겼다가 다시 돌리면 이어서 받는다.
- --limit-categories N : 아직 안 끝난 카테고리 중 앞의 N 개만(시험용).
- --date YYYYMMDD      : 이어받을 저장 폴더 날짜(기본 오늘). 자정을 넘겨 이어받을 때 쓴다.
- --check-sitemap      : 상품 주소 목록(sitemap/items-*.xml)을 1초에 1개씩 받아 수집한 상품번호와 대조한 네 숫자를 찍는다.
- --show-files         : 두 저장 파일 크기, products.csv 앞 5줄, 상품번호 중복 수를 찍는다.
마지막 줄은 "완료" 또는 "멈춤: <사유>".
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiggly.prices import fs_crawl  # noqa: E402
from wiggly.prices.fs_api import GuestSession  # noqa: E402
from wiggly.prices.polite import PoliteClient  # noqa: E402


def out_folder(root: Path, date: str | None) -> Path:
    return root / "data" / "raw" / "foodspring_full" / (date or datetime.now().strftime("%Y%m%d"))


def show_files(folder: Path) -> str:
    st = fs_crawl.Store(folder)
    lines = [f"폴더: {folder}"]
    for p in (st.raw_path, st.csv_path):
        lines.append(f"{p.name}: {p.stat().st_size:,} 바이트" if p.exists() else f"{p.name}: 없음")
    if st.csv_path.exists():
        with open(st.csv_path, encoding="utf-8-sig", newline="") as f:
            text = f.read().splitlines()
        lines.append("products.csv 앞 5줄:")
        lines += ["  " + t for t in text[:5]]
        with open(st.csv_path, encoding="utf-8-sig", newline="") as f:
            ids = [r["상품번호"] for r in csv.DictReader(f)]
        dup = sum(c - 1 for c in Counter(ids).values() if c > 1)
        lines.append(f"상품 수 {len(ids):,} · 상품번호 중복 {dup}")
    return "\n".join(lines)


def check_sitemap(folder: Path, client: PoliteClient) -> str:
    idx = client.get(fs_crawl.SITEMAP_INDEX)
    files = [u for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", idx.text) if "/items-" in u]
    sm: set[str] = set()
    for u in files:
        r = client.get(u, timeout=60)
        sm |= fs_crawl.parse_item_sitemap_ids(r.text)
    st = fs_crawl.Store.open(folder)
    got = st.seen
    both = sm & got
    pct = (len(both) / len(sm) * 100) if sm else 0.0
    return (f"상품 주소 목록 파일 {len(files)}개\n상품 주소 목록 수: {len(sm):,}\n수집 수: {len(got):,}\n"
            f"겹친 수: {len(both):,}\n겹친 비율: {pct:.1f}%")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="식봄 전체 상품 원본 수집")
    ap.add_argument("--limit-categories", type=int, default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--check-sitemap", action="store_true")
    ap.add_argument("--show-files", action="store_true")
    a = ap.parse_args(argv)

    folder = out_folder(Path(a.root), a.date)
    # 데이터 서버·본 사이트 각각 1초에 1번 이하. 전체 수집은 '사이트당 100회' 대신 실행당 8,000회(수집기에서 센다).
    client = PoliteClient(max_per_site=10**9, timeout=60)

    if a.show_files:
        print(show_files(folder))
        return 0
    if a.check_sitemap:
        print(check_sitemap(folder, client))
        return 0

    started = datetime.now()
    print(f"식봄 전체 수집 — {started.isoformat(timespec='seconds')} 시작, 저장: {folder}")
    r = client.get(fs_crawl.CATEGORY_SITEMAP)
    if r.status_code != 200:
        print(f"멈춤: 카테고리 목록 HTTP {r.status_code}")
        return 1
    cats = fs_crawl.parse_category_sitemap(r.text)
    store = fs_crawl.Store.open(folder)
    todo = [c for c in cats if not store.state(c).get("done")]
    print(f"카테고리 목록 {len(cats)}개 (끝난 것 {len(cats) - len(todo)}, 남은 것 {len(todo)})")
    if a.limit_categories is not None:
        todo = todo[: a.limit_categories]
        print(f"이번 실행: 앞의 {len(todo)}개 카테고리 {todo[:10]}{' …' if len(todo) > 10 else ''}")

    session = GuestSession(client)
    res = fs_crawl.crawl(todo, store, session.post)

    print("")
    print(f"카테고리: 이번에 끝냄 {res.categories_done} / 손댐 {res.categories_touched} "
          f"(전체 {len(cats)} 중 끝난 것 {sum(1 for c in cats if store.state(c).get('done'))})")
    print(f"요청 수: {res.requests} (데이터 서버) · 페이지 {res.pages}")
    print(f"오류 수: {res.errors}")
    if res.capped:
        print(f"10,000개 상한으로 끝낸 카테고리(같은 페이지 되풀이): {res.capped}")
    print(f"상품 수: 이번에 새로 {res.new_products:,} · 누적(중복 제거) {len(store.seen):,}")
    print(f"손님 쿠키 받은 횟수: {session.acquired} (재발급 {session.refreshes})")
    print(f"걸린 시간: {res.seconds:,.0f}초")
    print(f"저장: {store.raw_path} · {store.csv_path}")
    print("완료" if not res.stopped else f"멈춤: {res.stopped}")
    return 0 if not res.stopped else 2


if __name__ == "__main__":
    raise SystemExit(main())
