import asyncio

import pytest

from tests.fakes import FakeTurnHandle
from turn_manager import (
    TurnConflictError,
    TurnManager,
    TurnNotActiveError,
    TurnsUnavailableError,
)


@pytest.mark.asyncio
async def test_reserve_is_atomic_under_race() -> None:
    manager = TurnManager()

    async def reserve():
        try:
            await manager.reserve("thr_1")
            return "reserved"
        except TurnConflictError:
            return "conflict"

    assert sorted(await asyncio.gather(reserve(), reserve())) == [
        "conflict",
        "reserved",
    ]


@pytest.mark.asyncio
async def test_control_and_shutdown_interrupt_and_drain() -> None:
    manager = TurnManager()
    await manager.reserve(
        "thr_1",
        kind="goal",
        model="gpt-test",
        reasoning_effort="high",
    )
    handle = FakeTurnHandle("turn_1")
    await manager.mark_running("thr_1", turn_id="turn_1", handle=handle)
    assert (await manager.status())["active_threads"]["thr_1"]["model"] == "gpt-test"
    assert (await manager.status())["active_threads"]["thr_1"]["kind"] == "goal"
    assert (
        await manager.status()
    )["active_threads"]["thr_1"]["reasoning_effort"] == "high"

    async def pump():
        await handle.release.wait()

    task = asyncio.create_task(pump())
    await manager.attach_task("thr_1", task)
    await manager.steer("thr_1", "new direction")
    await manager.shutdown(timeout=1)

    assert handle.steers == ["new direction"]
    assert handle.interrupted is True
    assert (await manager.status())["active_turn_count"] == 0
    with pytest.raises(TurnsUnavailableError):
        await manager.reserve("thr_2")
    with pytest.raises(TurnNotActiveError):
        await manager.interrupt("thr_1")


@pytest.mark.asyncio
async def test_mutation_and_turn_reservations_exclude_each_other() -> None:
    manager = TurnManager()
    await manager.reserve_mutation("thr_1")
    with pytest.raises(TurnConflictError):
        await manager.reserve("thr_1")
    await manager.finish_mutation("thr_1")

    await manager.reserve("thr_1")
    with pytest.raises(TurnConflictError):
        await manager.reserve_mutation("thr_1")


@pytest.mark.asyncio
async def test_shutdown_times_out_hanging_interrupt_and_cancels_pump() -> None:
    class HangingHandle:
        async def interrupt(self):
            await asyncio.Event().wait()

    manager = TurnManager()
    await manager.reserve("thr_1")
    await manager.mark_running(
        "thr_1",
        turn_id="turn_1",
        handle=HangingHandle(),
    )
    pump = asyncio.create_task(asyncio.Event().wait())
    await manager.attach_task("thr_1", pump)

    await asyncio.wait_for(manager.shutdown(timeout=0.01), timeout=0.1)

    assert pump.cancelled()
    assert (await manager.status())["active_turn_count"] == 0
