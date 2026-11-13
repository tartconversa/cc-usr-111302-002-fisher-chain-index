"""双边 Fisher 价格/数量指数与预定时间链。

本模块只做确定性的指数核算，不训练需求模型、不做统计推断或参数估计。

口径要点（与 fixtures/indices.json 的 matching=product_and_quality 对应）：

* 可比键 = (product, quality)。同名产品的质量升级产生新键，不自动视为同品。
* 单位先按 products.json 中显式的 to_base_quantity 换算：
  标准数量 q_base = q_raw * factor，标准价格 p_base = p_raw / factor，
  换算前后单笔支出 p*q 不变。
* 相邻期主指数仅使用两期共同存在、且两期价格严格为正、数量非负的产品；
  共同集合中至少一个量权和为正，否则该链接明确标记为不可计算。
* Laspeyres 价格比 = sum(p1*q0)/sum(p0*q0)
  Paasche  价格比 = sum(p1*q1)/sum(p0*q1)
  Fisher 价格指数 = 两者几何均值；数量指数由价格与数量对称交换得到。
* 分母为 0 或价格缺失时返回 None 并写明原因，绝不填 0。
* Fisher 价格 × Fisher 数量 的核对分母只能是同一共同集合的支出比，
  不能使用全体市场支出。
* 链式值按相邻期链接相乘；缺期形成链断裂，报告不可比区间，不跨缺期硬接。
* 匹配集合指数只代表匹配集合，不等同于完整市场价格指数。

金额与权重全部使用 decimal.Decimal，平方根受配置的 decimal_precision 约束，
容差核对使用 indices.json 的 tolerance。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Optional

from src.index_reader import load

# 状态常量
COMPUTABLE = "computable"
NOT_COMPUTABLE = "not_computable"

# 不可计算原因
REASON_NO_COMMON_PRODUCTS = "no_common_products"
REASON_MISSING_PERIOD = "period_without_observations"
REASON_ALL_QUANTITIES_ZERO = "all_matched_quantities_zero"
REASON_LASPEYRES_ZERO_DENOMINATOR = "laspeyres_zero_denominator"
REASON_PAASCHE_ZERO_DENOMINATOR = "paasche_zero_denominator"


# ---------------------------------------------------------------------------
# 输入层：单位登记、观测、面板
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductRegistry:
    """产品与计价单位登记。"""

    unit_factors: dict[str, Decimal]
    products: frozenset[tuple[str, str]]

    def factor(self, unit: str) -> Decimal:
        try:
            return self.unit_factors[unit]
        except KeyError as exc:
            raise ValueError(f"未知计价单位，无法显式换算: {unit!r}") from exc

    def knows(self, key: tuple[str, str]) -> bool:
        return key in self.products


@dataclass(frozen=True)
class Observation:
    """单期单产品（含质量版本）的一条报价/成交量观测。"""

    period: str
    product: str
    quality: str
    unit: str
    p_raw: Optional[Decimal]
    q_raw: Optional[Decimal]
    factor: Decimal
    revision: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.product, self.quality)

    @property
    def p_base(self) -> Optional[Decimal]:
        if self.p_raw is None:
            return None
        return self.p_raw / self.factor

    @property
    def q_base(self) -> Optional[Decimal]:
        if self.q_raw is None:
            return None
        return self.q_raw * self.factor

    @property
    def expenditure(self) -> Optional[Decimal]:
        """标准单位支出；缺失价格或数量时不可计算（None），不填 0。"""
        p, q = self.p_base, self.q_base
        if p is None or q is None:
            return None
        return p * q

    def validity(self) -> Optional[str]:
        """返回无效原因；None 表示可进入共同集合候选。"""
        if self.p_raw is None:
            return "missing_or_blank_price"
        if self.q_raw is None:
            return "missing_or_blank_quantity"
        if self.p_raw <= 0:
            return "price_not_strictly_positive"
        if self.q_raw < 0:
            return "negative_quantity"
        return None


def _to_decimal(raw, field_name: str) -> Optional[Decimal]:
    """空字符串/None 视为缺失（None），区别于数值 0。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        return Decimal(text)
    except Exception as exc:  # Decimal 抛 InvalidOperation 等
        raise ValueError(f"字段 {field_name} 无法解析为 Decimal: {raw!r}") from exc


def load_registry(products_path: str | Path) -> ProductRegistry:
    payload = load(products_path)
    units = payload.get("units", {})
    factors: dict[str, Decimal] = {}
    for name, spec in units.items():
        factor = _to_decimal(spec["to_base_quantity"], f"units.{name}.to_base_quantity")
        if factor is None or factor <= 0:
            raise ValueError(f"单位 {name!r} 的换算倍率必须严格为正")
        factors[name] = factor
    products = frozenset((item["product"], item["quality"]) for item in payload["products"])
    return ProductRegistry(unit_factors=factors, products=products)


def observations_from_records(
    records: list[dict],
    registry: ProductRegistry,
    *,
    strict_products: bool = True,
    source: str = "<records>",
) -> list[Observation]:
    """把类 CSV 行字典转成 Observation；空值保持缺失语义，不静默丢行。"""
    observations: list[Observation] = []
    for line_no, row in enumerate(records, start=1):
        product = row.get("product")
        quality = row.get("quality")
        period = row.get("period")
        unit = row.get("unit")
        if not product or not quality or not period or not unit:
            raise ValueError(f"{source}:{line_no} 缺少 period/product/quality/unit 字段")
        key = (product, quality)
        if strict_products and not registry.knows(key):
            raise ValueError(f"{source}:{line_no} 产品/质量版本未登记: {key}")
        factor = registry.factor(unit)
        revision_raw = row.get("revision", 0)
        revision_text = "" if revision_raw is None else str(revision_raw).strip()
        revision = 0 if revision_text == "" else int(revision_text)
        observations.append(
            Observation(
                period=period,
                product=product,
                quality=quality,
                unit=unit,
                p_raw=_to_decimal(row.get("p"), "p"),
                q_raw=_to_decimal(row.get("q"), "q"),
                factor=factor,
                revision=revision,
            )
        )
    return observations


def load_observations(
    trades_path: str | Path,
    registry: ProductRegistry,
    *,
    strict_products: bool = True,
) -> list[Observation]:
    records = load(trades_path)
    return observations_from_records(
        records, registry, strict_products=strict_products, source=str(trades_path)
    )


def build_panel(
    observations: list[Observation],
) -> dict[str, dict[tuple[str, str], Observation]]:
    """按 period -> 可比键 组织面板。

    重复修订规则：同一 (period, product, quality) 多行时取 revision 最大者；
    revision 相同的重复行直接报错，避免静默覆盖。
    """
    panel: dict[str, dict[tuple[str, str], Observation]] = {}
    for obs in observations:
        bucket = panel.setdefault(obs.period, {})
        existing = bucket.get(obs.key)
        if existing is None:
            bucket[obs.key] = obs
        elif obs.revision > existing.revision:
            bucket[obs.key] = obs
        elif obs.revision == existing.revision:
            raise ValueError(
                f"期间 {obs.period} 产品 {obs.key} 存在相同 revision={obs.revision} 的重复行"
            )
        # 更小的 revision 被保留值覆盖忽略
    return panel


# ---------------------------------------------------------------------------
# 双边链接层（无方向原始汇总 + 有方向链接视图 + 缓存）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bilateral:
    """两个期间的无方向双边核算摘要（缓存单元）。"""

    periods: tuple[str, str]
    matched_keys: tuple[tuple[str, str], ...]
    excluded: tuple[dict, ...]
    universe: dict[str, frozenset[tuple[str, str]]]
    # 以 sorted(periods) 的字母序存放两侧标准单位 p/q
    _values: dict[str, dict[tuple[str, str], tuple[Decimal, Decimal]]]
    market_expenditure: dict[str, Optional[Decimal]]
    matched_expenditure: dict[str, Decimal]
    # 以字母序首元素为“0”端的四个交叉权重和
    s00: Decimal
    s10: Decimal
    s01: Decimal
    s11: Decimal

    def oriented(self, period_0: str, period_1: str) -> "OrientedSums":
        a, b = sorted(self.periods)
        if (period_0, period_1) == (a, b):
            return OrientedSums(self.s00, self.s10, self.s01, self.s11)
        if (period_0, period_1) == (b, a):
            # 反向：S00'=S11, S11'=S00, S10'=S01, S01'=S10
            return OrientedSums(self.s11, self.s01, self.s10, self.s00)
        raise KeyError((period_0, period_1))


@dataclass(frozen=True)
class OrientedSums:
    s00: Decimal  # sum(p0*q0)
    s10: Decimal  # sum(p1*q0)
    s01: Decimal  # sum(p0*q1)
    s11: Decimal  # sum(p1*q1)


@dataclass(frozen=True)
class LinkResult:
    """相邻（或任意指定）两期之间的有方向链接结果。"""

    period_0: str
    period_1: str
    status: str
    reasons: tuple[str, ...]
    matched_keys: tuple[tuple[str, str], ...]
    excluded: tuple[dict, ...]
    entries: tuple[tuple[str, str], ...]
    exits: tuple[tuple[str, str], ...]
    sums: Optional[OrientedSums]
    market_expenditure: dict[str, Optional[Decimal]]
    matched_expenditure: dict[str, Decimal]
    coverage: dict[str, Optional[Decimal]]
    laspeyres_price: Optional[Decimal]
    paasche_price: Optional[Decimal]
    fisher_price: Optional[Decimal]
    laspeyres_quantity: Optional[Decimal]
    paasche_quantity: Optional[Decimal]
    fisher_quantity: Optional[Decimal]
    expenditure_ratio: Optional[Decimal]
    factor_reversal: Optional[dict]

    @property
    def computable(self) -> bool:
        return self.status == COMPUTABLE


class LinkCache:
    """缓存每个无方向期间对的共同集合摘要，重复链接不重算。"""

    def __init__(self) -> None:
        self._store: dict[frozenset[str], Bilateral] = {}

    def bilateral(self, panel, period_a: str, period_b: str) -> Optional[Bilateral]:
        if period_a == period_b:
            raise ValueError("链接必须是两个不同期间")
        key = frozenset((period_a, period_b))
        cached = self._store.get(key)
        if cached is not None:
            return cached
        bilateral = _build_bilateral(panel, period_a, period_b)
        if bilateral is not None:
            self._store[key] = bilateral
        return bilateral

    def link(self, panel, period_0: str, period_1: str, tolerance: Decimal) -> LinkResult:
        bilateral = self.bilateral(panel, period_0, period_1)
        if bilateral is None:
            return LinkResult(
                period_0=period_0,
                period_1=period_1,
                status=NOT_COMPUTABLE,
                reasons=(REASON_MISSING_PERIOD,),
                matched_keys=(),
                excluded=(),
                entries=(),
                exits=(),
                sums=None,
                market_expenditure={},
                matched_expenditure={},
                coverage={},
                laspeyres_price=None,
                paasche_price=None,
                fisher_price=None,
                laspeyres_quantity=None,
                paasche_quantity=None,
                fisher_quantity=None,
                expenditure_ratio=None,
                factor_reversal=None,
            )
        return _orient_link(bilateral, period_0, period_1, tolerance)


def _market_expenditure(bucket: dict[tuple[str, str], Observation]) -> Optional[Decimal]:
    total: Optional[Decimal] = Decimal(0)
    saw_valid = False
    for obs in bucket.values():
        exp = obs.expenditure
        if exp is None or exp < 0:
            # 缺失或负值不进入市场支出合计；市场总额本身不充当指数核对分母
            continue
        saw_valid = True
        total += exp
    return total if saw_valid else None


def _build_bilateral(
    panel: dict[str, dict[tuple[str, str], Observation]],
    period_a: str,
    period_b: str,
) -> Optional[Bilateral]:
    bucket_a = panel.get(period_a)
    bucket_b = panel.get(period_b)
    if not bucket_a or not bucket_b:
        return None

    universe_a = frozenset(bucket_a)
    universe_b = frozenset(bucket_b)
    candidate = sorted(universe_a & universe_b)

    matched: list[tuple[str, str]] = []
    excluded: list[dict] = []
    values: dict[str, dict[tuple[str, str], tuple[Decimal, Decimal]]] = {
        period_a: {},
        period_b: {},
    }
    for key in candidate:
        obs_a, obs_b = bucket_a[key], bucket_b[key]
        reason_a, reason_b = obs_a.validity(), obs_b.validity()
        if reason_a is not None or reason_b is not None:
            excluded.append(
                {
                    "key": list(key),
                    "reasons": {period_a: reason_a, period_b: reason_b},
                }
            )
            continue
        values[period_a][key] = (obs_a.p_base, obs_a.q_base)
        values[period_b][key] = (obs_b.p_base, obs_b.q_base)
        matched.append(key)

    first, second = sorted((period_a, period_b))
    s00 = s10 = s01 = s11 = Decimal(0)
    matched_exp: dict[str, Decimal] = {period_a: Decimal(0), period_b: Decimal(0)}
    for key in matched:
        p_a, q_a = values[period_a][key]
        p_b, q_b = values[period_b][key]
        if first == period_a:
            p0, q0, p1, q1 = p_a, q_a, p_b, q_b
        else:
            p0, q0, p1, q1 = p_b, q_b, p_a, q_a
        s00 += p0 * q0
        s10 += p1 * q0
        s01 += p0 * q1
        s11 += p1 * q1
        matched_exp[period_a] += p_a * q_a
        matched_exp[period_b] += p_b * q_b

    market_a = _market_expenditure(bucket_a)
    market_b = _market_expenditure(bucket_b)

    return Bilateral(
        periods=(period_a, period_b),
        matched_keys=tuple(matched),
        excluded=tuple(excluded),
        universe={period_a: universe_a, period_b: universe_b},
        _values=values,
        market_expenditure={period_a: market_a, period_b: market_b},
        matched_expenditure=matched_exp,
        s00=s00,
        s10=s10,
        s01=s01,
        s11=s11,
    )


def _coverage(matched: Decimal, market: Optional[Decimal]) -> Optional[Decimal]:
    if market is None or market == 0:
        return None
    return matched / market


def _orient_link(
    bilateral: Bilateral, period_0: str, period_1: str, tolerance: Decimal
) -> LinkResult:
    sums = bilateral.oriented(period_0, period_1)
    universe_0 = bilateral.universe[period_0]
    universe_1 = bilateral.universe[period_1]
    entries = tuple(sorted(universe_1 - universe_0))
    exits = tuple(sorted(universe_0 - universe_1))
    coverage = {
        period_0: _coverage(bilateral.matched_expenditure[period_0],
                            bilateral.market_expenditure[period_0]),
        period_1: _coverage(bilateral.matched_expenditure[period_1],
                            bilateral.market_expenditure[period_1]),
    }

    reasons: list[str] = []
    if not bilateral.matched_keys:
        reasons.append(REASON_NO_COMMON_PRODUCTS)

    # 量权和：价格严格为正，故 s00>0 等价于某 q0>0，s11>0 等价于某 q1>0
    if sums.s00 == 0 and sums.s11 == 0:
        reasons.append(REASON_ALL_QUANTITIES_ZERO)

    las_p = paas_p = fish_p = None
    las_q = paas_q = fish_q = None
    ratio = None
    check: Optional[dict] = None

    if sums.s00 > 0:
        las_p = sums.s10 / sums.s00
        las_q = sums.s01 / sums.s00
        ratio = sums.s11 / sums.s00
    else:
        reasons.append(REASON_LASPEYRES_ZERO_DENOMINATOR)
    if sums.s01 > 0:
        paas_p = sums.s11 / sums.s01
    else:
        reasons.append(REASON_PAASCHE_ZERO_DENOMINATOR)
    if sums.s10 > 0:
        paas_q = sums.s11 / sums.s10

    if las_p is not None and paas_p is not None:
        fish_p = (las_p * paas_p).sqrt()
    if las_q is not None and paas_q is not None:
        fish_q = (las_q * paas_q).sqrt()

    if fish_p is not None and fish_q is not None and ratio is not None:
        product = fish_p * fish_q
        error = abs(product - ratio)
        check = {
            "matched_expenditure_0": str(sums.s00),
            "matched_expenditure_1": str(sums.s11),
            "expenditure_ratio": str(ratio),
            "fisher_price_times_quantity": str(product),
            "absolute_error": str(error),
            "tolerance": str(tolerance),
            "passed": error <= tolerance,
            "denominator_scope": "matched_common_set",
        }

    status = COMPUTABLE if fish_p is not None and fish_q is not None else NOT_COMPUTABLE
    return LinkResult(
        period_0=period_0,
        period_1=period_1,
        status=status,
        reasons=tuple(reasons),
        matched_keys=bilateral.matched_keys,
        excluded=bilateral.excluded,
        entries=entries,
        exits=exits,
        sums=sums,
        market_expenditure=bilateral.market_expenditure,
        matched_expenditure=bilateral.matched_expenditure,
        coverage=coverage,
        laspeyres_price=las_p,
        paasche_price=paas_p,
        fisher_price=fish_p,
        laspeyres_quantity=las_q,
        paasche_quantity=paas_q,
        fisher_quantity=fish_q,
        expenditure_ratio=ratio,
        factor_reversal=check,
    )


# ---------------------------------------------------------------------------
# 时间链层
# ---------------------------------------------------------------------------


@dataclass
class ChainSegment:
    """一段连续可计算链接构成的链；段间互为不可比区间。"""

    periods: list[str]
    price: dict[str, Decimal]
    quantity: dict[str, Decimal]
    expenditure: dict[str, Decimal]
    checks: list[dict] = field(default_factory=list)


@dataclass
class ChainResult:
    segments: list[ChainSegment]
    breaks: list[dict]
    links: list[LinkResult]
    incomparable_intervals: list[dict]
    segment_of: dict[str, int]


def chain_indices(
    panel: dict[str, dict[tuple[str, str], Observation]],
    period_order: list[str],
    cache: LinkCache,
    tolerance: Decimal,
) -> ChainResult:
    unknown = sorted(set(panel) - set(period_order))
    if unknown:
        raise ValueError(f"以下期间未在 period_order 中声明，期顺序必须显式: {unknown}")
    if len(set(period_order)) != len(period_order):
        raise ValueError("period_order 存在重复期间")

    links: list[LinkResult] = []
    breaks: list[dict] = []
    segments: list[ChainSegment] = []
    segment_of: dict[str, int] = {}

    def _start_segment(period: str) -> ChainSegment:
        segment = ChainSegment(
            periods=[period],
            price={period: Decimal(1)},
            quantity={period: Decimal(1)},
            expenditure={period: Decimal(1)},
        )
        segments.append(segment)
        segment_of[period] = len(segments) - 1
        return segment

    if period_order[0] in panel:
        _start_segment(period_order[0])
    # 首个期间无观测时不建空段，等第一个有观测期间再开段

    for prev, nxt in zip(period_order, period_order[1:]):
        link = cache.link(panel, prev, nxt, tolerance)
        links.append(link)
        if not link.computable:
            breaks.append(
                {
                    "period_0": prev,
                    "period_1": nxt,
                    "reasons": list(link.reasons),
                }
            )
            if nxt in panel and nxt not in segment_of:
                _start_segment(nxt)
            continue

        # 可计算链接：两端必有观测；prev 应已属于某段
        if prev not in segment_of:
            _start_segment(prev)
        current = segments[segment_of[prev]]
        segment_of[nxt] = segment_of[prev]
        current.periods.append(nxt)
        current.price[nxt] = current.price[prev] * link.fisher_price
        current.quantity[nxt] = current.quantity[prev] * link.fisher_quantity
        current.expenditure[nxt] = current.expenditure[prev] * link.expenditure_ratio
        error = abs(current.price[nxt] * current.quantity[nxt] - current.expenditure[nxt])
        current.checks.append(
            {
                "period": nxt,
                "absolute_error": str(error),
                "tolerance": str(tolerance),
                "passed": error <= tolerance,
                "denominator_scope": "product_of_matched_link_expenditure_ratios",
            }
        )

    incomparable: list[dict] = []
    for left_idx in range(len(segments)):
        for right_idx in range(left_idx + 1, len(segments)):
            left, right = segments[left_idx], segments[right_idx]
            incomparable.append(
                {
                    "left_segment": left_idx,
                    "right_segment": right_idx,
                    "left_periods": list(left.periods),
                    "right_periods": list(right.periods),
                    "reason": "chain_break_or_missing_period; no hard bridge across gap",
                }
            )

    return ChainResult(
        segments=segments,
        breaks=breaks,
        links=links,
        incomparable_intervals=incomparable,
        segment_of=segment_of,
    )


def rebase_segment(segment: ChainSegment, new_base: str) -> dict:
    """基期重定：仅除以同一链段在新基期的值，不跨段、不重新定权。"""
    if new_base not in segment.periods:
        raise ValueError(f"新基期 {new_base!r} 不在该链段内，不能跨段重定基期")
    base_p = segment.price[new_base]
    base_q = segment.quantity[new_base]
    base_e = segment.expenditure[new_base]
    ordered = list(segment.periods)  # 段内期间已按 period_order 排列
    return {
        "new_base": new_base,
        "segment_periods": list(ordered),
        "price_index": {t: str(segment.price[t] / base_p) for t in ordered},
        "quantity_index": {t: str(segment.quantity[t] / base_q) for t in ordered},
        "expenditure_ratio": {t: str(segment.expenditure[t] / base_e) for t in ordered},
    }


# ---------------------------------------------------------------------------
# 覆盖变化与反向链接核对
# ---------------------------------------------------------------------------


def coverage_changes(panel: dict, period_order: list[str]) -> list[dict]:
    changes: list[dict] = []
    for prev, nxt in zip(period_order, period_order[1:]):
        prev_keys = frozenset(panel.get(prev, {}))
        next_keys = frozenset(panel.get(nxt, {}))
        entered = sorted(next_keys - prev_keys)
        exited = sorted(prev_keys - next_keys)
        # 同名产品质量版本替换：进入键与退出键共享 product 名
        entered_products = {p for p, _ in entered}
        quality_replacements = [
            {"entered": list(k), "related_exited": [list(e) for e in exited if e[0] == k[0]]}
            for k in entered
            if k[0] in entered_products and any(e[0] == k[0] for e in exited)
        ]
        changes.append(
            {
                "period_0": prev,
                "period_1": nxt,
                "entered_keys": [list(k) for k in entered],
                "exited_keys": [list(k) for k in exited],
                "quality_version_replacements": quality_replacements,
                "note": "进入/退出只改变覆盖范围，匹配集合指数不代表完整市场价格",
            }
        )
    return changes


def reverse_checks(
    panel: dict,
    links: list[LinkResult],
    cache: LinkCache,
    tolerance: Decimal,
) -> list[dict]:
    """反向链接必须与正向链接互为倒数（时间反转检验）。"""
    out: list[dict] = []
    for link in links:
        reverse = cache.link(panel, link.period_1, link.period_0, tolerance)
        if not link.computable or not reverse.computable:
            out.append(
                {
                    "forward": [link.period_0, link.period_1],
                    "reverse": [link.period_1, link.period_0],
                    "passed": False,
                    "reason": "one_direction_not_computable",
                }
            )
            continue
        p_error = abs(link.fisher_price * reverse.fisher_price - Decimal(1))
        q_error = abs(link.fisher_quantity * reverse.fisher_quantity - Decimal(1))
        out.append(
            {
                "forward": [link.period_0, link.period_1],
                "reverse": [link.period_1, link.period_0],
                "forward_fisher_price": str(link.fisher_price),
                "reverse_fisher_price": str(reverse.fisher_price),
                "price_reciprocity_error": str(p_error),
                "quantity_reciprocity_error": str(q_error),
                "tolerance": str(tolerance),
                "passed": p_error <= tolerance and q_error <= tolerance,
            }
        )
    return out


def direct_vs_chain(
    panel: dict,
    pair: tuple[str, str],
    chain: ChainResult,
    cache: LinkCache,
    tolerance: Decimal,
) -> dict:
    """直接双边指数与链式指数的漂移比较；两者共同集合可能不同，均如实保留。"""
    period_0, period_1 = pair
    direct = cache.link(panel, period_0, period_1, tolerance)
    result: dict = {
        "period_0": period_0,
        "period_1": period_1,
        "direct_status": direct.status,
        "direct_matched_keys": [list(k) for k in direct.matched_keys],
        "direct_fisher_price": _d(direct.fisher_price),
        "direct_fisher_quantity": _d(direct.fisher_quantity),
        "direct_expenditure_ratio": _d(direct.expenditure_ratio),
        "direct_factor_reversal": direct.factor_reversal,
    }
    seg_0 = chain.segment_of.get(period_0)
    seg_1 = chain.segment_of.get(period_1)
    if seg_0 is not None and seg_0 == seg_1 and direct.computable:
        segment = chain.segments[seg_0]
        chained_p = segment.price[period_1] / segment.price[period_0]
        chained_q = segment.quantity[period_1] / segment.quantity[period_0]
        drift_p = chained_p - direct.fisher_price
        drift_q = chained_q - direct.fisher_quantity
        result.update(
            {
                "same_chain_segment": True,
                "chain_fisher_price": str(chained_p),
                "chain_fisher_quantity": str(chained_q),
                "drift_absolute_price": str(drift_p),
                "drift_relative_price": str(drift_p / direct.fisher_price),
                "drift_absolute_quantity": str(drift_q),
                "drift_relative_quantity": str(drift_q / direct.fisher_quantity),
                "tolerance": str(tolerance),
            }
        )
    else:
        result.update(
            {
                "same_chain_segment": False,
                "chain_fisher_price": None,
                "drift_absolute_price": None,
                "note": "两端不在同一链段（或缺期），不硬接；仅保留独立双边结果",
            }
        )
    return result


# ---------------------------------------------------------------------------
# 报告组装与序列化
# ---------------------------------------------------------------------------


def _d(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _link_to_dict(link: LinkResult) -> dict:
    return {
        "period_0": link.period_0,
        "period_1": link.period_1,
        "status": link.status,
        "not_computable_reasons": list(link.reasons),
        "matched_keys": [list(k) for k in link.matched_keys],
        "excluded_keys": list(link.excluded),
        "entries": [list(k) for k in link.entries],
        "exits": [list(k) for k in link.exits],
        "cross_weight_sums": None
        if link.sums is None
        else {
            "s00_p0q0": str(link.sums.s00),
            "s10_p1q0": str(link.sums.s10),
            "s01_p0q1": str(link.sums.s01),
            "s11_p1q1": str(link.sums.s11),
        },
        "market_expenditure": {t: _d(v) for t, v in link.market_expenditure.items()},
        "matched_expenditure": {t: _d(v) for t, v in link.matched_expenditure.items()},
        "expenditure_coverage": {t: _d(v) for t, v in link.coverage.items()},
        "price_index": {
            "laspeyres": _d(link.laspeyres_price),
            "paasche": _d(link.paasche_price),
            "fisher": _d(link.fisher_price),
        },
        "quantity_index": {
            "laspeyres": _d(link.laspeyres_quantity),
            "paasche": _d(link.paasche_quantity),
            "fisher": _d(link.fisher_quantity),
        },
        "expenditure_ratio_check": link.factor_reversal or {
            "expenditure_ratio": _d(link.expenditure_ratio),
            "passed": False,
            "denominator_scope": "matched_common_set",
            "note": "Fisher 不完整，因子反转检验无法执行",
        },
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_report(
    trades_path: str | Path,
    products_path: str | Path,
    indices_path: str | Path,
) -> dict:
    trades_path, products_path, indices_path = Path(trades_path), Path(products_path), Path(indices_path)
    config = load(indices_path)
    precision = int(config.get("decimal_precision", 40))
    tolerance = Decimal(str(config.get("tolerance", 1e-10)))
    period_order = list(config["period_order"])

    with localcontext() as ctx:
        ctx.prec = precision
        registry = load_registry(products_path)
        observations = load_observations(trades_path, registry)
        panel = build_panel(observations)
        cache = LinkCache()
        chain = chain_indices(panel, period_order, cache, tolerance)

        period_blocks = []
        for period in period_order:
            bucket = panel.get(period, {})
            period_blocks.append(
                {
                    "period": period,
                    "observation_keys": [list(k) for k in sorted(bucket)],
                    "market_expenditure": _d(_market_expenditure(bucket)),
                    "revisions": {
                        f"{k[0]}|{k[1]}": obs.revision for k, obs in sorted(bucket.items())
                    },
                }
            )

        segments_out = []
        for idx, segment in enumerate(chain.segments):
            segments_out.append(
                {
                    "segment": idx,
                    "periods": list(segment.periods),
                    "base_period": segment.periods[0],
                    "chained_fisher_price": {t: str(v) for t, v in segment.price.items()},
                    "chained_fisher_quantity": {t: str(v) for t, v in segment.quantity.items()},
                    "chained_expenditure_ratio": {t: str(v) for t, v in segment.expenditure.items()},
                    "chain_factor_reversal_checks": segment.checks,
                }
            )

        rebase = config.get("rebase")
        rebased = None
        if rebase is not None:
            seg_idx = chain.segment_of.get(rebase)
            if seg_idx is None:
                rebased = {"new_base": rebase, "error": "基期期间无观测，无法重定"}
            else:
                rebased = rebase_segment(chain.segments[seg_idx], rebase)

        direct_pair = tuple(config.get("direct_comparison", ()))
        direct = direct_vs_chain(panel, direct_pair, chain, cache, tolerance) if direct_pair else None

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "inputs": {
                "trades": {"path": str(trades_path), "sha256": _sha256(trades_path)},
                "products": {"path": str(products_path), "sha256": _sha256(products_path)},
                "indices": {"path": str(indices_path), "sha256": _sha256(indices_path)},
            },
            "config": {
                "period_order": period_order,
                "base": config.get("base"),
                "rebase": rebase,
                "direct_comparison": list(direct_pair),
                "matching": config.get("matching", "product_and_quality"),
                "decimal_precision": precision,
                "tolerance": str(tolerance),
            },
            "scope_notice": (
                "所有指数仅基于两期共同且有效的匹配产品集合；"
                "进入/退出产品单列覆盖变化，匹配集合指数不是完整市场价格指数"
            ),
            "periods": period_blocks,
            "links": [_link_to_dict(link) for link in chain.links],
            "reverse_links": reverse_checks(panel, chain.links, cache, tolerance),
            "chain": {
                "segments": segments_out,
                "breaks": chain.breaks,
                "incomparable_intervals": chain.incomparable_intervals,
            },
            "rebased": rebased,
            "direct_vs_chain": direct,
            "coverage_changes": coverage_changes(panel, period_order),
            "cached_link_summaries": [
                {
                    "periods": sorted(bilateral.periods),
                    "matched_keys": [list(k) for k in bilateral.matched_keys],
                    "matched_count": len(bilateral.matched_keys),
                    "cross_weight_sums": {
                        "s00_p0q0": str(bilateral.s00),
                        "s10_p1q0": str(bilateral.s10),
                        "s01_p0q1": str(bilateral.s01),
                        "s11_p1q1": str(bilateral.s11),
                    },
                    "excluded_count": len(bilateral.excluded),
                }
                for bilateral in sorted(
                    cache._store.values(), key=lambda b: tuple(sorted(b.periods))
                )
            ],
        }
        return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="固定可比产品集合的双边 Fisher 价格/数量指数与时间链（不训练需求模型）"
    )
    parser.add_argument("trades", help="报价与成交量 CSV/JSONL/JSON")
    parser.add_argument("products", help="产品、质量版本与单位换算登记 JSON")
    parser.add_argument("indices", help="期间顺序、基期与容差配置 JSON")
    parser.add_argument(
        "--output",
        default=".workspace-output/fisher_report.json",
        help="报告输出路径（默认 .workspace-output/fisher_report.json）",
    )
    args = parser.parse_args(argv)

    report = build_report(args.trades, args.products, args.indices)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for link in report["links"]:
        print(
            f"link {link['period_0']}->{link['period_1']} "
            f"status={link['status']} matched={len(link['matched_keys'])} "
            f"fisher_price={link['price_index']['fisher']} "
            f"fisher_quantity={link['quantity_index']['fisher']}"
        )
    print(f"report written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
