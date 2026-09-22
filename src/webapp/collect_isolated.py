"""隔离采集运行器：在受限子进程中执行获取与解析，父进程校验后入库。

安全模型：
- 父进程（Web 应用）持有业务库与模型密钥；子进程只拿到来源 ID、
  服务端来源配置文件与状态目录路径，环境变量为白名单构造——模型
  密钥、FAKENGIN_DATA_DIR、家目录都不会传给子进程；
- 子进程通过 rlimit 限制地址空间（512 MiB）、CPU 时间（60s）、写出
  文件大小（32 MiB）与 core dump，运行在独立进程组，超时可整组终止；
- 子进程输出必须通过 schema 校验（字段类型、数量与长度上限）才会
  被父进程采用；输出文件读取同样有大小上限；
- 临时文件（来源配置、结果、子进程 stderr）放在状态目录下，
  无论成败都清理。
"""

import json
import os
import resource
import signal
import subprocess
import sys
import uuid
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1]

CHILD_TIMEOUT_GRACE = 60.0       # 子进程自身截止之外的终止宽限
OUTPUT_CAP_BYTES = 8 * 1024 * 1024   # 结果文件读取上限（正常远小于此）
MAX_ITEMS = 10000
MAX_ITEM_TEXT = 6000             # 条目字段长度上限（解析层已限制，防御性复核）
STDERR_TAIL_CHARS = 500


class CollectionError(Exception):
    """隔离采集未能产出可用结果（子进程崩溃、超时或输出不合规）。"""


def _child_limits():
    """preexec_fn：独立进程组 + 资源上限（在 fork 后、exec 前执行）。"""
    os.setsid()
    limits = [
        (resource.RLIMIT_AS, 512 * 1024 * 1024),
        (resource.RLIMIT_CPU, 60),
        (resource.RLIMIT_FSIZE, 32 * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    ]
    for which, value in limits:
        resource.setrlimit(which, (value, value))


def _child_env():
    """白名单环境：不继承模型密钥、业务数据目录与家目录。"""
    env = {
        "PYTHONPATH": str(_SRC_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C.UTF-8",
    }
    # TLS 证书定位（虚拟环境有时需要）；测试回环限制（未设置则不传）
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR",
                "FAKENGIN_COLLECT_TEST_LOOPBACK"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _validate_child_output(data):
    """schema 校验子进程输出；不合规一律拒绝（宁可失败不采信）。"""
    if not isinstance(data, dict):
        raise CollectionError("子进程输出不是 JSON 对象")
    status = data.get("status")
    if status not in ("succeeded", "failed"):
        raise CollectionError("子进程输出状态不合规：{!r}".format(status))
    requests = data.get("requests", 0)
    if not isinstance(requests, int) or not 0 <= requests <= 1000:
        requests = 0
    error = data.get("error", "")
    if not isinstance(error, str):
        error = str(error)
    not_modified = data.get("not_modified") is True
    etag = data.get("etag", "")
    last_modified = data.get("last_modified", "")
    if not isinstance(etag, str) or not isinstance(last_modified, str):
        raise CollectionError("子进程输出的条件头字段不合规")
    if len(etag) > 500 or len(last_modified) > 500 or len(error) > 1000:
        raise CollectionError("子进程输出的字段超长")

    raw_items = data.get("items", [])
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        raise CollectionError("子进程输出的条目列表不合规")
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise CollectionError("子进程输出的条目不是对象")
        normalized = {}
        for key in ("external_id", "title", "content", "link", "published_at"):
            value = item.get(key, "")
            if not isinstance(value, str) or len(value) > MAX_ITEM_TEXT:
                raise CollectionError("子进程输出的条目字段不合规（{}）".format(key))
            normalized[key] = value
        if not normalized["external_id"] or not normalized["content"]:
            raise CollectionError("子进程输出的条目缺少必填字段")
        normalized["truncated"] = item.get("truncated") is True
        items.append(normalized)

    raw_rejections = data.get("rejections", [])
    if not isinstance(raw_rejections, list) or len(raw_rejections) > 200:
        raise CollectionError("子进程输出的拒绝列表不合规")
    rejections = [str(r)[:200] for r in raw_rejections
                  if isinstance(r, str)]

    return {
        "status": status,
        "requests": requests,
        "error": error,
        "not_modified": not_modified,
        "etag": etag,
        "last_modified": last_modified,
        "items": items,
        "rejections": rejections,
    }


def run_isolated_collection(source, conditional_headers=None, state_dir=None,
                            min_interval=None, request_timeout=None,
                            task_timeout=None, retry_backoff=None):
    """在受限子进程中执行一次获取+解析，返回校验后的结构化结果。"""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    source_file = state_dir / "source_{}.json".format(token)
    out_file = state_dir / "result_{}.json".format(token)
    err_file = state_dir / "stderr_{}.log".format(token)

    source_file.write_text(
        json.dumps(source, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(str(source_file), 0o600)
    except OSError:
        pass

    cmd = [
        sys.executable, "-u", "-m", "webapp.collect_child",
        "--source-id", str(source["id"]),
        "--source-file", str(source_file),
        "--out", str(out_file),
        "--state-dir", str(state_dir),
    ]
    if min_interval is not None:
        cmd += ["--min-interval", repr(float(min_interval))]
    if request_timeout is not None:
        cmd += ["--request-timeout", repr(float(request_timeout))]
    if task_timeout is not None:
        cmd += ["--task-timeout", repr(float(task_timeout))]
    if retry_backoff is not None:
        cmd += ["--retry-backoff", repr(float(retry_backoff))]
    conditional = conditional_headers or {}
    if conditional.get("If-None-Match"):
        cmd += ["--etag", conditional["If-None-Match"]]
    if conditional.get("If-Modified-Since"):
        cmd += ["--last-modified", conditional["If-Modified-Since"]]
    if os.geteuid() == 0 and os.getenv("FAKENGIN_COLLECT_ALLOW_ROOT") == "1":
        cmd.append("--allow-root")

    try:
        with open(err_file, "wb") as err_fh:
            proc = subprocess.Popen(
                cmd, env=_child_env(), cwd=str(_SRC_ROOT),
                stdout=subprocess.DEVNULL, stderr=err_fh,
                preexec_fn=_child_limits)
        timeout = (task_timeout or 120.0) + CHILD_TIMEOUT_GRACE
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 独立进程组：整组终止，避免残留孙进程
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            raise CollectionError(
                "采集子进程超时（{}s）被终止".format(int(timeout)))
        if returncode != 0:
            tail = ""
            try:
                tail = err_file.read_text(encoding="utf-8",
                                          errors="replace")[-STDERR_TAIL_CHARS:]
            except OSError:
                pass
            raise CollectionError(
                "采集子进程异常退出（code {}）{}".format(
                    returncode, "：{}".format(tail) if tail else ""))
        try:
            raw = out_file.read_bytes()
        except OSError as exc:
            raise CollectionError("采集子进程没有写出结果：{}".format(exc))
        if len(raw) > OUTPUT_CAP_BYTES:
            raise CollectionError("采集结果文件超过读取上限")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CollectionError("采集结果不是有效 JSON：{}".format(exc))
        return _validate_child_output(data)
    finally:
        for path in (source_file, out_file, err_file):
            try:
                path.unlink()
            except OSError:
                pass
