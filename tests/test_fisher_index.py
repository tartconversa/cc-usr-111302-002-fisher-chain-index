"""Fisher 双边指数与时间链的确定性核算测试。

运行（隔离模式，fixtures 视为不可信数据）：
    python -I -m unittest discover -s tests -t . -v
普通模式同样支持：
    python -m unittest discover -s tests -v
"""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src.fisher_index import (
    COMPUTABLE,
    NOT_COMPUTABLE,
    LinkCache,
    ProductRegistry,
    build_panel,
    build_report,
    chain_indices,
    coverage_changes,
    observations_from_records,
    rebase_segment,
    reverse_checks,
)

ROOT = Path(__file__).resolve().parents[1]
TOL = Decimal("1E-10")
PRECISION = 40

REGISTRY = ProductRegistry(
    unit_factors={"unit": Decimal(1), "thousand": Decimal(1000)},
    products=frozenset(
        [("a", "v1"), ("b", "v1"), ("c", "v1"), ("a", "v2"), ("u", "v1"), ("new", "v1")]
    ),
)


def obs(period, product, quality, p, q, unit="unit", revision=0):
    """构造一条记录字典；p/q 传 None 写成空字符串（缺失而非零）。"""
    return {
        "period": period,
        "product": product,
        "quality": quality,
        "p": "" if p is None else str(p),
        "q": "" if q is None else str(q),
        "unit": unit,
        "revision": str(revision),
    }


def panel_of(records):
    with __import__("decimal").localcontext() as ctx:
        ctx.prec = PRECISION
        return build_panel(observations_from_records(records, REGISTRY))


def link(panel, p0, p1):
    cache = LinkCache()
    return cache.link(panel, p0, p1, TOL)


def assert_decimal(testcase, actual, expected, tol=TOL):
    testcase.assertIsNotNone(actual, "指数不可计算，但测试期望可计算")
    testcase.assertIsInstance(actual, Decimal)
    testcase.assertTrue(
        abs(actual - Decimal(str(expected))) <= tol,
        f"{actual} != {expected}",
    )


class CrossWeightWorkedExample(unittest.TestCase):
    """用户给定交叉权重例：两产品价格/数量对调。"""

    def setUp(self):
        records = [
            obs("t0", "a", "v1", 1, 2),
            obs("t0", "b", "v1", 2, 1),
            obs("t1", "a", "v1", 2, 1),
            obs("t1", "b", "v1", 1, 2),
        ]
        self.panel = panel_of(records)
        self.result = link(self.panel, "t0", "t1")

    def test_indices_match_hand_derived_values(self):
        r = self.result
        self.assertEqual(r.status, COMPUTABLE)
        self.assertEqual(r.matched_keys, (("a", "v1"), ("b", "v1")))
        # 交叉权重和：p0q0=4, p1q0=5, p0q1=5, p1q1=4
        self.assertEqual(r.sums.s00, Decimal(4))
        self.assertEqual(r.sums.s10, Decimal(5))
        self.assertEqual(r.sums.s01, Decimal(5))
        self.assertEqual(r.sums.s11, Decimal(4))
        assert_decimal(self, r.laspeyres_price, "1.25")   # 5/4
        assert_decimal(self, r.paasche_price, "0.8")      # 4/5
        assert_decimal(self, r.fisher_price, "1")
        assert_decimal(self, r.fisher_quantity, "1")
        # 数量指数对称：Laspeyres_Q = s01/s00 = 5/4，Paasche_Q = s11/s10 = 4/5
        assert_decimal(self, r.laspeyres_quantity, "1.25")
        assert_decimal(self, r.paasche_quantity, "0.8")
        assert_decimal(self, r.expenditure_ratio, "1")

    def test_matched_expenditure_both_periods_four(self):
        self.assertEqual(self.result.matched_expenditure["t0"], Decimal(4))
        self.assertEqual(self.result.matched_expenditure["t1"], Decimal(4))
        assert_decimal(self, self.result.coverage["t0"], "1")
        assert_decimal(self, self.result.coverage["t1"], "1")

    def test_factor_reversal_uses_matched_set_only(self):
        check = self.result.factor_reversal
        self.assertTrue(check["passed"])
        self.assertEqual(check["denominator_scope"], "matched_common_set")
        self.assertEqual(Decimal(check["expenditure_ratio"]), Decimal(1))

    def test_blank_is_missing_not_zero_on_reader_path(self):
        # 端到端：空价格行必须被排除在共同集合外，而不是按 0 参与
        records = [
            obs("t0", "a", "v1", 1, 2),
            obs("t0", "b", "v1", 2, 1),
            obs("t1", "a", "v1", 2, 1),
            obs("t1", "b", "v1", None, 2),
        ]
        result = link(panel_of(records), "t0", "t1")
        self.assertEqual(result.matched_keys, (("a", "v1"),))
        self.assertEqual(result.excluded[0]["key"], ["b", "v1"])
        self.assertIn("missing_or_blank_price", result.excluded[0]["reasons"].values())


class ThreePeriodChainDrift(unittest.TestCase):
    """固定篮子三期：链式 Fisher 与直接双边 Fisher 不同，漂移被保留。"""

    def setUp(self):
        records = [
            # t0: 两产品各价1量10
            obs("t0", "a", "v1", 1, 10),
            obs("t0", "b", "v1", 1, 10),
            # t1: a 涨价到4、需求降到5；b 价不变、量升到20
            obs("t1", "a", "v1", 4, 5),
            obs("t1", "b", "v1", 1, 20),
            # t2: a 回到价1量10；b 涨到4量5（结构对调）
            obs("t2", "a", "v1", 1, 10),
            obs("t2", "b", "v1", 4, 5),
        ]
        self.panel = panel_of(records)
        self.cache = LinkCache()
        self.chain = chain_indices(self.panel, ["t0", "t1", "t2"], self.cache, TOL)

    def test_hand_derived_link_values(self):
        l01, l12 = self.chain.links
        # t0->t1: s00=20, s10=50 -> Lp=2.5；s01=25, s11=40 -> Pp=1.6；Fisher=2
        assert_decimal(self, l01.laspeyres_price, "2.5")
        assert_decimal(self, l01.paasche_price, "1.6")
        assert_decimal(self, l01.fisher_price, "2")
        assert_decimal(self, l01.fisher_quantity, "1")
        assert_decimal(self, l01.expenditure_ratio, "2")
        # t1->t2: Lp=34/16=2.125，Pp=12/18=2/3
        assert_decimal(self, l12.laspeyres_price, Decimal(34) / Decimal(16))
        assert_decimal(self, l12.paasche_price, Decimal(2) / Decimal(3))
        assert_decimal(
            self,
            l12.fisher_price,
            (Decimal(34) / Decimal(16) * Decimal(2) / Decimal(3)).sqrt(),
        )
        for link_ in (l01, l12):
            self.assertTrue(link_.factor_reversal["passed"])

    def test_chain_differs_from_direct_and_drift_is_reported(self):
        direct = self.cache.link(self.panel, "t0", "t2", TOL)
        # 直接双边 t0->t2：Lp=2.5，Pp=2，Fisher=sqrt(5)
        assert_decimal(self, direct.laspeyres_price, "2.5")
        assert_decimal(self, direct.paasche_price, "2")
        assert_decimal(self, direct.fisher_price, Decimal(5).sqrt())
        assert_decimal(self, direct.expenditure_ratio, "1.5")
        chained = self.chain.segments[0].price["t2"]
        # 链式 = 2 * sqrt(17/12)，与直接值 sqrt(5) 不同：漂移被保留而非抹除
        expected_chain = Decimal(2) * (Decimal(17) / Decimal(12)).sqrt()
        assert_decimal(self, chained, expected_chain)
        self.assertNotEqual(chained, direct.fisher_price)
        self.assertGreater(abs(chained - direct.fisher_price), Decimal("0.1"))
        # 链内逐期因子反转仍成立（漂移不等于核算错误）
        self.assertTrue(all(c["passed"] for c in self.chain.segments[0].checks))


class ProductExitAndCoverage(unittest.TestCase):
    def test_exited_product_listed_and_coverage_below_one(self):
        records = [
            obs("t0", "a", "v1", 2, 3),
            obs("t0", "b", "v1", 5, 4),
            obs("t1", "a", "v1", 3, 3),  # b 退出
        ]
        result = link(panel_of(records), "t0", "t1")
        self.assertEqual(result.matched_keys, (("a", "v1"),))
        self.assertEqual(result.exits, (("b", "v1"),))
        # 单产品 Fisher=L=P=1.5
        assert_decimal(self, result.fisher_price, "1.5")
        assert_decimal(self, result.fisher_quantity, "1")
        assert_decimal(self, result.coverage["t0"], Decimal(6) / Decimal(26))
        # t1 市场仅剩匹配产品 a，覆盖率为 1
        assert_decimal(self, result.coverage["t1"], "1")

    def test_chain_break_when_period_has_no_observations(self):
        records = [
            obs("t0", "a", "v1", 1, 2),
            obs("t2", "a", "v1", 2, 2),
        ]
        panel = panel_of(records)
        cache = LinkCache()
        chain = chain_indices(panel, ["t0", "t1", "t2"], cache, TOL)
        self.assertEqual(len(chain.segments), 2)
        self.assertEqual(chain.breaks[0]["reasons"], ["period_without_observations"])
        self.assertEqual(len(chain.incomparable_intervals), 1)
        interval = chain.incomparable_intervals[0]
        self.assertEqual(interval["left_segment"], 0)
        self.assertEqual(interval["right_segment"], 1)
        self.assertEqual(interval["left_periods"], ["t0"])
        self.assertEqual(interval["right_periods"], ["t2"])
        # 不跨缺期硬接：t0 与 t2 的链值互不可比
        self.assertEqual(chain.segment_of["t0"], 0)
        self.assertEqual(chain.segment_of["t2"], 1)
        self.assertEqual(chain.segments[0].periods, ["t0"])
        self.assertEqual(chain.segments[1].periods, ["t2"])

    def test_missing_first_period_opens_segment_at_first_observed(self):
        records = [
            obs("t1", "a", "v1", 1, 2),
            obs("t2", "a", "v1", 2, 2),
        ]
        panel = panel_of(records)
        cache = LinkCache()
        chain = chain_indices(panel, ["t0", "t1", "t2"], cache, TOL)
        self.assertEqual(len(chain.segments), 1)
        self.assertEqual(chain.segments[0].periods, ["t1", "t2"])
        self.assertNotIn("t0", chain.segment_of)
        self.assertEqual(len(chain.breaks), 1)
        assert_decimal(self, chain.segments[0].price["t2"], "2")


class QualityVersionAndUnitConversion(unittest.TestCase):
    def test_quality_upgrade_is_new_key_not_same_product(self):
        records = [
            obs("t0", "a", "v1", 1, 10),
            obs("t1", "a", "v2", 2, 10),  # 同名质量升级
        ]
        panel = panel_of(records)
        result = link(panel, "t0", "t1")
        self.assertEqual(result.status, NOT_COMPUTABLE)
        self.assertIn("no_common_products", result.reasons)
        self.assertEqual(result.entries, (("a", "v2"),))
        self.assertEqual(result.exits, (("a", "v1"),))
        changes = coverage_changes(panel, ["t0", "t1"])
        self.assertEqual(changes[0]["quality_version_replacements"][0]["entered"], ["a", "v2"])

    def test_unit_conversion_preserves_expenditure_and_inverts_price(self):
        records = [
            obs("t0", "u", "v1", 2, 5, unit="unit"),       # p=2/标准单位, q=5
            obs("t1", "u", "v1", "0.003", "4000", unit="thousand"),
        ]
        panel = panel_of(records)
        obs0 = panel["t0"][("u", "v1")]
        obs1 = panel["t1"][("u", "v1")]
        # 标准单位：p1=0.003/1000=0.000003, q1=4000*1000=4,000,000；支出=12
        assert_decimal(self, obs1.p_base, "0.000003")
        assert_decimal(self, obs1.q_base, "4000000")
        self.assertEqual(obs0.expenditure, Decimal(10))
        self.assertEqual(obs1.expenditure, Decimal(12))
        result = link(panel, "t0", "t1")
        # Fisher=L=P=0.000003/2=1.5E-6（标准单位价比），数量指数=4e6/5=800000
        assert_decimal(self, result.fisher_price, "0.0000015")
        assert_decimal(self, result.fisher_quantity, "800000")
        self.assertTrue(result.factor_reversal["passed"])


class UndefinedCases(unittest.TestCase):
    def test_missing_price_is_not_filled_with_zero(self):
        records = [
            obs("t0", "a", "v1", 1, 5),
            obs("t1", "a", "v1", None, 5),
        ]
        result = link(panel_of(records), "t0", "t1")
        self.assertIsNone(result.fisher_price)
        self.assertIsNone(result.laspeyres_price)
        self.assertEqual(result.status, NOT_COMPUTABLE)

    def test_all_quantities_zero_is_undefined(self):
        records = [
            obs("t0", "a", "v1", 1, 0),
            obs("t0", "b", "v1", 2, 0),
            obs("t1", "a", "v1", 2, 0),
            obs("t1", "b", "v1", 1, 0),
        ]
        result = link(panel_of(records), "t0", "t1")
        self.assertIn("all_matched_quantities_zero", result.reasons)
        self.assertIsNone(result.fisher_price)

    def test_zero_base_quantity_kills_laspeyres_but_paasche_may_survive(self):
        # q0 全 0、q1 有正量：Laspeyres 分母 0 不可计算，Paasche 可计算但 Fisher 不完整
        records = [
            obs("t0", "a", "v1", 1, 0),
            obs("t1", "a", "v1", 2, 5),
        ]
        result = link(panel_of(records), "t0", "t1")
        self.assertIsNone(result.laspeyres_price)
        assert_decimal(self, result.paasche_price, "2")
        self.assertIsNone(result.fisher_price)
        self.assertEqual(result.status, NOT_COMPUTABLE)

    def test_non_positive_price_excluded_from_common_set(self):
        records = [
            obs("t0", "a", "v1", 1, 5),
            obs("t0", "b", "v1", 2, 5),
            obs("t1", "a", "v1", 0, 5),  # 价格必须严格正
            obs("t1", "b", "v1", 3, 5),
        ]
        result = link(panel_of(records), "t0", "t1")
        self.assertEqual(result.matched_keys, (("b", "v1"),))
        self.assertEqual(result.excluded[0]["reasons"]["t1"], "price_not_strictly_positive")


class ReciprocityAndRebase(unittest.TestCase):
    def test_forward_reverse_links_are_reciprocal(self):
        records = [
            obs("t0", "a", "v1", 1, 10),
            obs("t0", "b", "v1", 2, 5),
            obs("t1", "a", "v1", 3, 4),
            obs("t1", "b", "v1", 1, 8),
            obs("t2", "a", "v1", 2, 6),
            obs("t2", "b", "v1", 4, 3),
        ]
        panel = panel_of(records)
        cache = LinkCache()
        chain = chain_indices(panel, ["t0", "t1", "t2"], cache, TOL)
        checks = reverse_checks(panel, chain.links, cache, TOL)
        self.assertTrue(all(c["passed"] for c in checks))
        # 反向链接必须命中同一缓存条目
        self.assertEqual(len(cache._store), 2)

    def test_rebase_only_divides_by_same_chain_value(self):
        records = [
            obs("t0", "a", "v1", 1, 10),
            obs("t1", "a", "v1", 2, 10),
            obs("t2", "a", "v1", 4, 10),
        ]
        panel = panel_of(records)
        cache = LinkCache()
        chain = chain_indices(panel, ["t0", "t1", "t2"], cache, TOL)
        rebased = rebase_segment(chain.segments[0], "t1")
        self.assertEqual(list(rebased["price_index"].keys()), ["t0", "t1", "t2"])
        assert_decimal(self, Decimal(rebased["price_index"]["t1"]), "1")
        assert_decimal(self, Decimal(rebased["price_index"]["t0"]), "0.5")
        assert_decimal(self, Decimal(rebased["price_index"]["t2"]), "2")
        with self.assertRaises(ValueError):
            rebase_segment(chain.segments[0], "tx")

    def test_rebase_target_outside_segment_is_refused(self):
        records = [
            obs("t0", "a", "v1", 1, 10),
            obs("t2", "a", "v1", 2, 10),
        ]
        panel = panel_of(records)
        cache = LinkCache()
        chain = chain_indices(panel, ["t0", "t1", "t2"], cache, TOL)
        with self.assertRaises(ValueError):
            rebase_segment(chain.segments[0], "t2")  # t2 属于另一段


class CacheRevisionsAndScale(unittest.TestCase):
    def test_revision_takes_max_duplicate_and_equal_revision_errors(self):
        records = [
            obs("t0", "a", "v1", 1, 10, revision=1),
            obs("t1", "a", "v1", 2, 10, revision=1),
            obs("t1", "a", "v1", 3, 10, revision=2),  # 修订覆盖
        ]
        panel = panel_of(records)
        self.assertEqual(panel["t1"][("a", "v1")].p_raw, Decimal(3))
        result = link(panel, "t0", "t1")
        assert_decimal(self, result.fisher_price, "3")

        bad = records + [obs("t1", "a", "v1", 9, 10, revision=2)]
        with self.assertRaises(ValueError):
            panel_of(bad)

    def test_cache_reuses_bilateral_summary(self):
        records = [
            obs("t0", "a", "v1", 1, 10),
            obs("t1", "a", "v1", 2, 10),
        ]
        panel = panel_of(records)
        cache = LinkCache()
        forward = cache.link(panel, "t0", "t1", TOL)
        reverse = cache.link(panel, "t1", "t0", TOL)
        self.assertEqual(len(cache._store), 1)
        assert_decimal(self, forward.fisher_price * reverse.fisher_price, "1")

    def test_large_panel_is_linear_and_immutable(self):
        n_products, n_periods = 500, 6
        records = []
        for t in range(n_periods):
            for i in range(n_products):
                records.append(
                    obs(f"t{t}", f"p{i:04d}", "v1",
                        1 + (t + i) % 7, 10 + (t * i) % 13)
                )
        # 大表使用独立登记，避免污染 REGISTRY
        registry = ProductRegistry(
            unit_factors={"unit": Decimal(1)},
            products=frozenset((f"p{i:04d}", "v1") for i in range(n_products)),
        )
        from decimal import localcontext
        with localcontext() as ctx:
            ctx.prec = PRECISION
            panel = build_panel(observations_from_records(records, registry))
            cache = LinkCache()
            chain = chain_indices(panel, [f"t{t}" for t in range(n_periods)], cache, TOL)
        self.assertEqual(len(chain.links), n_periods - 1)
        self.assertTrue(all(l.computable for l in chain.links))
        self.assertEqual(len(chain.links[0].matched_keys), n_products)
        self.assertTrue(all(c["passed"] for s in chain.segments for c in s.checks))
        # 缓存摘要数量 = 相邻链接数
        self.assertEqual(len(cache._store), n_periods - 1)
        # 不可变对象：观测与汇总不可就地改写
        first_key = next(iter(panel["t0"]))
        with self.assertRaises(Exception):
            panel["t0"][first_key].p_raw = Decimal(99)


class EndToEndFixtures(unittest.TestCase):
    """对随仓库 fixtures 的端到端核对（输入哈希由既有契约测试另行校验）。"""

    def test_fixture_report_reproduces_worked_example_and_drift(self):
        report = build_report(
            ROOT / "fixtures/trades.csv",
            ROOT / "fixtures/products.json",
            ROOT / "fixtures/indices.json",
        )
        l01, l12 = report["links"]
        self.assertEqual(l01["price_index"]["laspeyres"], "1.25")
        self.assertEqual(l01["price_index"]["paasche"], "0.8")
        self.assertEqual(l01["price_index"]["fisher"], "1.00")
        self.assertEqual(l01["quantity_index"]["fisher"], "1.00")
        self.assertEqual(l01["matched_expenditure"], {"p0": "4", "p1": "4"})
        self.assertTrue(l01["expenditure_ratio_check"]["passed"])
        # 新产品进入 p2，覆盖率 2/3
        self.assertEqual(l12["entries"], [["new", "v1"]])
        assert_decimal(
            self, Decimal(l12["expenditure_coverage"]["p2"]),
            Decimal(8) / Decimal(12),
        )
        # 反向倒数
        self.assertTrue(all(c["passed"] for c in report["reverse_links"]))
        # 直接 p0->p2 Fisher=2，链式 ≈1.6733，漂移如实保留
        dvc = report["direct_vs_chain"]
        self.assertEqual(dvc["direct_fisher_price"], "2")
        self.assertNotEqual(dvc["chain_fisher_price"], dvc["direct_fisher_price"])
        self.assertTrue(Decimal(dvc["drift_relative_price"]) < 0)
        # 重定基期到 p1：p1 自身为 1，键顺序按期顺序
        self.assertEqual(list(report["rebased"]["price_index"].keys()), ["p0", "p1", "p2"])
        self.assertEqual(report["rebased"]["price_index"]["p1"], "1")
        # 缓存含三个无方向期间对
        self.assertEqual(len(report["cached_link_summaries"]), 3)
        # 范围声明存在，匹配指数不被称为完整市场指数
        self.assertIn("不是完整市场价格指数", report["scope_notice"])

    def test_report_written_through_cli_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "nested" / "report.json"
            from src.fisher_index import main
            rc = main([
                str(ROOT / "fixtures/trades.csv"),
                str(ROOT / "fixtures/products.json"),
                str(ROOT / "fixtures/indices.json"),
                "--output", str(out),
            ])
            self.assertEqual(rc, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["config"]["period_order"], ["p0", "p1", "p2"])


if __name__ == "__main__":
    unittest.main()
