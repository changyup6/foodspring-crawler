"""규격 글자("1kg", "500g x 2", "1.5L*12", "355ml*24") → 총량(g 또는 ml) → 단위가격(원/100g·원/100ml).

네트워크와 무관한 순수 함수만 둔다(시험이 오프라인으로 돈다).
- 무게는 g, 부피는 ml 로 통일한다. kg→×1000, L→×1000, cc→ml.
- 첫 번째 용량 뒤에 바로 "x 2", "*24", "×12" 처럼 곱하기가 붙으면 곱한다.
  "850g(170g*5)" 처럼 괄호 안 설명은 바깥 총량(850g)을 쓴다 — 첫 용량 뒤에 곱하기가 없으므로.
- 해석 못 하면 None. 호출하는 쪽이 상태 칸에 "규격 해석 실패" 를 적는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 단위 글자 → (기준 단위, 배수)
_UNIT = {
    "kg": ("g", 1000.0), "키로": ("g", 1000.0), "킬로": ("g", 1000.0), "g": ("g", 1.0), "그램": ("g", 1.0),
    "l": ("ml", 1000.0), "리터": ("ml", 1000.0), "ml": ("ml", 1.0), "cc": ("ml", 1.0),
}
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
# kg 를 g 보다, ml 을 l 보다 먼저 시도해야 "1kg" 이 "1k"+"g" 로 잘리지 않는다.
# 단위 뒤에 영문자가 붙으면 단위가 아니다 — 단, "500gX20" 처럼 곱하기 x 는 허용.
_QTY = re.compile(_NUM + r"\s*(kg|키로|킬로|그램|ml|cc|리터|g|l)(?![a-wyz])", re.IGNORECASE)
_MULT = re.compile(r"\s*[x×X*]\s*(\d+)\s*(?:개|입|ea|병|캔|봉|팩|pet|ea)?", re.IGNORECASE)
_COUNT = re.compile(r"(?<![\d.])(\d+)\s*(?:개입|입|ea)(?![a-z가-힣])", re.IGNORECASE)
_BOX = re.compile(r"box|박스|cs\b|케이스", re.IGNORECASE)


@dataclass(frozen=True)
class Qty:
    amount: float  # 총량 (g 또는 ml)
    base: str      # "g" 또는 "ml"
    multiplied: bool  # 곱하기(묶음)가 적용됐는지


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _mult_after(text: str, pos: int) -> int:
    mm = _MULT.match(text, pos)
    return int(mm.group(1)) if mm else 1


def parse_qty(text: str | None) -> Qty | None:
    """글자에서 총량을 읽는다. 없으면 None.

    1) 용량 뒤에 바로 곱하기가 붙은 것("355ml x 24")이 있으면 그 첫 번째를 쓴다.
    2) 없으면 첫 용량을 쓰고, 글자 어딘가에 "24입"·"30개입"·"24ea" 같은 개수가 따로 있으면 곱한다
       ("칠성 사이다 355ml (뚱캔,24입)" → 8,520ml).
    """
    if not text:
        return None
    found = []
    for m in _QTY.finditer(text):
        base, factor = _UNIT[m.group(2).lower()]
        amount = _num(m.group(1)) * factor
        if amount > 0:
            found.append((amount, base, _mult_after(text, m.end())))
    if not found:
        return None
    multi = [f for f in found if f[2] > 1]
    if multi:
        amount, base, n = multi[0]
        return Qty(amount=amount * n, base=base, multiplied=True)
    amount, base, _ = found[0]
    c = _COUNT.search(text)
    if c and int(c.group(1)) > 1:
        return Qty(amount=amount * int(c.group(1)), base=base, multiplied=True)
    return Qty(amount=amount, base=base, multiplied=False)


def parse_spec(spec: str | None, name: str | None = None, want_base: str | None = None) -> Qty | None:
    """규격 칸을 먼저 읽고, 못 읽으면 상품명에서 읽는다.

    - 규격 칸이 "1.5L / BOX" 처럼 낱개 용량에 상자 단위만 적혀 있고 상품명에 "1.5L*12" 같은
      묶음 표기가 있으면 상품명 쪽(총량)을 쓴다 — 상자 가격을 낱개 용량으로 나누면 단위가격이 12배 부풀기 때문.
    - 음료·기름처럼 기준이 ml 인데 규격 칸은 무게("8.52KG / BOX")만 있고 상품명에 ml 이 있으면
      상품명의 ml 을 쓴다. 규격 무게 ÷ 상품명 용량이 정수 n(오차 3% 안)이면 n 개 묶음으로 본다
      ("칠성 사이다(355ml ...)" + "8.52KG / BOX" → 355ml × 24).
    """
    q_spec = parse_qty(spec)
    q_name = parse_qty(name)
    if q_spec is None:
        return q_name
    if (
        q_name is not None
        and q_name.multiplied
        and not q_spec.multiplied
        and q_name.base == q_spec.base
        and spec is not None
        and _BOX.search(spec)
    ):
        return q_name
    if want_base and q_spec.base != want_base and q_name is not None and q_name.base == want_base:
        ratio = q_spec.amount / q_name.amount
        n = round(ratio)
        if n >= 1 and abs(ratio - n) <= 0.03 * n:
            return Qty(amount=q_name.amount * n, base=want_base, multiplied=n > 1 or q_name.multiplied)
        return q_name
    return q_spec


def base_of(spec: str | None) -> str | None:
    """기준규격("1kg", "1.5L") 이 무게(g)인지 부피(ml)인지."""
    q = parse_qty(spec)
    return q.base if q else None


def unit_price(price: float | int | None, qty: Qty | None) -> float | None:
    """원/100g 또는 원/100ml. 소수 첫째 자리까지."""
    if price is None or qty is None or qty.amount <= 0:
        return None
    return round(float(price) / qty.amount * 100.0, 1)


def parse_price(text: str | None) -> int | None:
    """"1,760원" → 1760. 숫자가 없으면 None."""
    if not text:
        return None
    m = re.search(r"\d{1,3}(?:,\d{3})+|\d+", text)
    return int(m.group(0).replace(",", "")) if m else None


def parse_piece(spec: str | None, name: str | None = None, want_base: str | None = None) -> Qty | None:
    """낱개 1개 용량(묶음 수는 곱하지 않는다). 품목 규칙의 '낱개규격' 과 비교할 때 쓴다.

    후보를 이 순서로 보고, 기준 단위(want_base)와 같은 첫 후보를 고른다:
      1) 규격 칸에서 곱하기 바로 앞 용량("0.355kg * 24입" → 355g, "1.5L*12" → 1.5L)
      2) 상품명에서 곱하기 바로 앞 용량
      3) 규격 칸의 첫 용량("1KG / EA" → 1kg)
      4) 상품명의 첫 용량
    기준 단위와 같은 후보가 없으면 첫 후보를 그대로 돌려준다(호출하는 쪽에서 단위가 달라 규칙 불일치가 된다).
    예: 규격 "8.52KG / BOX" + 상품명 "칠성 사이다(355ml ...)" + 기준 ml → 355ml.
    """
    def scan(text: str | None) -> tuple[list[Qty], list[Qty]]:
        multi, plain = [], []
        for m in _QTY.finditer(text or ""):
            base, factor = _UNIT[m.group(2).lower()]
            amount = _num(m.group(1)) * factor
            if amount <= 0:
                continue
            q = Qty(amount=amount, base=base, multiplied=False)
            (multi if _mult_after(text, m.end()) > 1 else plain).append(q)
        return multi, plain

    s_multi, s_plain = scan(spec)
    n_multi, n_plain = scan(name)
    cands = s_multi[:1] + n_multi[:1] + (s_multi + s_plain)[:1] + (n_multi + n_plain)[:1]
    if not cands:
        return None
    if want_base:
        for q in cands:
            if q.base == want_base:
                return q
    return cands[0]
