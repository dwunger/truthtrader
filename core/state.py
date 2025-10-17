import json, os, threading
from typing import Any, Dict

class State:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save(self):
        tmp = self.path + ".tmp"
        with self._lock, open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.path)

    def get(self, *keys, default=None):
        cur = self._data
        for k in keys:
            cur = cur.get(k, {})
        return cur if cur != {} else default

    def set(self, value, *keys):
        with self._lock:
            cur = self._data
            for k in keys[:-1]:
                cur = cur.setdefault(k, {})
            cur[keys[-1]] = value
            self.save()

    def root(self):
        return self._data
