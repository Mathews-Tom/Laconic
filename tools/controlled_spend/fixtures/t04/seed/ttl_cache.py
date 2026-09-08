from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _Entry(Generic[T]):  # noqa: UP046
    value: T
    expires_at: float


class TtlCache(Generic[T]):  # noqa: UP046
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _Entry[T]] = {}

    def put(self, key: str, value: T, ttl_seconds: float) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        self._entries[key] = _Entry(value=value, expires_at=self._clock() + ttl_seconds)

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < self._clock():
            del self._entries[key]
            return None
        return entry.value

    def prune(self) -> int:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at < now]
        for key in expired:
            del self._entries[key]
        return len(expired)
