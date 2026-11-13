"""生成综合合成样例（链漂移/退出/质量版本/单位换算/缺期）。

输出到 synthetic_example/，与受哈希保护的 fixtures/ 完全分离；
本脚本不读取任何外部数据，全部数值为手工设计的虚构数据。
运行：python -I scripts/make_synthetic_example.py
"""

import csv
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "synthetic_example"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 单位：unit 倍率 1；thousand 表示千单位，标准数量 = 原始数量 * 1000
    products = {
        "products": [
            {"product": "alpha", "quality": "v1"},
            {"product": "alpha", "quality": "v2"},
            {"product": "beta", "quality": "v1"},
            {"product": "gamma", "quality": "v1"},
        ],
        "units": {
            "unit": {"to_base_quantity": "1"},
            "thousand": {"to_base_quantity": "1000"},
        },
    }
    (OUT / "products.json").write_text(
        json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows = [
        # m0: alpha v1 与 beta v1
        ("m0", "alpha", "v1", "10", "100", "unit"),
        ("m0", "beta", "v1", "20", "50", "unit"),
        # m1: alpha 涨价且量缩（替代效应），beta 量扩；gamma 用千单位报价
        ("m1", "alpha", "v1", "40", "25", "unit"),
        ("m1", "beta", "v1", "20", "100", "unit"),
        ("m1", "gamma", "v1", "0.002", "50", "thousand"),
        # m2: beta 退出；alpha 质量升级 v2（v1 同时退出）；gamma 改回 unit 报价
        ("m2", "alpha", "v2", "12", "90", "unit"),
        ("m2", "gamma", "v1", "0.000004", "40000", "unit"),
        # m3 缺观测（断链，不得与 m2 硬接）
        # m4: 市场恢复，仅 alpha v2 与 gamma
        ("m4", "alpha", "v2", "15", "80", "unit"),
        ("m4", "gamma", "v1", "0.000005", "30000", "unit"),
        # m5: alpha v2 修订（revision=1 覆盖同键 revision=0 的临时报价）
        ("m5", "alpha", "v2", "16", "70", "unit", 0),
        ("m5", "alpha", "v2", "18", "70", "unit", 1),
        ("m5", "gamma", "v1", "0.000006", "25000", "unit"),
    ]

    with (OUT / "trades.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["period", "product", "quality", "p", "q", "unit", "revision"])
        for row in rows:
            if len(row) == 6:
                writer.writerow(list(row) + [0])
            else:
                writer.writerow(row)

    indices = {
        "period_order": ["m0", "m1", "m2", "m3", "m4", "m5"],
        "base": "m0",
        "rebase": "m1",
        # m0->m2 无共同键（alpha 质量升级不自动同品），直接对比放在同段 m0->m1
        "direct_comparison": ["m0", "m1"],
        "matching": "product_and_quality",
        "decimal_precision": 40,
        "tolerance": 1e-10,
    }
    (OUT / "indices.json").write_text(
        json.dumps(indices, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"synthetic example written under {OUT}")


if __name__ == "__main__":
    main()
