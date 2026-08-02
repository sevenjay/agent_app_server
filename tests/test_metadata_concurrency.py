import asyncio

import pytest
from sqlalchemy import func, select

from database import async_session
from main import _touch_thread_metadata
from models import ThreadUIMetadata


@pytest.mark.asyncio
async def test_parallel_fragment_touches_use_atomic_metadata_upsert() -> None:
    thread = {
        "id": "thr_parallel_metadata",
        "project_key": "agent_app_server",
    }

    async def touch() -> None:
        async with async_session() as session:
            await _touch_thread_metadata(session, thread, opened=True)

    await asyncio.gather(touch(), touch(), touch())

    async with async_session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ThreadUIMetadata)
            .where(ThreadUIMetadata.thread_id == thread["id"])
        )
        metadata = await session.get(ThreadUIMetadata, thread["id"])
    assert count == 1
    assert metadata is not None
    assert metadata.project_key == "agent_app_server"
    assert metadata.last_opened_at is not None
