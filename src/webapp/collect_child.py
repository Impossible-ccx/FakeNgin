"""采集子进程入口：在受资源限制的独立进程中执行获取与解析。

由 webapp.collect_isolated 以独立进程启动（python -m webapp.collect_child），
进程边界即安全边界：

- 输入只有来源 ID 与服务端来源配置文件（白名单登记的条目，不是
  任意 URL），外加条件请求头与状态目录路径；
- 环境为白名单构造（PYTHONPATH 等），不含模型密钥、业务数据目录与
  家目录；本模块不导入业务数据库层，没有业务库访问权；
- 输出为结构化 JSON 文件，由父进程按 schema 校验后才入库；
- 网络边界沿用 collect_fetch：allowlist 校验、TLS 证书校验、DNS
  解析后 IP 绑定、私网/回环/元数据地址拒绝；这些安全上限不接受
  命令行覆盖（可传的只有限流等待等时间参数）；
- FAKENGIN_COLLECT_TEST_LOOPBACK=1 时（仅测试环境）额外要求所有连接
  目标为回环地址，保证模拟验收不出网。

资源上限（地址空间/CPU/写出文件大小/进程组）由父进程通过 rlimit
施加。以 root 身份运行会被拒绝，除非显式 --allow-root（仅限开发容器，
由父进程在 FAKENGIN_COLLECT_ALLOW_ROOT=1 时传入）。
"""

import argparse
import ipaddress
import json
import os
import socket
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from webapp import collect_fetch, collect_parse, collect_sources  # noqa: E402


def _install_loopback_guard():
    """测试模式：所有连接目标必须是回环地址（模拟验收不出网）。"""
    real_create = socket.create_connection

    def guarded(address, *args, **kwargs):
        ip = ipaddress.ip_address(address[0])
        if not ip.is_loopback:
            raise OSError("测试子进程拒绝连接非回环地址：{}".format(address[0]))
        return real_create(address, *args, **kwargs)

    socket.create_connection = guarded


def _load_source(source_id, source_file):
    """加载来源条目：优先自身 allowlist 登记，否则读父进程写出的配置文件。

    无论来源如何，条目结构都按同一规则校验；allow_private 只能来自
    服务端配置，不接受 Web 输入。
    """
    entry = None
    try:
        entry = collect_sources.get_source(source_id)
    except ValueError:
        pass
    if entry is None:
        if not source_file:
            raise ValueError("未知消息来源：{}".format(source_id))
        with open(source_file, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
    if not isinstance(entry, dict) or entry.get("id") != source_id:
        raise ValueError("来源配置与来源 ID 不一致")
    for key in ("name", "url", "format"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ValueError("来源配置缺少字段：{}".format(key))
    if entry["format"] not in ("rss", "atom", "json"):
        raise ValueError("来源格式仅支持 rss / atom / json")
    entry.setdefault("https_only", True)
    entry.setdefault("allow_private", False)
    if not isinstance(entry["https_only"], bool) or \
            not isinstance(entry["allow_private"], bool):
        raise ValueError("https_only / allow_private 必须是布尔值")
    return entry


def child_main(argv=None):
    parser = argparse.ArgumentParser(description="采集子进程（由隔离运行器调用）")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-file", default="",
                        help="父进程写出的来源配置 JSON 路径")
    parser.add_argument("--out", required=True, help="结果 JSON 输出路径")
    parser.add_argument("--state-dir", required=True,
                        help="跨进程间隔状态目录（与业务库分离）")
    parser.add_argument("--min-interval", type=float, default=None)
    parser.add_argument("--request-timeout", type=float, default=None)
    parser.add_argument("--task-timeout", type=float, default=None)
    parser.add_argument("--retry-backoff", type=float, default=None)
    parser.add_argument("--etag", default="")
    parser.add_argument("--last-modified", default="")
    parser.add_argument("--allow-root", action="store_true",
                        help="允许以 root 运行（仅开发容器，正式部署拒绝）")
    args = parser.parse_args(argv)

    if os.geteuid() == 0 and not args.allow_root:
        print("采集子进程拒绝以 root 运行；正式部署的应用容器应使用非 root 用户",
              file=sys.stderr)
        return 2
    if os.environ.get("FAKENGIN_COLLECT_TEST_LOOPBACK") == "1":
        _install_loopback_guard()

    def _emit(payload):
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)

    try:
        source = _load_source(args.source_id, args.source_file)
    except (OSError, ValueError) as exc:
        _emit({"status": "failed", "requests": 0, "error": str(exc)[:500],
               "items": [], "rejections": [], "not_modified": False,
               "etag": "", "last_modified": ""})
        return 0

    if args.request_timeout is not None:
        collect_fetch.REQUEST_TOTAL_TIMEOUT = max(0.5, float(args.request_timeout))
    task_timeout = (collect_fetch.TASK_TIMEOUT if args.task_timeout is None
                    else max(1.0, float(args.task_timeout)))
    if args.retry_backoff is not None:
        collect_fetch.RETRY_BACKOFF_SECONDS = max(0.0, float(args.retry_backoff))

    budget = collect_fetch.FetchBudget()
    deadline = time.monotonic() + task_timeout
    gate = collect_fetch.SqliteIntervalGate(args.state_dir,
                                            min_interval=args.min_interval)
    try:
        conditional = {}
        if args.etag:
            conditional["If-None-Match"] = args.etag
        if args.last_modified:
            conditional["If-Modified-Since"] = args.last_modified
        result = collect_fetch.fetch_source(
            source, budget=budget, deadline=deadline,
            conditional_headers=conditional, gate=gate)
        if result.not_modified:
            payload = {"status": "succeeded", "requests": budget.used,
                       "not_modified": True, "items": [], "rejections": [],
                       "etag": result.etag, "last_modified": result.last_modified,
                       "error": ""}
        else:
            # 解析耗时同样受任务截止约束
            if time.monotonic() > deadline:
                raise collect_fetch.FetchError(
                    "任务截止时间已到（解析耗时超出预算）", retryable=False)
            items, rejections = collect_parse.parse_items(source, result.body)
            payload = {"status": "succeeded", "requests": budget.used,
                       "not_modified": False, "items": items,
                       "rejections": [str(r)[:200] for r in rejections[:200]],
                       "etag": result.etag,
                       "last_modified": result.last_modified, "error": ""}
    except Exception as exc:  # 结构化失败：错误摘要回传父进程，不伪造成功
        payload = {"status": "failed", "requests": budget.used,
                   "not_modified": False, "items": [], "rejections": [],
                   "etag": "", "last_modified": "",
                   "error": "{}：{}".format(type(exc).__name__,
                                           str(exc))[:500]}
    finally:
        gate.close()

    _emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(child_main())
