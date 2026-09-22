"""进程内滑动窗口限流。

用于登录失败与匿名检测等入口的滥用防护：
- allowed(key, limit, window)：只读判断当前窗口内是否仍可放行；
- record(key)：记录一次事件（如一次失败尝试）。

仅进程内计数：gunicorn 多 worker 部署时各 worker 分别计数，防护强度
按 worker 数稀释，课程规模下可接受（单 worker 部署则完全精确）。
"""

import threading
import time

_lock = threading.Lock()
_events = {}

_MAX_KEYS = 4096


def _prune(now):
    """事件列表全部过期且无新事件的键删除，防止内存无限增长。"""
    if len(_events) < _MAX_KEYS:
        return
    stale = [key for key, events in _events.items()
             if not events or events[-1] < now - 3600]
    for key in stale:
        del _events[key]


def allowed(key, limit, window_seconds):
    """当前窗口内事件数未达上限返回 True；只判断，不记录。"""
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        _prune(now)
        events = [ts for ts in _events.get(key, ()) if ts >= cutoff]
        _events[key] = events
        return len(events) < limit


def record(key):
    """记录一次事件。"""
    now = time.monotonic()
    with _lock:
        _events.setdefault(key, []).append(now)


def retry_after(key, window_seconds):
    """最早事件滑出窗口还需多少秒（向上取整，至少 1 秒）。"""
    now = time.monotonic()
    with _lock:
        events = _events.get(key)
        if not events:
            return 1
        return max(1, int(events[0] + window_seconds - now) + 1)
