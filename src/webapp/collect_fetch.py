"""在线采集的受限 HTTP 获取器。

设计目标：把“能访问什么”限制到服务端配置的来源，其余一律拒绝。

- 不提供任意 URL 入口：调用方只能传入 collect_sources.SOURCES 的条目；
  重定向默认关闭，开启时每跳也必须与来源同协议、同域名、同端口。
- 域名预校验 + 连接绑定（IP pinning）：先解析 DNS 并校验全部地址，
  再直接连接已校验的 IP，杜绝检查与连接之间的 DNS 重绑定竞态。
- 地址黑名单覆盖回环、私网、链路本地、组播、保留与未指定地址，
  含 IPv4 映射 IPv6（::ffff:x.x.x.x 按对应 IPv4 复查）。
- 限流与预算：同来源请求起始间隔、每次任务的请求上限、全局截止时间。
- 体积与时间上限：压缩与解压后各 1 MiB（流式累计实际字节），
  连接 5s / 读取 10s / 单请求总 20s 截止。
- 重试：网络错误与 5xx 最多重试 1 次（带退避，计入预算）；
  429 尊重 Retry-After（上限 60s，超预算则中止）；403 本轮停用不重试。
- 不使用系统代理配置，TLS 证书校验保持开启。
"""

import http.client
import ipaddress
import logging
import socket
import ssl
import threading
import time
import zlib
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
RETRY_AFTER_CAP = 60.0
READ_CHUNK = 8192
USER_AGENT = "FakeNgin-CourseBot/1.0 (course project rumor collector)"

XML_CONTENT_TYPES = {
    "application/rss+xml", "application/atom+xml", "application/xml",
    "text/xml", "application/rss", "text/rss",
}
JSON_CONTENT_TYPES = {"application/json", "text/json"}

_interval_lock = threading.Lock()
_next_allowed_at = {}  # source_id -> time.monotonic()


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

def _open_connection(scheme, host, port, ip):
    """连接到已校验的 IP（连接绑定），HTTPS 时按原主机名做 SNI 与证书校验。

    socket 超时取“读取超时”与“单请求总截止”的较小值：getresponse 之后
    socket 对象会被 http.client 标记关闭、无法再调整超时，因此必须在
    请求前设定好；慢速持续响应最多阻塞到该值，总截止在读取间检查。
    """
    sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
    sock.settimeout(min(READ_TIMEOUT, REQUEST_TOTAL_TIMEOUT))
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


def _gunzip_capped(chunks):
    """流式解压 gzip，解压后体积同样限制在 1 MiB。"""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = []
    total = 0
    for chunk in chunks:
        data = decompressor.decompress(chunk)
        total += len(data)
        if total > MAX_DECOMPRESSED_BYTES:
            raise FetchRejected("解压后体积超过 1 MiB 上限")
        out.append(data)
    return b"".join(out)


def _single_request(scheme, host, port, path, ip, headers,
                    allowed_content_types):
    """发起到已校验目标的一次请求，处理响应头与编码。"""
    deadline_read = time.monotonic() + REQUEST_TOTAL_TIMEOUT
    conn = _open_connection(scheme, host, port, ip)
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
        body = _gunzip_capped(chunks)
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
    if not value:
        return None
    try:
        return max(0.0, min(RETRY_AFTER_CAP, float(value)))
    except ValueError:
        return None


# ------------------------------------------------------------- 获取入口

def _wait_source_interval(source_id, deadline):
    """同来源请求起始间隔限流：必要时等待（不超过截止时间）。"""
    with _interval_lock:
        next_allowed = _next_allowed_at.get(source_id, 0.0)
        wait = next_allowed - time.monotonic()
        if wait > 0:
            if time.monotonic() + wait > deadline:
                raise FetchRejected("同来源请求间隔不足，且等待会超过任务截止时间")
            time.sleep(wait)
        _next_allowed_at[source_id] = time.monotonic() + MIN_INTERVAL_SECONDS


def fetch_source(source, budget=None, deadline=None, conditional_headers=None):
    """按来源配置执行受限获取，返回 FetchResult。

    source：collect_sources.SOURCES 的条目（含 url、https_only、
    allow_private、max_items 等字段）。
    budget / deadline 为空时使用默认任务预算与截止时间。
    conditional_headers：可选的 {If-None-Match, If-Modified-Since}。
    """
    budget = budget or FetchBudget()
    deadline = deadline or (time.monotonic() + TASK_TIMEOUT)
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
        _wait_source_interval(source["id"], deadline)
        budget.consume()
        try:
            ip = validate_host_addresses(host, port, allow_private)
            result = _single_request(scheme, host, port, path, ip, headers,
                                     allowed_types)
        except _TooManyRequests as exc:
            retry_after = exc.retry_after if exc.retry_after is not None else RETRY_AFTER_CAP
            if (attempts >= 1 or time.monotonic() + retry_after > deadline):
                raise FetchError("来源限流（429），建议 {} 秒后再试".format(
                    int(retry_after) + 1)) from exc
            logger.info("来源 %s 返回 429，按 Retry-After 等待 %.0fs",
                        source["id"], retry_after)
            time.sleep(retry_after)
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
                raise FetchError("网络错误：{}".format(type(exc).__name__)) from exc
            attempts += 1
            wait = min(RETRY_BACKOFF_SECONDS, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                raise FetchError("任务截止时间已到") from exc
            time.sleep(wait)
            continue
        return result


def next_allowed_time(source_id):
    """该来源下一次可发起请求的时间（time.monotonic 基准）；无记录返回当前时间。"""
    with _interval_lock:
        return _next_allowed_at.get(source_id, time.monotonic())

