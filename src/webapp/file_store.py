"""小型 CSV 文件存储：字段展开、原子写入、变化失效缓存与进程间写锁。

每个文件使用 path,type,value 三列；嵌套字段用 ~0/~1 转义的路径表示，
不在单元格里存 JSON。空字典、空列表及标量类型都能完整往返。
"""

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import csv
import hashlib
import io
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from weakref import WeakValueDictionary


MAX_CACHED_FILES = 256
_cache = OrderedDict()
_cache_lock = threading.RLock()
_lock_registry = WeakValueDictionary()
_registry_lock = threading.Lock()
_held = threading.local()
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
# 旧报告可能含长原文；解除 csv 默认 128 KiB 单元格限制，兼容 Windows C long。
csv.field_size_limit(max(csv.field_size_limit(), 2 ** 31 - 1))


def uuid_path(directory, identifier):
    """仅接受规范 UUID；用户输入不能指定文件夹、后缀或符号链接。"""
    if not isinstance(identifier, str) or not _UUID.fullmatch(identifier):
        raise ValueError("Invalid record identifier")
    path = Path(directory).resolve() / (identifier + ".csv")
    if path.is_symlink():
        raise ValueError("Record path must not be a symbolic link")
    return path


def key_path(directory, key):
    """复合数据引用编码成固定散列文件名，原始字段不会参与路径拼接。"""
    digest = hashlib.sha256(_encode_csv(key)).hexdigest()
    path = Path(directory).resolve() / (digest + ".csv")
    if path.is_symlink():
        raise ValueError("Record path must not be a symbolic link")
    return path


def _fingerprint(path):
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def _remember(key, fingerprint, value):
    with _cache_lock:
        _cache[key] = (fingerprint, deepcopy(value))
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHED_FILES:
            _cache.popitem(last=False)


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _field_path(parent, field):
    return parent + "/" + field.replace("~", "~0").replace("/", "~1")


def _rows(value, path="", ancestors=None):
    """按字段顺序展开；只使用 CSV 原生单元格，保留明确的标量类型。"""
    if value is None:
        yield (path, "null", "")
    elif isinstance(value, bool):
        yield (path, "bool", "true" if value else "false")
    elif isinstance(value, str):
        yield (path, "str", value)
    elif isinstance(value, int):
        yield (path, "int", str(value))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Record numbers must be finite")
        yield (path, "float", repr(value))
    elif isinstance(value, (dict, list, tuple)):
        ancestors = set() if ancestors is None else ancestors
        if id(value) in ancestors:
            raise ValueError("Record must not contain circular references")
        ancestors.add(id(value))
        try:
            yield (path, "dict" if isinstance(value, dict) else "list", "")
            fields = value.items() if isinstance(value, dict) else enumerate(value)
            for field, nested in fields:
                if isinstance(value, dict) and not isinstance(field, str):
                    raise TypeError("Record field names must be strings")
                yield from _rows(nested, _field_path(path, str(field)), ancestors)
        finally:
            ancestors.remove(id(value))
    else:
        raise TypeError("Unsupported record value type: " + type(value).__name__)


def _encode_csv(value):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(("path", "type", "value"))
    writer.writerows(_rows(value))
    return stream.getvalue().encode("utf-8-sig")


def _typed_value(kind, value):
    if kind in ("dict", "list", "null"):
        if value:
            raise ValueError("Container and null rows must have empty values")
        return {} if kind == "dict" else [] if kind == "list" else None
    if kind == "str":
        return value
    if kind == "bool" and value in ("true", "false"):
        return value == "true"
    if kind == "int":
        return int(value)
    if kind == "float":
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Record numbers must be finite")
        return number
    raise ValueError("Unknown or invalid record value type")


def _decode_csv(stream):
    reader = csv.reader(stream)
    if next(reader, None) != ["path", "type", "value"]:
        raise ValueError("Invalid record CSV header")
    fields = {}
    for row in reader:
        if len(row) != 3:
            raise ValueError("Record CSV rows must have three columns")
        path, kind, encoded = row
        if path in fields:
            raise ValueError("Duplicate record field path")
        value = _typed_value(kind, encoded)
        if not fields:
            if path != "":
                raise ValueError("Record root must be the first field")
        else:
            if not path.startswith("/"):
                raise ValueError("Invalid record field path")
            parent_path, _, escaped = path.rpartition("/")
            if parent_path not in fields:
                raise ValueError("Record field parent is missing")
            if re.search(r"~(?![01])", escaped):
                raise ValueError("Invalid record field escape")
            field = escaped.replace("~1", "/").replace("~0", "~")
            parent = fields[parent_path]
            if isinstance(parent, dict):
                parent[field] = value
            elif isinstance(parent, list) and field == str(len(parent)):
                parent.append(value)
            else:
                raise ValueError("Invalid record field parent or list index")
        fields[path] = value
    if "" not in fields:
        raise ValueError("Record CSV is missing its root")
    return fields[""]


def read_csv(path):
    """不存在返回 None；返回副本，避免调用方污染读缓存。"""
    path = Path(path)
    key = str(path.absolute())
    if path.is_symlink():
        raise ValueError("Record path must not be a symbolic link")
    for _ in range(3):
        try:
            fingerprint = _fingerprint(path)
        except FileNotFoundError:
            with _cache_lock:
                _cache.pop(key, None)
            return None
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None and cached[0] == fingerprint:
                _cache.move_to_end(key)
                return deepcopy(cached[1])
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                value = _decode_csv(stream)
            if _fingerprint(path) == fingerprint:
                _remember(key, fingerprint, value)
                return deepcopy(value)
        except FileNotFoundError:
            continue
    # 连续写入时读取一个完整快照，但不缓存可能过期的版本。
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return _decode_csv(stream)
    except FileNotFoundError:
        return None


@contextmanager
def locked(directory):
    """同一存储目录的读改写串行执行；支持当前线程的嵌套调用。"""
    directory = Path(directory).resolve()
    key = str(directory)
    with _registry_lock:
        mutex = _lock_registry.get(key)
        if mutex is None:
            mutex = threading.RLock()
            _lock_registry[key] = mutex
    with mutex:
        held = getattr(_held, "directories", set())
        if key in held:
            yield
            return
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / ".write.lock"
        if lock_path.is_symlink():
            raise ValueError("Lock path must not be a symbolic link")
        with lock_path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            held.add(key)
            _held.directories = held
            try:
                yield
            finally:
                held.remove(key)
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_csv(path, value, *, overwrite=True):
    """写临时文件后原子替换；失败时保留原文件；不覆盖模式用于迁移。"""
    encoded = _encode_csv(value)
    path = Path(path)
    with locked(path.parent):
        if path.is_symlink():
            raise ValueError("Record path must not be a symbolic link")
        if not overwrite and path.exists():
            return False
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            # 外部编辑可能紧接着替换发生；不能把旧 encoded 配上新文件版本。
            # 下一次 read_csv 会验证实际文件的稳定快照后再建立缓存。
            with _cache_lock:
                _cache.pop(str(path.absolute()), None)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return True


def iter_records(directory):
    """只遍历规范 UUID CSV 文件，临时文件及锁文件不会成为记录。"""
    directory = Path(directory)
    if not directory.exists():
        return
    for path in directory.glob("*.csv"):
        if _UUID.fullmatch(path.stem) and not path.is_symlink():
            record = read_csv(path)
            if record is not None:
                yield record
