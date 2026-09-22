"""在线采集的受限 HTTP 获取器。

设计目标：把“能访问什么”限制到服务端配置的来源，其余一律拒绝。

- 不提供任意 URL 入口：调用方只能传入 collect_sources.SOURCES 的条目；
  重定向默认关闭，开启时每跳也必须与来源同协议、同域名、同端口。
- 域名预校验 + 连接绑定（IP pinning）：先解析 DNS 并校验全部地址，
  再直接连接已校验的 IP，杜绝检查与连接之间的 DNS 重绑定竞态。
- 地址黑名单覆盖回环、私网、链路本地、组播、保留与未指定地址，
  含 IPv4 映射 IPv6（::ffff:x.x.x.x 按对应 IPv4 复查）。
- 限流与预算：同来源请求起始间隔、每次任务的请求上限、全局截止时间。
- 体积与时间上限：传输 1 MiB（流式累计实际字节），解压输出按剩余预算
  分批取出（峰值内存与预算同量级，压缩炸弹在超限时立即拒绝），
  连接 5s / 读取 10s / 单请求总 20s 截止（DNS 与连接阶段同样受
  任务截止约束）。
- 重试：网络错误与 5xx 最多重试 1 次（带退避，计入预算）；
  429 尊重 Retry-After（上限 60s，超预算则中止）；403 本轮停用不重试。
- 不使用系统代理配置，TLS 证书校验保持开启。
"""

import http.client
import ipaddress
import logging
import math
import socket
import sqlite3
import ssl
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("fakengin.collect")

# ------------------------------------------------------------- 边界参数
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
REQUEST_TOTAL_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 1024 * 1024        # 传输（压缩后）体积上限
MAX_DECOMPRESSED_BYTES = 1024 * 1024    # 解压后体积上限
TASK_TIMEOUT = 120.0                    # 单次采集任务总截止
MAX_REQUESTS_PER_TASK = 10              # 单来源每次任务请求上限
MIN_INTERVAL_SECONDS = 5.0              # 同来源请求起始最小间隔
RETRY_BACKOFF_SECONDS = 2.0
MAX_ATTEMPTS = 2                        # 首次 + 重试 1 次
MAX_REDIRECTS = 2
# 429 处理：服务端未给出 Retry-After 时按 60s 保守假设；
# 给出时不缩短为更早重试（最多按 24h 封顶防溢出值）。
RETRY_AFTER_DEFAULT = 60.0
RETRY_AFTER_SANITY_MAX = 86400.0
# 跨进程全局预算：全部来源合计每小时最多请求次数（重试也计入）。
GLOBAL_WINDOW_SECONDS = 3600.0
GLOBAL_MAX_REQUESTS = 120
READ_CHUNK = 8192
USER_AGENT = "FakeNgin-CourseBot/1.0 (course project rumor collector)"

XML_CONTENT_TYPES = {
    "application/rss+xml", "application/atom+xml", "application/xml",
    "text/xml", "application/rss", "text/rss",
}
JSON_CONTENT_TYPES = {"application/json", "text/json"}


class FetchError(Exception):
    """获取失败（网络、超时、协议错误）。retryable 标记是否按策略重试。"""

    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class FetchRejected(FetchError):
    """明确拒绝（安全边界、来源限制或体积上限）。不可重试。"""


class FetchBudget:
    """请求预算：每次任务最多消耗这么多次请求（重试也计入）。"""

    def __init__(self, max_requests=MAX_REQUESTS_PER_TASK):
        self.max_requests = max_requests
        self.used = 0

    def consume(self):
        if self.used >= self.max_requests:
            raise FetchRejected("请求预算已用完（单次任务最多 {} 次请求）".format(
                self.max_requests))
        self.used += 1


class FetchResult:
    """一次成功获取的结果。"""

    def __init__(self, status, body, etag="", last_modified="", requests=1,
                 not_modified=False):
        self.status = status
        self.body = body                # 已解压的 bytes
        self.etag = etag
        self.last_modified = last_modified
        self.requests = requests        # 本次获取实际消耗的请求数
        self.not_modified = not_modified


# ------------------------------------------------------------- 地址校验

def _blocked_ip(ip):
    """受限地址判定：非全局可路由地址一律拒绝。"""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_host_addresses(host, port, allow_private=False):
    """解析域名并校验全部地址；返回第一个可用 IP 字符串。

    任意一条解析结果命中受限地址就整体拒绝（防轮询式重绑定）。
    allow_private 仅用于本地测试源（模拟服务在 127.0.0.1 上）。
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError("域名解析失败：{}".format(type(exc).__name__)) from exc
    for info in infos:
        sockaddr = info[4]
        ip = ipaddress.ip_address(sockaddr[0])
        if _blocked_ip(ip) and not allow_private:
            raise FetchRejected("目标地址属于受限地址段（{}），已拒绝".format(
                _classify_ip(ip)))
    if not infos:
        raise FetchError("域名没有可用解析结果")
    return infos[0][4][0]


def _classify_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "回环地址"
    if ip.is_link_local:
        return "链路本地地址（含云元数据地址）"
    if ip.is_private:
        return "私网地址"
    if ip.is_multicast:
        return "组播地址"
    if ip.is_reserved:
        return "保留地址"
    if ip.is_unspecified:
        return "未指定地址"
    return "受限地址"


def parse_source_url(url, https_only=True):
    """校验 URL 结构；返回 (scheme, host, port, path)。

    拒绝：非 http/https、带用户名密码、无主机。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchRejected("仅允许 http/https 协议（当前：{}）".format(parts.scheme or "空"))
    if https_only and parts.scheme != "https":
        raise FetchRejected("真实来源仅允许 HTTPS")
    if parts.username or parts.password:
        raise FetchRejected("URL 中不允许携带用户名密码")
    host = parts.hostname
    if not host:
        raise FetchRejected("URL 缺少主机名")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, path


# ------------------------------------------------------------- 单次请求

def _open_connection(scheme, host, port, ip, deadline):
    """连接到已校验的 IP（连接绑定），HTTPS 时按原主机名做 SNI 与证书校验。

    socket 超时取“连接/读取超时”与“任务剩余时间”的较小值：getresponse
    之后 socket 对象会被 http.client 标记关闭、无法再调整超时，因此必须
    在请求前设定好；慢速持续响应最多阻塞到该值，总截止在读取间检查。
    """
    remaining = max(0.5, deadline - time.monotonic())
    sock = socket.create_connection((ip, port), timeout=min(CONNECT_TIMEOUT, remaining))
    sock.settimeout(min(READ_TIMEOUT, REQUEST_TOTAL_TIMEOUT, remaining))
    if scheme == "https":
        context = ssl.create_default_context()  # 校验系统 CA 与主机名
        sock = context.wrap_socket(sock, server_hostname=host)
    conn = http.client.HTTPConnection(host, port, timeout=READ_TIMEOUT)
    conn.sock = sock
    return conn


def _read_capped(resp, deadline):
    """流式读取响应体，累计实际字节数并执行体积与时间上限。

    单次 read 的阻塞上限由 socket 超时保证（读取超时与总截止取小），
    总截止时间在每次读取之间检查；读取超时但未到总截止按网络错误处理。
    """
    chunks = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise FetchError("单请求总超时（{}s）".format(REQUEST_TOTAL_TIMEOUT),
                             retryable=False)
        try:
            # read1：单次底层读取即返回可用字节；普通 read 会一直阻塞到
            # 凑满请求的字节数，持续滴流响应可借此绕过读取间的截止检查
            chunk = resp.read1(READ_CHUNK)
        except socket.timeout:
            if time.monotonic() >= deadline:
                raise FetchError(
                    "单请求总超时（{}s）".format(REQUEST_TOTAL_TIMEOUT),
                    retryable=False) from None
            raise  # 未到总截止的读取超时：按网络错误处理
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise FetchRejected("响应传输体积超过 1 MiB 上限")
        chunks.append(chunk)
    return chunks, total


def _gunzip_capped(chunks, deadline=None):
    """流式解压 gzip，解压后体积限制在 1 MiB 且峰值内存与预算同量级。

    - decompress 的 max_length 参数按剩余预算分批取出输出，剩余输入经
      unconsumed_tail 循环处理——单次调用不会把整段输入完整解压进内存，
      因此压缩炸弹在超预算时立即拒绝，而不是先完整解压再截断；
    - 流结束（eof）后仍有输入视为尾随数据：HTTP 传输编码 gzip 应只有
      一个成员，出现尾随数据（含拼接的第二个 gzip 成员）一律拒绝；
    - 输入耗尽但未到流结束视为截断，按可重试的网络错误处理；
    - deadline 在每个输出批次间检查，慢速解压不越过任务截止时间。
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = []
    total = 0
    for chunk in chunks:
        pending = chunk
        while pending:
            if deadline is not None and time.monotonic() > deadline:
                raise FetchError("解压超时（任务截止时间已到）", retryable=False)
            # 多取 1 字节探测超限；max_length=0 表示无限制，必须保证 ≥ 1
            budget = MAX_DECOMPRESSED_BYTES - total + 1
            data = decompressor.decompress(pending, budget)
            total += len(data)
            if total > MAX_DECOMPRESSED_BYTES:
                raise FetchRejected("解压后体积超过 1 MiB 上限")
            if data:
                out.append(data)
            if decompressor.eof:
                break
            pending = decompressor.unconsumed_tail
    if not decompressor.eof:
        raise FetchError("gzip 流被截断（未到流结束）")
    if decompressor.unused_data:
        raise FetchRejected("gzip 流结束后仍有尾随数据，已拒绝")
    return b"".join(out)


def _single_request(scheme, host, port, path, ip, headers,
                    allowed_content_types, deadline):
    """发起到已校验目标的一次请求，处理响应头与编码。"""
    deadline_read = min(time.monotonic() + REQUEST_TOTAL_TIMEOUT, deadline)
    conn = _open_connection(scheme, host, port, ip, deadline_read)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        if status == 304:
            return FetchResult(status, b"", requests=1, not_modified=True)
        if status in (301, 302, 303, 307, 308):
            raise FetchRejected(
                "来源返回重定向（{}），已按默认策略拒绝跟进".format(status))
        if status == 429:
            retry_after = _parse_retry_after(resp.getheader("Retry-After"))
            raise _TooManyRequests(retry_after)
        if status == 403:
            raise FetchRejected("来源返回 403，本轮停用该来源（不尝试绕过）")
        if status >= 500:
            raise FetchError("来源返回服务器错误（HTTP {}）".format(status),
                             retryable=True)
        if status != 200:
            # 其余 4xx（如 404/410）重试无意义
            raise FetchError("来源返回非成功状态（HTTP {}）".format(status),
                             retryable=False)

        # 类型声明检查：只接受适配器明确支持的内容类型
        # （实际内容是否为 XML/JSON 由解析层再检查一次）
        declared = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if declared not in allowed_content_types:
            raise FetchRejected(
                "响应内容类型不在允许列表（声明：{}，允许：{}）".format(
                    declared or "未声明", "、".join(sorted(allowed_content_types))))

        content_type = resp.getheader("Content-Type", "")
        encoding = (resp.getheader("Content-Encoding") or "identity").strip().lower()
        chunks, _size = _read_capped(resp, deadline_read)
    finally:
        conn.close()

    if encoding in ("", "identity"):
        body = b"".join(chunks)
    elif encoding == "gzip":
        body = _gunzip_capped(chunks, deadline_read)
    else:
        raise FetchRejected("不支持的传输编码（{}），已拒绝".format(encoding))

    return FetchResult(
        status, body,
        etag=resp.getheader("ETag") or "",
        last_modified=resp.getheader("Last-Modified") or "",
        requests=1,
    )


class _TooManyRequests(FetchError):
    def __init__(self, retry_after):
        super().__init__("来源返回 429 限流")
        self.retry_after = retry_after


def _parse_retry_after(value):
    """解析 Retry-After：不缩短服务端要求的等待（仅按 24h 封顶防溢出值）。"""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_SANITY_MAX)


# ------------------------------------------------------------- 获取入口

_interval_lock = threading.Lock()
_next_allowed_at = {}  # source_id -> time.monotonic()（进程内实现的状态）


class IntervalGate:
    """同来源请求间隔门。

    reserve：每次请求前等待到允许时刻并登记下一次；
    postpone：来源要求延后（如 429 Retry-After）时推迟允许时刻。
    """

    def reserve(self, source_id, deadline):
        raise NotImplementedError

    def postpone(self, source_id, seconds):
        raise NotImplementedError

    def next_wait_seconds(self, source_id):
        raise NotImplementedError


class InProcessIntervalGate(IntervalGate):
    """进程内实现（fetch_source 的默认门）：单进程部署与直接调用使用。

    多进程部署下各 worker 不共享此状态；正式采集经子进程执行时
    传入 SqliteIntervalGate，跨进程生效。
    """

    def reserve(self, source_id, deadline):
        with _interval_lock:
            next_allowed = _next_allowed_at.get(source_id, 0.0)
            wait = next_allowed - time.monotonic()
            if wait > 0:
                if time.monotonic() + wait > deadline:
                    raise FetchRejected(
                        "同来源请求间隔不足，且等待会超过任务截止时间")
                time.sleep(wait)
            _next_allowed_at[source_id] = time.monotonic() + MIN_INTERVAL_SECONDS

    def postpone(self, source_id, seconds):
        with _interval_lock:
            now = time.monotonic()
            next_allowed = max(_next_allowed_at.get(source_id, now),
                               now + max(0.0, float(seconds)))
            _next_allowed_at[source_id] = next_allowed

    def next_wait_seconds(self, source_id):
        with _interval_lock:
            return max(0.0, _next_allowed_at.get(source_id, time.monotonic())
                       - time.monotonic())


_default_gate_instance = InProcessIntervalGate()


class SqliteIntervalGate(IntervalGate):
    """跨进程实现：同来源间隔与全局请求预算持久化在独立 SQLite 状态库。

    状态库与业务库分离（位于专门的 collect_state 目录），采集子进程
    因此不需要业务数据库的任何访问权。时间基准为 time.time()（跨进程
    可比；秒级间隔下 NTP 微调的影响可忽略）。
    """

    def __init__(self, state_dir, min_interval=None,
                 global_max=GLOBAL_MAX_REQUESTS,
                 global_window=GLOBAL_WINDOW_SECONDS):
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = (MIN_INTERVAL_SECONDS if min_interval is None
                             else max(0.0, float(min_interval)))
        self.global_max = int(global_max)
        self.global_window = float(global_window)
        self._conn = sqlite3.connect(str(state_dir / "gate.db"), timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # 手动事务
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS source_interval ("
                "source_id TEXT PRIMARY KEY, next_allowed REAL NOT NULL)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS global_window ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "start REAL NOT NULL, count INTEGER NOT NULL)")
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def close(self):
        self._conn.close()

    def _check_and_bump_global(self, now):
        row = self._conn.execute(
            "SELECT start, count FROM global_window WHERE id = 1").fetchone()
        if row is None or now - row["start"] >= self.global_window:
            self._conn.execute(
                "INSERT OR REPLACE INTO global_window (id, start, count) "
                "VALUES (1, ?, 1)", (now,))
            return
        if row["count"] >= self.global_max:
            raise FetchRejected(
                "全局采集请求预算已用完（{} 秒内 {} 次上限），请稍后再试".format(
                    int(self.global_window), self.global_max))
        self._conn.execute(
            "UPDATE global_window SET count = count + 1 WHERE id = 1")

    def reserve(self, source_id, deadline):
        while True:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                row = self._conn.execute(
                    "SELECT next_allowed FROM source_interval WHERE source_id = ?",
                    (source_id,)).fetchone()
                wait = max(0.0, row["next_allowed"] - now) if row else 0.0
                if wait <= 0:
                    self._check_and_bump_global(now)
                    self._conn.execute(
                        "INSERT OR REPLACE INTO source_interval "
                        "(source_id, next_allowed) VALUES (?, ?)",
                        (source_id, now + self.min_interval))
                    self._conn.execute("COMMIT")
                    return
                self._conn.execute("ROLLBACK")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            # 需要等待：在截止时间内分段睡眠，醒来后重读（其他进程可能再推迟）
            if time.monotonic() + wait > deadline:
                raise FetchRejected(
                    "同来源请求间隔不足，且等待会超过任务截止时间")
            time.sleep(min(wait, 1.0) + 0.01)

    def postpone(self, source_id, seconds):
        seconds = max(0.0, float(seconds))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            row = self._conn.execute(
                "SELECT next_allowed FROM source_interval WHERE source_id = ?",
                (source_id,)).fetchone()
            next_allowed = max(row["next_allowed"] if row else now, now + seconds)
            self._conn.execute(
                "INSERT OR REPLACE INTO source_interval "
                "(source_id, next_allowed) VALUES (?, ?)",
                (source_id, next_allowed))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def next_wait_seconds(self, source_id):
        row = self._conn.execute(
            "SELECT next_allowed FROM source_interval WHERE source_id = ?",
            (source_id,)).fetchone()
        if row is None:
            return 0.0
        return max(0.0, row["next_allowed"] - time.time())


def _wait_source_interval(source_id, deadline):
    """兼容入口：经默认进程内门执行间隔等待。"""
    _default_gate_instance.reserve(source_id, deadline)


def fetch_source(source, budget=None, deadline=None, conditional_headers=None,
                 gate=None):
    """按来源配置执行受限获取，返回 FetchResult。

    source：collect_sources.SOURCES 的条目（含 url、https_only、
      allow_private、max_items 等字段）。
    budget / deadline 为空时使用默认任务预算与截止时间。
    conditional_headers：可选的 {If-None-Match, If-Modified-Since}。
    gate：同来源间隔门；默认进程内实现，跨进程部署传 SqliteIntervalGate。
    """
    budget = budget or FetchBudget()
    deadline = deadline or (time.monotonic() + TASK_TIMEOUT)
    gate = gate if gate is not None else _default_gate_instance
    https_only = source.get("https_only", True)
    allow_private = source.get("allow_private", False)
    scheme, host, port, path = parse_source_url(source["url"], https_only)

    headers = {
        # netloc 已在 parse_source_url 拒绝过用户信息，可直接作 Host
        "Host": urlsplit(source["url"]).netloc,
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }
    if conditional_headers:
        headers.update(conditional_headers)
    # 内容类型白名单随适配器格式走：rss/atom → XML 家族，json → JSON 家族
    allowed_types = (JSON_CONTENT_TYPES if source.get("format") == "json"
                     else XML_CONTENT_TYPES)

    attempts = 0
    while True:
        if time.monotonic() > deadline:
            raise FetchError("任务截止时间已到")
        gate.reserve(source["id"], deadline)
        budget.consume()
        try:
            # DNS 解析无法从外部中断，只在前后检查截止；解析耗时计入任务预算
            ip = validate_host_addresses(host, port, allow_private)
            if time.monotonic() > deadline:
                raise FetchError("任务截止时间已到（DNS 解析耗时超出预算）")
            result = _single_request(scheme, host, port, path, ip, headers,
                                     allowed_types, deadline)
        except _TooManyRequests as exc:
            # 服务端要求的等待不被缩短：先推迟该来源的允许时刻
            # （跨进程可见），再决定本任务是否等得起。
            server_wait = (exc.retry_after if exc.retry_after is not None
                           else RETRY_AFTER_DEFAULT)
            gate.postpone(source["id"], server_wait)
            if attempts >= 1 or time.monotonic() + server_wait > deadline:
                raise FetchError(
                    "来源限流（429），服务端要求 {:.0f} 秒后再试".format(
                        server_wait)) from exc
            logger.info("来源 %s 返回 429，按 Retry-After 等待 %.0fs",
                        source["id"], server_wait)
            time.sleep(server_wait)
            attempts += 1
            continue
        except FetchRejected:
            raise
        except (FetchError, OSError, http.client.HTTPException) as exc:
            # 网络错误 / 5xx：最多重试 1 次，退避计入预算；4xx 不重试
            retryable = (isinstance(exc, (OSError, http.client.HTTPException))
                         or getattr(exc, "retryable", True))
            if attempts >= 1 or not retryable:
                if isinstance(exc, FetchError):
                    raise
                raise FetchError("网络错误：{}：{}".format(
                    type(exc).__name__, str(exc)[:200])) from exc
            attempts += 1
            wait = min(RETRY_BACKOFF_SECONDS, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                raise FetchError("任务截止时间已到") from exc
            time.sleep(wait)
            continue
        return result


def next_wait_seconds(source_id, state_dir=None):
    """该来源距下次可请求的等待秒数。

    state_dir 提供时读跨进程状态库（正式采集的间隔状态在子进程中维护），
    否则退回进程内状态（直接调用 fetch_source 的场景）。
    """
    if state_dir is not None:
        gate_db = Path(state_dir) / "gate.db"
        if gate_db.exists():
            gate = SqliteIntervalGate(state_dir)
            try:
                return gate.next_wait_seconds(source_id)
            finally:
                gate.close()
        return 0.0
    return _default_gate_instance.next_wait_seconds(source_id)

