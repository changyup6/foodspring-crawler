"""품목별 비교 규칙(2차): 같은 규격·같은 맛 상품끼리만 최저가를 비교한다.

config/price_items.csv 의 세 열:
  - 필수낱말: 상품명에 전부 있어야 한다(`|` 로 구분). 띄어쓰기·대소문자 무시.
  - 제외낱말: 하나라도 있으면 뺀다(`|` 로 구분).
  - 낱개규격: 낱개 1개 용량. 상품의 낱개 용량이 정확히 같아야 한다(1kg ≠ 900g). 묶음 수는 달라도 된다.
규칙에 맞는 상품이 없으면 없는 대로 둔다 — 규칙을 자동으로 느슨하게 하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import units


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _words(s: str | None) -> list[str]:
    return [w.strip() for w in (s or "").split("|") if w.strip()]


@dataclass(frozen=True)
class ItemRule:
    품목: str
    필수: tuple[str, ...]
    제외: tuple[str, ...]
    낱개규격: str
    piece: units.Qty | None  # 낱개규격을 읽은 값

    @classmethod
    def from_item(cls, it: dict) -> "ItemRule":
        spec = it.get("낱개규격") or it.get("기준규격") or ""
        return cls(it["품목"], tuple(_words(it.get("필수낱말"))), tuple(_words(it.get("제외낱말"))),
                   spec, units.parse_qty(spec))

    def describe(self) -> str:
        return f"필수 {'·'.join(self.필수) or '—'} / 제외 {'·'.join(self.제외) or '—'} / 낱개 {self.낱개규격}"


def check(rule: ItemRule, name: str, spec: str) -> tuple[bool, str]:
    """상품 하나가 규칙에 맞나 → (맞음?, 안 맞는 이유)."""
    n = _norm(name)
    missing = [w for w in rule.필수 if _norm(w) not in n]
    if missing:
        return False, f"필수낱말 없음({','.join(missing)})"
    hit = [w for w in rule.제외 if _norm(w) in n]
    if hit:
        return False, f"제외낱말({','.join(hit)})"
    if rule.piece is None:
        return False, f"낱개규격 해석 실패({rule.낱개규격})"
    p = units.parse_piece(spec, name, want_base=rule.piece.base)
    if p is None:
        return False, "낱개 용량 해석 실패"
    if p.base != rule.piece.base or abs(p.amount - rule.piece.amount) > 1e-6:
        return False, f"낱개 용량 다름({_fmt(p)} ≠ {rule.낱개규격})"
    return True, ""


def _fmt(q: units.Qty) -> str:
    v = q.amount
    if v >= 1000:
        return f"{v / 1000:g}{'kg' if q.base == 'g' else 'L'}"
    return f"{v:g}{q.base}"


def violations(table: list[dict], rules: dict[str, ItemRule]) -> list[str]:
    """최저가 표의 행을 규칙에 다시 대 본다. table 은 summary.cheapest() 결과."""
    out = []
    for c in table:
        b = c["best"]
        if b is None:
            continue
        ok, why = check(rules[c["item"]["품목"]], b.상품명, b.규격)
        if not ok:
            out.append(f"{c['item']['품목']}: {b.상품명} — {why}")
    return out
