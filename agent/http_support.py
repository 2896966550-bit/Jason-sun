"""Shared HTTP input contracts and static-resource versioning."""
import hashlib
from pathlib import Path
import threading
import time

from flask import request


class RequestInputError(ValueError):
    """A malformed client request; handled as a structured HTTP 400."""


def json_object() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise RequestInputError("请求必须是有效的 JSON 对象")
    return data


def text_field(data, key, *, required=False, default="") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise RequestInputError(f"{key} 必须是字符串")
    if required and not value.strip():
        raise RequestInputError(f"{key} 不能为空")
    return value


def positive_limit(value, default=20, maximum=100) -> int:
    if value is None:
        return default
    try:
        count = int(value)
    except (ValueError, TypeError):
        raise RequestInputError("n 必须是正整数") from None
    if count <= 0:
        raise RequestInputError("n 必须是正整数")
    return min(count, maximum)


class AssetVersion:
    """Short cached fingerprint; nanosecond timestamps detect equal-size saves."""
    def __init__(self, static_dir, ttl=3.0):
        self.static_dir = Path(static_dir)
        self.ttl = ttl
        self._version = None
        self._checked = 0.0
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            now = time.monotonic()
            if self._version and now - self._checked < self.ttl:
                return self._version
            fingerprint = hashlib.sha256()
            for sub in ("css", "js"):
                directory = self.static_dir/sub
                if not directory.is_dir():
                    continue
                for path in sorted(directory.iterdir()):
                    if path.suffix not in (".css", ".js"):
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    fingerprint.update(f"{sub}/{path.name}|{stat.st_mtime_ns}|{stat.st_size};".encode())
            self._version = fingerprint.hexdigest()[:10]
            self._checked = now
            return self._version
