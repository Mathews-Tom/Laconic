from __future__ import annotations

import unittest

from ttl_cache import TtlCache


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TtlCacheTests(unittest.TestCase):
    def test_entry_expires_at_exact_boundary(self) -> None:
        clock = Clock()
        cache: TtlCache[str] = TtlCache(clock)
        cache.put("key", "value", 5)
        clock.now = 105.0
        self.assertIsNone(cache.get("key"))

    def test_prune_uses_same_boundary(self) -> None:
        clock = Clock()
        cache: TtlCache[str] = TtlCache(clock)
        cache.put("key", "value", 5)
        clock.now = 105.0
        self.assertEqual(cache.prune(), 1)

    def test_zero_ttl_is_immediately_unavailable(self) -> None:
        clock = Clock()
        cache: TtlCache[str] = TtlCache(clock)
        cache.put("key", "value", 0)
        self.assertIsNone(cache.get("key"))


if __name__ == "__main__":
    unittest.main()
