"""重置账户密码的命令行入口（忘记密码、口令文件丢失时使用）。

用法：
    python scripts/reset_admin_password.py <用户名> [--generate]

- --generate：生成随机密码并打印一次（请立即用于登录并修改）；
- 否则交互式输入两次新密码（不回显），两次一致才生效。

数据目录与运行中的应用一致（可用 FAKENGIN_DATA_DIR 指定）。
"""

import argparse
import getpass
import secrets
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from webapp import db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="重置 FakeNgin 账户密码")
    parser.add_argument("username", help="要重置密码的用户名")
    parser.add_argument("--generate", action="store_true",
                        help="生成随机密码并打印（默认交互式输入）")
    args = parser.parse_args()

    if db.find_user(args.username) is None:
        print("账户不存在：{}".format(args.username))
        return 1

    if args.generate:
        password = secrets.token_urlsafe(12)
    else:
        first = getpass.getpass("新密码（至少 8 位）：")
        if len(first) < 8:
            print("密码至少 8 位")
            return 1
        if first != getpass.getpass("再次输入新密码："):
            print("两次输入不一致")
            return 1
        password = first

    db.update_password(args.username, password)
    if args.generate:
        print("已重置 {} 的密码，新密码：{}".format(args.username, password))
        print("请立即登录并修改，不要把本输出保存到共享位置。")
    else:
        print("已重置 {} 的密码。".format(args.username))
    return 0


if __name__ == "__main__":
    sys.exit(main())
