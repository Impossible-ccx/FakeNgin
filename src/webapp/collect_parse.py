"""采集内容解析与规范化：RSS 2.0 / Atom / JSON → 结构化条目。

安全原则：
- XML 拒绝 DTD 与实体定义（配合 1 MiB 体积上限，阻断实体展开攻击）；
  ElementTree 本身不解析外部实体。
- HTML 一律转纯文本：标签全部剥离（含 script/style 的内容），
  实体反转义，空白折叠；外部字段不进入路径、命令或 SQL 拼接。
- 条目数量与字段长度受限；URL 仅保留 http/https，其他协议丢弃。
- 发布时间缺失时明确置空（展示为“时间未知”），不伪造。
"""

import codecs
import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import xml.etree.ElementTree as ET

MAX_ITEMS_DEFAULT = 20
MAX_TITLE_LENGTH = 300
MAX_CONTENT_LENGTH = 5000  # 与消息正文上限一致
TRUNCATION_MARKER = "……（来源正文超长，已截断）"

_XML_DECL_ENCODING = re.compile(rb"^\s*<\?xml[^>]*encoding=[\"']([\w\-]+)[\"']")


class ParseRejected(Exception):
    """响应内容不符合结构要求（类型、编码、格式），整份拒绝。"""


class ItemRejected(Exception):
    """单条目不合规（缺字段、时间非法等），跳过并记录原因。"""


# ------------------------------------------------------------- HTML 转纯文本

class _TextExtractor(HTMLParser):
    """提取纯文本：忽略 script/style 内容，其余标签全部剥离，实体转文本。"""

    _SKIP = {"script", "style", "noscript", "template", "iframe", "object",
             "embed", "svg", "math"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif self._parts and not self._parts[-1].endswith((" ", "\n")):
            self._parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self):
        return _collapse_ws("".join(self._parts))


def _collapse_ws(text):
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(value):
    if not value:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(str(value))
        extractor.close()
    except Exception:
        # 解析失败的兜底：至少把标签剥掉，绝不让原始 HTML 进入存储
        return _collapse_ws(re.sub(r"<[^>]*>", " ", str(value)))
    return extractor.text()


def _strip_controls(text):
    """去掉 C0/C1 控制字符（保留换行与制表符）。"""
    return "".join(ch for ch in text
                   if ch in "\n\t" or ord(ch) >= 32)


# ------------------------------------------------------------- 解码

def decode_body(body, content_type=""):
    match = re.search(r"charset=[\"']?([\w\-]+)", content_type, re.IGNORECASE)
    candidates = []
    if match:
        candidates.append(match.group(1))
    if body.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    decl = _XML_DECL_ENCODING.match(body)
    if decl:
        candidates.append(decl.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for charset in candidates:
        try:
            return body.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ParseRejected("响应编码无法解码（非声明的字符集）")


# ------------------------------------------------------------- RSS / Atom

def _local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _find_text(element, *names):
    """按本地名查找第一个子元素的文本。"""
    wanted = {name.lower() for name in names}
    for child in element:
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _parse_pubdate(value):
    """RSS pubDate（RFC 822）或 Atom 时间（ISO 8601）→ 标准时间串；非法返回 None。"""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        iso = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            raise ItemRejected("发布时间格式无法解析：{}".format(value[:50]))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _parse_xml(text, max_items):
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseRejected("XML 解析失败：{}".format(exc))

    items, rejections = [], []
    if _local_name(root.tag) == "rss":
        channel = root.find("channel")
        entries = list(channel) if channel is not None else []
    elif _local_name(root.tag) == "feed":  # Atom
        entries = list(root)
    else:
        raise ParseRejected("不是可识别的 RSS/Atom 文档（根元素 {}）".format(
            _local_name(root.tag)))

    for entry in entries:
        name = _local_name(entry.tag)
        if name not in ("item", "entry"):
            continue
        if len(items) >= max_items:
            break
        try:
            items.append(_normalize_entry(entry))
        except ItemRejected as exc:
            rejections.append(str(exc))
    return items, rejections


def _normalize_entry(entry):
    title_raw = _find_text(entry, "title")
    link = ""
    for child in entry:
        if _local_name(child.tag) == "link":
            link = child.get("href") or (child.text or "").strip()
            if link:
                break
    external_id = _find_text(entry, "guid", "id")
    pub_raw = _find_text(entry, "pubDate", "published", "updated")
    body_html = _find_text(entry, "encoded") or _find_text(entry, "content")
    if not body_html:
        body_html = _find_text(entry, "description", "summary")

    title = _strip_controls(_collapse_ws(html_to_text(title_raw)))[:MAX_TITLE_LENGTH]
    body = _strip_controls(html_to_text(body_html))
    if body and title and title not in body:
        content = "{}\n{}".format(title, body)
    else:
        content = body or title
    content = _collapse_ws(content)
    if not content:
        raise ItemRejected("标题与正文均为空")
    truncated = False
    if len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
        truncated = True

    published_at = _parse_pubdate(pub_raw)

    # 原文链接仅保留 http/https，其他协议不能成为可点击链接
    parts = (link or "").split(":", 1)
    if link and parts[0].lower() not in ("http", "https"):
        link = ""
    if not link and not external_id:
        external_id = "hash-" + hashlib.sha1(
            (title + content).encode("utf-8")).hexdigest()[:16]

    return {
        "external_id": _strip_controls(external_id or link)[:500],
        "title": title,
        "content": content,
        "link": link[:1000],
        "published_at": published_at or "",
        "truncated": truncated,
    }


# ------------------------------------------------------------- JSON

def _parse_json(text, max_items):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise ParseRejected("JSON 解析失败")
    if not isinstance(data, list):
        raise ParseRejected("JSON 顶层必须是条目数组")
    items = []
    for raw in data[:max_items]:
        if not isinstance(raw, dict):
            raise ParseRejected("JSON 条目必须是对象")
        try:
            items.append(_normalize_json_item(raw))
        except ItemRejected:
            raise ParseRejected("JSON 条目缺少必需字段（title/content）")
    return items


def _normalize_json_item(raw):
    title = _strip_controls(_collapse_ws(str(raw.get("title") or "")))[:MAX_TITLE_LENGTH]
    content = _strip_controls(_collapse_ws(str(raw.get("content") or "")))
    # 与 RSS 条目同规则：标题未包含在正文时前置标题，给检测更多上下文
    if content and title and title not in content:
        content = "{}\n{}".format(title, content)
    if not content:
        content = title
    if not content:
        raise ItemRejected("标题与正文均为空")
    truncated = False
    if len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
        truncated = True
    link = str(raw.get("link") or raw.get("url") or "")
    if link.split(":", 1)[0].lower() not in ("http", "https"):
        link = ""
    external_id = str(raw.get("id") or link or "")
    if not external_id:
        external_id = "hash-" + hashlib.sha1(
            (title + content).encode("utf-8")).hexdigest()[:16]
    return {
        "external_id": external_id[:500],
        "title": title,
        "content": content,
        "link": link[:1000],
        "published_at": _parse_pubdate(str(raw.get("published_at") or "")) or "",
        "truncated": truncated,
    }


# ------------------------------------------------------------- 入口

def parse_items(source, body, content_type=""):
    """解析响应体为条目列表。返回 (items, rejections)。

    整份不合格（编码 / DTD / 格式）抛 ParseRejected；
    单条不合格记入 rejections 跳过。
    """
    fmt = source.get("format", "rss")
    max_items = int(source.get("max_items", MAX_ITEMS_DEFAULT))

    text = decode_body(body, content_type)

    # 禁 DTD / 实体定义：配合体积上限阻断实体展开
    head = text[:4096].lower()
    if "<!doctype" in head or "<!entity" in head:
        raise ParseRejected("文档包含 DTD/实体定义，已拒绝")

    if fmt == "json":
        return _parse_json(text, max_items), []

    # 实际内容与声明类型都要像 XML
    stripped = text.lstrip()
    if not stripped.startswith("<?xml") and not re.match(
            r"<(rss|feed|channel)\b", stripped, re.IGNORECASE):
        raise ParseRejected("响应内容不是 XML（类型声明与内容不符）")
    return _parse_xml(text, max_items)
