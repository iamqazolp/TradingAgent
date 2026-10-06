"""Tests for in-memory TTL cache in mcp_server.cache."""

from __future__ import annotations

import time

from mcp_server.cache import TickerCache, get_cache


def test_cache_put_and_get():
    cache = TickerCache(ttl=10)
    cache.put({"result": 123}, "test_tool", ticker="VNM", scope="full")

    hit = cache.get("test_tool", ticker="VNM", scope="full")
    assert hit == {"result": 123}

    miss = cache.get("test_tool", ticker="HPG", scope="full")
    assert miss is None


def test_cache_ttl_expiration():
    cache = TickerCache(ttl=0.05)  # 50ms TTL
    cache.put({"data": 456}, "test_tool", ticker="VNM")

    assert cache.get("test_tool", ticker="VNM") == {"data": 456}
    time.sleep(0.06)
    assert cache.get("test_tool", ticker="VNM") is None


def test_cache_max_entries_eviction():
    cache = TickerCache(ttl=10, max_entries=3)
    cache.put("val1", "tool", id=1)
    cache.put("val2", "tool", id=2)
    cache.put("val3", "tool", id=3)

    assert cache.size == 3
    # Adding a 4th should evict the oldest (id=1)
    cache.put("val4", "tool", id=4)
    assert cache.size == 3
    assert cache.get("tool", id=1) is None
    assert cache.get("tool", id=2) == "val2"
    assert cache.get("tool", id=4) == "val4"


def test_cache_clear():
    cache = TickerCache(ttl=10)
    cache.put("val1", "tool", ticker="AAA")
    cache.put("val2", "tool", ticker="BBB")
    assert cache.size == 2

    cleared = cache.clear()
    assert cleared == 2
    assert cache.size == 0
    assert cache.get("tool", ticker="AAA") is None


def test_singleton_cache():
    c1 = get_cache()
    c2 = get_cache()
    assert c1 is c2
