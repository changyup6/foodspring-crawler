"""식봄(foodspring.co.kr) 상품·가격 수집. 실행은 scripts/crawl_foodspring_full.py.

- polite: 요청 간격·횟수·robots.txt
- fs_api: 손님 쿠키를 받아 데이터 서버(GraphQL)에 요청
- fs_crawl: 카테고리 전체 수집·저장·이어하기
- foodspring: 검색어 하나로 상품을 찾는 수집기
- units / rules / model: 규격 → 총량 → 단위가격 계산
"""
