"""Conservative byte accounting shared by update download and output writers."""

from __future__ import annotations

import contextvars
import io
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Buffer
    from types import TracebackType
    from typing import IO


class StagingBudget:
    """Bound bytes written during an attempt, including files retained from its retry.

    Deleted and replaced bytes stay charged until the next attempt. This can refuse a
    large retry early, but never allows temporary writes to exceed the disk allowance.
    """

    def __init__(self, limit: int, used: int = 0) -> None:
        self.limit = limit
        self.used = used
        self.lock = threading.Lock()
        self.consume(0)

    def consume(self, size: int) -> None:
        """Reserve a write before it reaches disk, safely across download workers."""
        with self.lock:
            if self.used + size > self.limit:
                msg = "update exceeds its aggregate staging budget; raise max_bytes or --max-bytes"
                raise ValueError(msg)
            self.used += size


ACTIVE_BUDGET: contextvars.ContextVar[StagingBudget | None] = contextvars.ContextVar("staging_budget", default=None)


def charge(size: int) -> None:
    """Reserve the next write when an update owns the operation."""
    if (budget := ACTIVE_BUDGET.get()) is not None:
        budget.consume(size)


class BudgetWriter(io.BufferedWriter):
    """Charge every write through a binary file object, including table backend writes."""

    def __init__(self, raw: IO[bytes]) -> None:
        super().__init__(raw)
        # Native table writers can call back from threads without the coordinator's context.
        self.budget = ACTIVE_BUDGET.get()
        self._failure: ValueError | None = None

    def write(self, data: Buffer, /) -> int:
        """Reserve bytes before BufferedWriter stores or flushes them."""
        if self.budget is not None:
            try:
                self.budget.consume(memoryview(data).nbytes)
            except ValueError as error:
                self._failure = error
                raise
        return super().write(data)

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        """Surface budget errors even when a native writer wraps or suppresses them."""
        try:
            super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            if self._failure is not None and self._failure is not exc_val:
                raise self._failure from exc_val
