"""用户数据导入与批量检测引用验证；导入不会触发模型调用。"""

import csv
import io
from pathlib import Path
import uuid

import pandas as pd

from checkmodel.ensemble import MAX_MESSAGE_LENGTH

from . import newsdata

MAX_IMPORT_BYTES = 1024 * 1024
MAX_IMPORT_ROWS = 500
MAX_BATCH_ROWS = 100
CONTENT_HEADERS = ("content", "内容", "正文")


def _source(value):
    if not isinstance(value, str):
        raise ValueError("消息来源必须是文本")
    value = value.strip()
    if len(value) > 200:
        raise ValueError("消息来源不能超过 200 个字符")
    return value


def _content(value, row_number):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("第 {} 条消息内容为空".format(row_number))
    value = value.strip()
    if len(value) > MAX_MESSAGE_LENGTH:
        raise ValueError("第 {} 条消息超过 {} 个字符".format(row_number, MAX_MESSAGE_LENGTH))
    return value


def _normalize(data, row_number, source):
    row = {column: "" for column in newsdata.COLUMNS}
    row["content"] = _content(data.get("content"), row_number)
    nature = str(data.get("nature") or "").strip()
    row["nature"] = {"True": "虚假", "False": "真实"}.get(nature, nature)
    if row["nature"] not in newsdata.NATURES:
        row["nature"] = newsdata.DEFAULT_NATURE
    row["source"] = _source(str(data.get("source") or "")) or source or "用户导入"
    row["publish_time"] = str(data.get("publish_time") or "").strip()
    row["process_time"] = newsdata.now_string()
    return row


def _save(rows):
    if not rows:
        raise ValueError("没有可以导入的消息")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError("每次最多导入 {} 条消息".format(MAX_IMPORT_ROWS))
    directory = Path(newsdata.NEWSDATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    filename = "import_{}.csv".format(uuid.uuid4().hex)
    path = directory / filename
    temporary = path.with_suffix(".csv.tmp")
    try:
        pd.DataFrame(rows, columns=newsdata.COLUMNS).to_csv(temporary, index=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"imported_count": len(rows), "filename": filename}


def import_text(text, source=""):
    source = _source(source)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请粘贴需要导入的消息，每行一条")
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("导入内容不能超过 1 MiB")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > MAX_IMPORT_ROWS:
        raise ValueError("每次最多导入 {} 条消息".format(MAX_IMPORT_ROWS))
    rows = [_normalize({"content": line}, index + 1, source) for index, line in enumerate(lines)]
    return _save(rows)


def import_csv(content, source=""):
    source = _source(source)
    if not isinstance(content, bytes) or len(content) > MAX_IMPORT_BYTES:
        raise ValueError("CSV 文件不能超过 1 MiB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("请使用 UTF-8 编码的 CSV 文件") from None
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise ValueError("CSV 不能包含重复列名")
        content_column = next((name for name in CONTENT_HEADERS if name in headers), None)
        if content_column is None:
            raise ValueError("CSV 需要 content、内容或正文列")
        for data in reader:
            if None in data:
                raise ValueError("CSV 存在列数不匹配的行")
            if all(not str(value or "").strip() for value in data.values()):
                continue
            if len(rows) >= MAX_IMPORT_ROWS:
                raise ValueError("每次最多导入 {} 条消息".format(MAX_IMPORT_ROWS))
            data["content"] = data.get(content_column)
            data["source"] = data.get("source") or data.get("来源")
            rows.append(_normalize(data, len(rows) + 1, source))
    except csv.Error:
        raise ValueError("CSV 格式无法解析，请检查引号和分隔符") from None
    return _save(rows)


def resolve_rows(references):
    """先验证全部选择，再返回独立消息快照，避免行号漂移和部分提交。"""
    if not isinstance(references, list) or not 1 <= len(references) <= MAX_BATCH_ROWS:
        raise ValueError("每批请选择 1 到 {} 条消息".format(MAX_BATCH_ROWS))
    tables = {}
    seen = set()
    resolved = []
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("消息选择格式无效，请刷新页面")
        name, index, signature = (reference.get(key) for key in ("file", "row", "signature"))
        if not isinstance(name, str) or not name or "/" in name or "\\" in name:
            raise ValueError("消息表名无效")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("消息行号无效")
        if not isinstance(signature, str) or not signature:
            raise ValueError("缺少消息指纹，请刷新页面")
        key = (name, index)
        if key in seen:
            raise ValueError("不能重复选择同一条消息")
        seen.add(key)
        if name not in tables:
            tables[name] = newsdata.read_table(name)
        table = tables[name]
        if index >= len(table) or newsdata.signature(table.iloc[index]) != signature:
            raise ValueError("所选消息已发生变化，请刷新页面后重新选择")
        resolved.append({
            "file": name, "row": index, "signature": signature,
            "message": _content(table.iloc[index]["content"], len(resolved) + 1),
        })
    return resolved
