import json, os, threading, time, tempfile
from typing import Any, Optional

class State:
    """
    Simple thread-safe JSON state store.
    - Keeps an in-memory dict guarded by a re-entrant lock.
    - Persists via atomic temp-file + os.replace with Windows-friendly retries.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data = {}
        self._load()

    # ---------- public API ----------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, value: Any, key: str) -> None:
        with self._lock:
            self._data[key] = value
            self._persist_with_retries()

    # ---------- internals ----------
    def _load(self) -> None:
        with self._lock:
            try:
                if os.path.exists(self.path):
                    with open(self.path, "r", encoding="utf-8") as f:
                        self._data = json.load(f)
                else:
                    self._data = {}
            except Exception:
                # Corrupt or unreadable state; start fresh but don't crash the app
                self._data = {}

    def _persist_with_retries(self, tries: int = 25, base_sleep: float = 0.05) -> None:
        """
        Write to a temp file then atomically replace the target.
        Retries handle Windows 'file in use' races from other threads/processes.
        """
        payload = json.dumps(self._data, ensure_ascii=False)
        dirpath = os.path.dirname(self.path) or "."
        for i in range(tries):
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=dirpath, text=True)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)  # atomic on Win/Unix
                return
            except Exception:
                # brief backoff; exponential-ish
                time.sleep(base_sleep * (1 + i * 0.25))
            finally:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
        # If we get here, we failed to persist after many retries
        # Don't raise—avoid killing the app; last good in-memory value remains.
        # You can add logging here if desired.
