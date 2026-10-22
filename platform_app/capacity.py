"""FIFO permits that can be resized without abandoning existing waiters."""
import asyncio
from collections import deque

DEFAULT_LIMITS = {'model_concurrency': 4, 'queue_limit': 64, 'queue_timeout': 15}


class ModelGate:
    def __init__(self, limit):
        self.limit = limit
        self.active = 0
        self.waiters = deque()

    def resize(self, limit):
        self.limit = limit
        self._wake()

    def _wake(self):
        while self.active < self.limit and self.waiters:
            future = self.waiters.popleft()
            if not future.done():
                self.active += 1
                future.set_result(None)

    async def acquire(self):
        if self.active < self.limit and not self.waiters:
            self.active += 1
            return
        future = asyncio.get_running_loop().create_future()
        self.waiters.append(future)
        try:
            await future
        except BaseException:
            # Cancellation may race with a permit already granted by release/resize.
            if future.done() and not future.cancelled():
                self.active -= 1
            else:
                future.cancel()
            try:
                self.waiters.remove(future)
            except ValueError:
                pass
            self._wake()
            raise

    def release(self):
        self.active -= 1
        self._wake()
