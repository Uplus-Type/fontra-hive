"""The heavy reading of a font's history, kept apart from everything else.

Describing the history (``fontdiff``) is pure Python: while it runs in a
thread it holds the GIL, and a thread pool shared with push, pull, import
and Try lets one person who sends many requests make the whole Instance
wait. :class:`HistoryWork` runs these computations (the history, the font
diff, the merge preview and the merge's domain check):

- in an executor of their own, small (:data:`THREADS`): push, pull, import
  and Try keep the default pool;
- at most :data:`PER_PERSON` at once for one person, who gets 429 with
  :data:`BUSY` beyond (``asyncio.Semaphore``-like, without waiting);
- identical requests in progress (same key) share one computation;
- a time limit, when one is given, counted from the start of the
  computation, never from the moment the request entered the queue: a
  request that only waited is never told it takes too long.
"""

from __future__ import annotations

import asyncio
import collections
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Hashable

from aiohttp import web

# One person never holds every thread: one is always left to the others.
THREADS = 3
PER_PERSON = 2
assert PER_PERSON < THREADS
BUSY = "Hive is still working on your last request. Try again in a moment."


class TooLong(Exception):
    """The computation ran longer than its time limit."""


class HistoryWork:
    def __init__(self, threads: int = THREADS, perPerson: int = PER_PERSON):
        self.threads = threads
        self.perPerson = perPerson
        self._executor: ThreadPoolExecutor | None = None
        # person -> requests in progress (waiting or running)
        self._busy: collections.Counter = collections.Counter()
        # key -> the shared computation in progress
        self._shared: dict[Hashable, asyncio.Future] = {}

    @property
    def executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                self.threads, thread_name_prefix="hive-history"
            )
        return self._executor

    def busy(self, person: str) -> int:
        return self._busy[person]

    async def run(
        self,
        person: str,
        key: Hashable | None,
        function: Callable[..., Any],
        *args,
        timeout: float | None = None,
    ):
        """``function(*args)`` in the history's executor, for ``person``.
        ``key``: requests with the same key in progress share one
        computation (None: never shared). Raises ``web.HTTPTooManyRequests``
        when the person has :attr:`perPerson` requests in progress already,
        :class:`TooLong` when the computation outran ``timeout``."""
        if self._busy[person] >= self.perPerson:
            raise web.HTTPTooManyRequests(text=BUSY, headers={"Retry-After": "1"})
        self._busy[person] += 1
        try:
            if key is None:
                return await self._compute(function, args, timeout)
            shared = self._shared.get(key)
            if shared is None:
                shared = asyncio.ensure_future(self._compute(function, args, timeout))
                self._shared[key] = shared
                shared.add_done_callback(lambda _f, key=key: self._forget(key, _f))
            # Shielded: one person leaving does not stop the others' answer.
            return await asyncio.shield(shared)
        finally:
            self._busy[person] -= 1
            if self._busy[person] <= 0:
                del self._busy[person]

    def _forget(self, key: Hashable, future: asyncio.Future) -> None:
        if self._shared.get(key) is future:
            del self._shared[key]
        if not future.cancelled():
            future.exception()  # read: never "exception was never retrieved"

    async def _compute(self, function, args, timeout: float | None):
        loop = asyncio.get_running_loop()
        started = loop.create_future()

        def call():
            loop.call_soon_threadsafe(_set, started, time.monotonic())
            return function(*args)

        work = loop.run_in_executor(self.executor, call)
        if timeout is None:
            return await work
        # Wait in the queue as long as it takes; the time limit runs from
        # the start of the computation.
        await asyncio.wait({work, started}, return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return work.result()
        try:
            return await asyncio.wait_for(asyncio.shield(work), timeout)
        except asyncio.TimeoutError:
            # The thread is not stopped (it cannot be): its answer is lost.
            work.add_done_callback(_consume)
            raise TooLong()

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


def _set(future: asyncio.Future, value) -> None:
    if not future.done():
        future.set_result(value)


def _consume(future) -> None:
    if not future.cancelled():
        future.exception()
