"""API key pool for round-robin multi-key concurrent LLM calls."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence, TypeVar

logger = logging.getLogger(__name__)

KEY_COOLDOWN_SECONDS = 60
_AUTH_COOLDOWN_SECONDS = 300

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class KeySlot:
    api_key: str
    index: int
    in_use: bool = False
    error_count: int = 0
    cooldown_until: float = 0.0
    total_calls: int = 0
    total_errors: int = 0

    @property
    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def mark_error(self, cooldown: float = KEY_COOLDOWN_SECONDS) -> None:
        self.error_count += 1
        self.total_errors += 1
        backoff = cooldown * (2 ** max(0, self.error_count - 1))
        self.cooldown_until = time.time() + backoff

    def mark_success(self) -> None:
        self.error_count = 0
        self.cooldown_until = 0.0


class KeyContext:
    """Async context manager: acquires a key slot under a global semaphore."""

    def __init__(self, pool: "ApiKeyPool") -> None:
        self._pool = pool
        self._slot: Optional[KeySlot] = None
        self._semaphore_acquired = False

    async def __aenter__(self) -> str:
        await self._pool._semaphore.acquire()
        self._semaphore_acquired = True
        slot = await self._pool._pick_key()
        slot.in_use = True
        slot.total_calls += 1
        self._slot = slot
        return slot.api_key

    async def __aexit__(self, exc_type, exc, tb) -> None:
        slot = self._slot
        if slot is not None:
            slot.in_use = False
            if exc is None:
                slot.mark_success()
            else:
                cooldown = self._classify_cooldown(exc)
                slot.mark_error(cooldown)
                logger.warning(
                    "[KeyPool] key idx=%d marked error (count=%d, cooldown=%.0fs): %s",
                    slot.index, slot.error_count, cooldown, exc,
                )
        if self._semaphore_acquired:
            self._pool._semaphore.release()
            self._semaphore_acquired = False

    @staticmethod
    def _classify_cooldown(exc: BaseException) -> float:
        msg = str(exc).lower()
        auth_markers = (
            "401", "403", "unauthorized", "forbidden",
            "invalid api key", "invalid_api_key", "authentication",
        )
        if any(m in msg for m in auth_markers):
            return _AUTH_COOLDOWN_SECONDS
        return KEY_COOLDOWN_SECONDS


class ApiKeyPool:
    """Round-robin pool of API keys with global concurrency control."""

    def __init__(self, keys: List[str], concurrency: Optional[int] = None) -> None:
        cleaned: List[str] = []
        seen = set()
        for k in keys or []:
            k = (k or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            cleaned.append(k)
        if not cleaned:
            raise ValueError("ApiKeyPool requires at least one non-empty API key")

        self._slots: List[KeySlot] = [
            KeySlot(api_key=k, index=i) for i, k in enumerate(cleaned)
        ]
        max_conc = concurrency if (concurrency and concurrency > 0) else len(self._slots)
        self._semaphore = asyncio.Semaphore(max_conc)
        self._concurrency = max_conc
        self._rr_lock = asyncio.Lock()
        self._rr_pointer = 0

    @classmethod
    def from_config(
        cls,
        api_keys_str: str,
        concurrency: Optional[int] = None,
    ) -> "ApiKeyPool":
        parts = [p.strip() for p in (api_keys_str or "").split(",") if p.strip()]
        return cls(parts, concurrency=concurrency)

    @property
    def size(self) -> int:
        return len(self._slots)

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def acquire(self) -> KeyContext:
        return KeyContext(self)

    def stats(self) -> List[dict]:
        return [
            {
                "index": s.index,
                "in_use": s.in_use,
                "error_count": s.error_count,
                "total_calls": s.total_calls,
                "total_errors": s.total_errors,
                "cooldown_until": s.cooldown_until,
                "available": s.available,
            }
            for s in self._slots
        ]

    async def _pick_key(self) -> KeySlot:
        async with self._rr_lock:
            n = len(self._slots)
            for offset in range(n):
                idx = (self._rr_pointer + offset) % n
                slot = self._slots[idx]
                if slot.available:
                    self._rr_pointer = (idx + 1) % n
                    return slot

            slot = min(self._slots, key=lambda s: s.cooldown_until)
            wait = max(0.0, slot.cooldown_until - time.time())

        if wait > 0:
            logger.warning(
                "[KeyPool] all keys cooling down; waiting %.1fs for slot idx=%d",
                wait, slot.index,
            )
            await asyncio.sleep(wait)
        return slot


async def run_with_pool(
    tasks: Sequence[T],
    worker: Callable[[T, str], Awaitable[R]],
    pool: ApiKeyPool,
    ckpt=None,
) -> List[R]:
    """Run *worker(task, api_key)* concurrently across *pool*.

    The pool's semaphore enforces global concurrency.  When *ckpt* is given,
    failures are recorded via ``ckpt.mark_failed`` keyed on ``str(task)``.
    """
    async def _runner(task: T) -> Optional[R]:
        async with pool.acquire() as api_key:
            try:
                return await worker(task, api_key)
            except Exception as e:
                if ckpt is not None:
                    try:
                        ckpt.mark_failed(str(task), str(e))
                    except Exception as ckpt_err:
                        logger.debug("ckpt.mark_failed raised: %s", ckpt_err)
                raise

    coros = [_runner(t) for t in tasks]
    results = await asyncio.gather(*coros, return_exceptions=True)

    output: List[R] = []
    for task, res in zip(tasks, results):
        if isinstance(res, BaseException):
            logger.error("[KeyPool] task %r failed: %s", task, res)
        else:
            output.append(res)
    return output
