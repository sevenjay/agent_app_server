from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
import httpx2


@asynccontextmanager
async def application_client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """Serve an ASGI app through httpx2 while explicitly managing its lifespan."""
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client
