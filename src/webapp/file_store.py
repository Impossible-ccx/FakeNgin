"""小型 JSON 文件存储：原子写入、文件变化失效缓存与进程间写锁。"""

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
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


def uuid_path(directory, identifier):
    """仅接受规范 UUID；用户输入不能指定文件夹、后缀或符号链接。"""
    if not isinstance(identifier, str) or not _UUID.fullmatch(identifier):
        raise ValueError("Invalid record identifier")
    path = Path(directory).resolve() / (identifier + ".json")
    if path.is_symlink():
        raise ValueError("Record path must not be a symbolic link")
    return path


def key_path(directory, key):
    """复合数据引用编码成固定散列文件名，原始字段不会参与路径拼接。"""
    encoded = json.dumps(key, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    path = Path(directory).resolve() / (digest + ".json")
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


def read_json(path):
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
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if _fingerprint(path) == fingerprint:
                _remember(key, fingerprint, value)
                return deepcopy(value)
        except FileNotFoundError:
            continue
    # 连续写入时读取一个完整快照，但不缓存可能过期的版本。
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
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


def write_json(path, value, *, overwrite=True):
    """写临时文件后原子替换；失败时保留原文件；不覆盖模式用于迁移。"""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
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
            # 下一次 read_json 会验证实际文件的稳定快照后再建立缓存。
            with _cache_lock:
                _cache.pop(str(path.absolute()), None)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return True


def iter_records(directory):
    """只遍历规范 UUID JSON 文件，临时文件及锁文件不会成为记录。"""
    directory = Path(directory)
    if not directory.exists():
        return
    for path in directory.glob("*.json"):
        if _UUID.fullmatch(path.stem) and not path.is_symlink():
            record = read_json(path)
            if record is not None:
                yield record
