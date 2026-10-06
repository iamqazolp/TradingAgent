"""Lightweight in-memory cache for MCP tool results.

Caches the pre-computed analysis payload for (tool, ticker, **params) tuples
so consecutive questions about the same stock don't re-scan the database and
re-run the indicator engine.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

_DEFAULT_TTL = 300  # 5 minutes — enough for a Q&A burst, auto-expires when new session begins
_MAX_ENTRIES = 50   # Bound memory; 50 entries * ~20 KB ~= 1 MB


class TickerCache:
    """A TTL-bounded in-memory cache keyed on (tool, **kwargs).

    Thread-safe via a standard lock.
    """

    def __init__(self, ttl: int = _DEFAULT_TTL, max_entries: int = _MAX_ENTRIES):
        self._ttl = ttl
        self._max_entries = max_entries
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(tool: str, **kwargs: Any) -> str:
        """Deterministic key from tool name and sorted JSON-serializable parameters."""
        raw = json.dumps({"tool": tool, **kwargs}, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get(self, tool: str, **kwargs: Any) -> Any | None:
        """Retrieve a cached entry if present and not expired."""
        key = self._key(tool, **kwargs)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self._ttl:
                del self._store[key]
                return None
            return value

    def put(self, value: Any, tool: str, **kwargs: Any) -> None:
        """Store an entry, evicting the oldest if at capacity."""
        key = self._key(tool, **kwargs)
        with self._lock:
            if len(self._store) >= self._max_entries and key not in self._store:
                self._evict_oldest()
            self._store[key] = (time.monotonic(), value)

    def clear(self) -> int:
        """Clear all entries. Returns the number of entries cleared."""
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    def _evict_oldest(self) -> None:
        """Remove the oldest entry."""
        if not self._store:
            return
        oldest_key = min(self._store, key=lambda k: self._store[k][0])
        del self._store[oldest_key]

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)


_cache = TickerCache()


def get_cache() -> TickerCache:
    """Module-level singleton cache."""
    return _cache
