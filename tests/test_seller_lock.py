"""The per-seller lock must still serialise, and must not accumulate.

It exists because sale handling runs as independent tasks, so two sales for the same seller would
otherwise race on the value window, the alert cooldown and the mug claim — and double-alert. The
leak fix must not weaken that.
"""

import asyncio

from server import detection


def test_lock_entry_is_released_when_nobody_holds_it():
    async def scenario():
        async with detection._seller_lock(42):
            assert 42 in detection._seller_locks
        assert detection._seller_locks == {}

    detection._seller_locks.clear()
    asyncio.run(scenario())


def test_many_distinct_sellers_do_not_accumulate():
    """Previously one lock per seller ever seen, kept for the lifetime of a months-long process."""
    async def scenario():
        for seller_id in range(10_000):
            async with detection._seller_lock(seller_id):
                pass
        assert detection._seller_locks == {}

    detection._seller_locks.clear()
    asyncio.run(scenario())


def test_same_seller_is_still_serialised():
    """The actual purpose of the lock — two concurrent sales must not interleave."""
    async def scenario():
        overlapping = []
        inside = 0

        async def worker():
            nonlocal inside
            async with detection._seller_lock(7):
                inside += 1
                overlapping.append(inside)
                await asyncio.sleep(0)      # yield: an unguarded section would interleave here
                inside -= 1

        await asyncio.gather(*(worker() for _ in range(20)))
        assert max(overlapping) == 1, "two tasks were inside the lock at once"
        assert detection._seller_locks == {}

    detection._seller_locks.clear()
    asyncio.run(scenario())


def test_different_sellers_still_run_concurrently():
    """Serialising per seller must not become a global bottleneck on the alert path."""
    async def scenario():
        started = []

        async def worker(seller_id):
            async with detection._seller_lock(seller_id):
                started.append(seller_id)
                await asyncio.sleep(0.02)

        await asyncio.wait_for(
            asyncio.gather(*(worker(i) for i in range(5))), timeout=0.1)
        assert sorted(started) == [0, 1, 2, 3, 4]

    detection._seller_locks.clear()
    asyncio.run(scenario())


def test_a_waiter_is_not_evicted_by_the_task_that_finishes():
    """The race the reference count exists to prevent: the holder must not delete the entry while
    another task is queued on that same lock object."""
    async def scenario():
        seen_locks = []

        async def worker():
            entry = detection._seller_locks.get(9)
            async with detection._seller_lock(9):
                seen_locks.append(id(detection._seller_locks[9][0]))
                await asyncio.sleep(0.01)
            del entry

        await asyncio.gather(worker(), worker(), worker())
        assert len(set(seen_locks)) == 1, "tasks used different lock objects — serialisation broken"
        assert detection._seller_locks == {}

    detection._seller_locks.clear()
    asyncio.run(scenario())


def test_an_exception_inside_the_lock_still_releases_it():
    async def scenario():
        try:
            async with detection._seller_lock(5):
                raise RuntimeError("sale handling blew up")
        except RuntimeError:
            pass
        assert detection._seller_locks == {}
        async with detection._seller_lock(5):    # still acquirable
            pass

    detection._seller_locks.clear()
    asyncio.run(scenario())
