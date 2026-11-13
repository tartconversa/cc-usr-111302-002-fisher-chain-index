# 数据产品价格与交易量的链式指数

本项目保存虚构数据共享及产品运营的合成表格与 Python 读取/指数核算工具。**不执行统计推断、需求模型训练、优化、估计或校准**；所有指数都是确定性的固定集合核算。所有地区、机构、产品与人数均为合成。

## 字段与单位

period 是有序月份，product 及 quality 共同标识可比品；p 和 q 为原单位价格和数量。products 内 to_base_quantity 表示原数量转标准数量倍率，标准单位价格相应除倍率（标准支出 p_base*q_base 与原始支出恒等）。

material-index.json 记录全部输入内容哈希；fixtures 保存原始资料与配置，计算输出放 `.workspace-output/`（已 gitignore），原始资料不应被覆盖。

## 指数口径（src/fisher_index.py）

- **可比键 = (product, quality)**。同名产品的质量升级产生新键，不自动当作同品；进入/退出在覆盖变化中单列。
- 单位先按显式换算统一，再做任何加总。
- 相邻期主指数只使用两期共同存在、两期价格严格为正、数量非负的产品；共同集合中至少一个量权和为正，否则该链接标记 `not_computable` 并写明原因，**绝不填 0**。
- Laspeyres 价格比 = Σ(p1·q0)/Σ(p0·q0)；Paasche 价格比 = Σ(p1·q1)/Σ(p0·q1)；Fisher = 两者几何均值。数量指数由价格与数量对称交换得到。
- 分母为 0、价格缺失（空字符串区别于 0）时对应指数输出 `null` 与原因码。
- 因子反转核对：Fisher 价格 × Fisher 数量 必须等于**同一共同集合**的支出比；市场总支出只用于覆盖率，不作核对分母。
- 链式值按相邻期链接逐期相乘；缺期形成链断裂，报告 `incomparable_intervals`，不跨缺期硬接。基期重定仅除以同一链段在新基期的值。
- 直接双边指数与链式指数可能不同，漂移（drift）原样保留。反向链接与正向链接互为倒数（时间反转检验）。
- 匹配集合指数只代表匹配集合，不是完整市场价格指数（报告 `scope_notice` 明示）。
- 全部金额使用 Decimal，精度取 indices.json 的 `decimal_precision`，平方根误差按 `tolerance` 容差核对；每个无方向期间对的集合摘要由 `LinkCache` 缓存一次。

## 运行

Python 3.11 以上，标准库即可，不需第三方依赖。fixtures 是不可信输入，推荐隔离模式运行（`-I`，忽略 PYTHONPATH 且不把 cwd 放入 sys.path）：

    # 读取器（既有，只解析）
    python -I -m json.tool fixtures/indices.json
    python -m src.index_reader fixtures/trades.csv

    # Fisher 指数报告
    python -I run_fisher_index.py fixtures/trades.csv fixtures/products.json \
        fixtures/indices.json --output .workspace-output/fisher_report.json

    # 综合合成样例：链漂移、产品退出、质量版本、单位换算、缺期断链、重复修订
    python -I scripts/make_synthetic_example.py
    python -I run_fisher_index.py synthetic_example/trades.csv \
        synthetic_example/products.json synthetic_example/indices.json \
        --output .workspace-output/synthetic_report.json

    # 测试（隔离模式需用 -t 指定顶层目录，普通模式可省略）
    python -I -m unittest discover -s tests -t . -v
    python -m unittest discover -s tests -v
    python -m compileall -q src tests

`synthetic_example/` 由脚本生成、与受哈希保护的 `fixtures/` 分离；修订行通过 `revision` 列取最大值覆盖，同 revision 重复行报错。

## 报告结构

- `links`：每个相邻链接的共同集合、被排除键及原因、进入/退出、四个交叉权重和、市场/匹配支出与覆盖率、Laspeyres/Paasche/Fisher 价格与数量、因子反转核对。
- `reverse_links`：正反向倒数核对。
- `chain`：链段（段内基期=1 的链式价格、数量、支出比及逐期核对）、断裂点、不可比区间。
- `rebased`：同链段重定基期结果。
- `direct_vs_chain`：直接双边与链式的漂移比较；不同链段时不给链式值。
- `coverage_changes`：逐期进入/退出键与同名质量版本替换。
- `cached_link_summaries`：缓存的无方向期间对集合摘要。

## 固定容器

    docker build -t fisher-chain-index:local .
    docker run --rm fisher-chain-index:local

容器镜像以完整 digest 固定，默认仅检查输入契约；实现新增分析功能所需依赖由开发者明确登记（当前仍无第三方依赖）。
