# =============================================================================
# NestEgg - app/database.py
# MySQL connection pool and async cursor context managers.
# Uses aiomysql for async FastAPI compatibility.
# =============================================================================

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiomysql
from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Pool singleton
# ---------------------------------------------------------------------------

_pool: aiomysql.Pool | None = None


async def create_pool() -> aiomysql.Pool:
    global _pool
    _pool = await aiomysql.create_pool(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        db=os.environ["DB_NAME"],
        minsize=2,
        maxsize=10,
        autocommit=False,
        charset="utf8mb4",
        cursorclass=aiomysql.DictCursor,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


def get_pool() -> aiomysql.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call create_pool() first.")
    return _pool


# ---------------------------------------------------------------------------
# FastAPI lifespan (attach to app in main.py)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool()
    yield
    await close_pool()


# ---------------------------------------------------------------------------
# Context managers for use in route handlers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_conn() -> AsyncGenerator[aiomysql.Connection, None]:
    """Yield a connection from the pool. Commits on success, rolls back on error."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


@asynccontextmanager
async def get_cursor(conn: aiomysql.Connection) -> AsyncGenerator[aiomysql.DictCursor, None]:
    """Yield a DictCursor from an existing connection."""
    async with conn.cursor(aiomysql.DictCursor) as cursor:
        yield cursor


# ---------------------------------------------------------------------------
# Convenience: run a single query and return all rows
# ---------------------------------------------------------------------------

async def fetchall(query: str, args: tuple | None = None) -> list[dict]:
    async with get_conn() as conn:
        async with get_cursor(conn) as cur:
            await cur.execute(query, args)
            return await cur.fetchall()


async def fetchone(query: str, args: tuple | None = None) -> dict | None:
    async with get_conn() as conn:
        async with get_cursor(conn) as cur:
            await cur.execute(query, args)
            return await cur.fetchone()


async def execute(query: str, args: tuple | None = None) -> int:
    """Execute a write query. Returns lastrowid."""
    async with get_conn() as conn:
        async with get_cursor(conn) as cur:
            await cur.execute(query, args)
            return cur.lastrowid


async def executemany(query: str, args: list[tuple]) -> None:
    async with get_conn() as conn:
        async with get_cursor(conn) as cur:
            await cur.executemany(query, args)
