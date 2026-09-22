"""CSV 消息/评论导入 CLI。

用法：
    python scripts/import_csv.py <消息csv>
    python scripts/import_csv.py --comments <评论csv>

消息 CSV 列：content, nature, fake_probability, source, publish_time, process_time。
评论 CSV 列：ref, message_content, parent_ref, content, publish_time
（message_content 须与已导入消息正文完全一致；先导入消息再导入评论）。

校验失败时不导入并输出错误清单；重复内容自动跳过并计数。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webapp import newsdata  # noqa: E402

USAGE = "用法：python scripts/import_csv.py <消息csv路径> | --comments <评论csv路径>"


def _report(stats):
    print("导入完成：新增 {} 条，跳过重复 {} 条，错误 {} 条".format(
        stats["imported"], stats["duplicates"], len(stats["errors"])))
    for error in stats["errors"]:
        print("  第 {} 行：{}".format(error["row"], error["error"]))
    return 0 if not stats["errors"] else 1


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--comments":
        path = Path(args[1])
        if not path.exists():
            print("文件不存在：{}".format(path))
            return 1
        try:
            stats = newsdata.import_comments_csv(path)
        except ValueError as exc:
            print(str(exc))
            return 1
        return _report(stats)
    if len(args) != 1 or args[0].startswith("--"):
        print(USAGE)
        return 1
    path = Path(args[0])
    if not path.exists():
        print("文件不存在：{}".format(path))
        return 1
    try:
        stats = newsdata.import_csv(path)
    except ValueError as exc:
        print(str(exc))
        return 1
    return _report(stats)


if __name__ == "__main__":
    raise SystemExit(main())
