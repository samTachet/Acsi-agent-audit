"""Limiteur de débit à seau de jetons, par clé, avec horloge injectable (tests déterministes)."""
from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.clock = clock
        self._state: dict[str, tuple[float, float]] = {}  # clé -> (jetons, dernier instant)
        self._lock = threading.Lock()

    def _refill(self, key: str) -> float:
        tokens, last = self._state.get(key, (self.capacity, self.clock()))
        now = self.clock()
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        self._state[key] = (tokens, now)
        return tokens

    def peek(self, key: str) -> bool:
        """Un appel serait-il admis ? Ne consomme rien."""
        with self._lock:
            return self._refill(key) >= 1.0

    def consume(self, key: str) -> bool:
        with self._lock:
            tokens = self._refill(key)
            if tokens < 1.0:
                return False
            self._state[key] = (tokens - 1.0, self._state[key][1])
            return True
