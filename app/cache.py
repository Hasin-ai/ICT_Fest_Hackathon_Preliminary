"""In-memory response caches for read-heavy reporting endpoints.

Usage reports and per-room availability are relatively expensive to compute and
are read far more often than the underlying data changes, so results are cached
and invalidated when the data they depend on is modified.
"""
import collections
import threading

class LRUCache:
    def __init__(self, maxsize=1000):
        self.maxsize = maxsize
        self.cache = collections.OrderedDict()

    def get(self, key, default=None):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return default

    def __setitem__(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)

    def __getitem__(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        raise KeyError(key)

    def pop(self, key, default=None):
        return self.cache.pop(key, default)

    def __iter__(self):
        return iter(self.cache)

    def keys(self):
        return self.cache.keys()

    def __len__(self):
        return len(self.cache)

    def clear(self):
        self.cache.clear()


_report_cache = LRUCache(maxsize=1000)
_availability_cache = LRUCache(maxsize=1000)
_cache_lock = threading.Lock()


def get_report(org_id: int, frm: str, to: str):
    with _cache_lock:
        return _report_cache.get((org_id, frm, to))


def set_report(org_id: int, frm: str, to: str, value: dict) -> None:
    with _cache_lock:
        _report_cache[(org_id, frm, to)] = value


def invalidate_report(org_id: int) -> None:
    with _cache_lock:
        for key in [k for k in _report_cache if k[0] == org_id]:
            _report_cache.pop(key, None)


def get_availability(room_id: int, date: str):
    with _cache_lock:
        return _availability_cache.get((room_id, date))


def set_availability(room_id: int, date: str, value: dict) -> None:
    with _cache_lock:
        _availability_cache[(room_id, date)] = value


def invalidate_availability(room_id: int, date: str) -> None:
    with _cache_lock:
        _availability_cache.pop((room_id, date), None)

