"""隔离模式（python -I）运行入口。

fixtures 中的文件是不可信输入：-I 会忽略 PYTHONPATH 且不把 cwd 放入 sys.path，
因此这里由运行器自身（位于仓库根，路径来自 sys.argv[0]）把仓库根加入 sys.path，
再调用 src.fisher_index.main。不依赖任何环境变量，也不解释输入目录中的 .py 文件。

用法：
    python -I run_fisher_index.py fixtures/trades.csv fixtures/products.json \
        fixtures/indices.json --output .workspace-output/fisher_report.json
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fisher_index import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
