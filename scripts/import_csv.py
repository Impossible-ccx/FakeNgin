"""CSV 消息导入 CLI。

用法：
    python scripts/import_csv.py <csv文件路径>

校验失败时不导入并输出错误清单；重复正文自动跳过并计数。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webapp import newsdata  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("用法：python scripts/import_csv.py <csv文件路径>")
        return 1
    path = Path(sys.argv[1])
    if not path.exists():
        print("文件不存在：{}".format(path))
        return 1

    stats = newsdata.import_csv(path)
    print("导入完成：新增 {} 条，跳过重复 {} 条，错误 {} 条".format(
        stats["imported"], stats["duplicates"], len(stats["errors"])))
    for error in stats["errors"]:
        print("  第 {} 行：{}".format(error["row"], error["error"]))
    return 0 if not stats["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
