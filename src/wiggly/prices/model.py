"""수집 결과 한 줄(CSV 한 행)과 채널 판정 한 줄(가능 여부 표 한 행)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import rules as rules_mod
from . import units
from .rules import ItemRule  # noqa: F401  (make_row 의 형 표시)

# CSV 열 순서. 계약의 열에 "단위기준"(100g/100ml) 하나를 더했다 — 같은 열에 g·ml 값이 섞이면 비교를 잘못하기 때문.
# 2차: "수집방식"(직접 / 화면) 열을 더했다.
CSV_COLUMNS = ["수집시각", "채널", "품목", "상품명", "가격(원)", "규격", "단위가격", "단위기준", "URL", "상태", "수집방식"]

STATUS_OK = "정상"
STATUS_NO_RESULT = "결과 없음"
STATUS_SPEC_FAIL = "규격 해석 실패"
STATUS_RULE_MISS = "규칙 불일치"
STATUS_NO_MATCH = "같은 규격 상품 없음"
STATUS_LOGIN = "로그인 필요"


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class PriceRow:
    채널: str
    품목: str
    상태: str
    상품명: str = ""
    가격: int | None = None
    규격: str = ""
    단위가격: float | None = None
    단위기준: str = ""
    URL: str = ""
    수집방식: str = ""  # 식봄: "직접"(데이터 서버) / "화면"(브라우저). 다른 채널은 빈칸
    수집시각: str = field(default_factory=now_str)

    def as_csv(self) -> dict:
        return {
            "수집시각": self.수집시각, "채널": self.채널, "품목": self.품목, "상품명": self.상품명,
            "가격(원)": "" if self.가격 is None else self.가격, "규격": self.규격,
            "단위가격": "" if self.단위가격 is None else self.단위가격, "단위기준": self.단위기준,
            "URL": self.URL, "상태": self.상태, "수집방식": self.수집방식,
        }


@dataclass
class ChannelVerdict:
    """채널별 가능 여부 표의 한 행. 판정은 '가능' / '로그인 필요' / '차단' / 'robots 금지' / '오류' 중 하나."""

    채널: str
    판정: str
    사유: str
    증거: list[str] = field(default_factory=list)  # "GET https://... → 403" 같은 줄, 캡처 경로


@dataclass
class ChannelResult:
    verdict: ChannelVerdict
    rows: list[PriceRow] = field(default_factory=list)


# ---------------------------------------------------------------- 행 만들기 (채널 공통)
def make_row(채널: str, rule: "ItemRule", *, name: str, price: int | None, spec: str, url: str,
             method: str = "") -> PriceRow:
    """상품 하나 → CSV 한 행. 상태는 정상 / 가격 없음 / 규격 해석 실패 / 규칙 불일치: <이유> 중 하나.

    단위가격은 묶음 전체 용량으로 계산하고(상자 가격 ÷ 상자 총량), 규칙은 낱개 용량으로 본다.
    """
    row = PriceRow(채널=채널, 품목=rule.품목, 상태=STATUS_OK, 상품명=name, 가격=price, 규격=spec, URL=url,
                   수집방식=method)
    if price is None:
        row.상태 = "가격 없음"
        return row
    want = rule.piece.base if rule.piece else None
    q = units.parse_spec(spec, name, want_base=want)
    if q is not None:
        row.단위기준 = f"100{q.base}"
        row.단위가격 = units.unit_price(price, q)
    ok, why = rules_mod.check(rule, name, spec)
    if not ok:
        row.상태 = f"{STATUS_RULE_MISS}: {why}"
    elif q is None:
        row.상태 = STATUS_SPEC_FAIL
    return row


STATUS_OUTLIER = "단위가격 이상치(최저가 비교 제외, 확인 필요)"


def flag_outliers(rows: list[PriceRow], ratio: float = 3.0) -> list[PriceRow]:
    """같은 채널·품목의 정상 행 중앙값보다 ratio 배 넘게 싸거나 비싼 행은 이상치로 표시한다.

    규격 칸이 묶음("2.5kg*6")인데 가격은 낱개 값인 상품처럼, 표기와 가격이 어긋난 행이 최저가로 뽑히는 것을 막는다.
    """
    groups: dict[tuple[str, str], list[PriceRow]] = {}
    for r in rows:
        if r.상태 == STATUS_OK and r.단위가격 is not None:
            groups.setdefault((r.채널, r.품목), []).append(r)
    for grp in groups.values():
        if len(grp) < 3:
            continue
        vals = sorted(r.단위가격 for r in grp)
        med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        for r in grp:
            if r.단위가격 < med / ratio or r.단위가격 > med * ratio:
                r.상태 = STATUS_OUTLIER
    return rows
