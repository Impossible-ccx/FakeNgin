"""CSV 消息导出 CLI。

用法：
    python scripts/export_csv.py <输出csv路径>
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webapp import newsdata  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("用法：python scripts/export_csv.py <输出csv路径>")
        return 1
    path = Path(sys.argv[1])
    if path.exists():
        print("输出文件已存在，为避免覆盖请先删除或换名：{}".format(path))
        return 1

    count = newsdata.export_csv(path)
    print("导出完成：{} 条消息 → {}".format(count, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
