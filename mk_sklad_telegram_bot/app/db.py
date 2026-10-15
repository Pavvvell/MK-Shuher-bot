import json
import asyncpg

DDL = """
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id BIGINT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS pending_actions (
    user_id BIGINT PRIMARY KEY,
    action_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id BIGINT NOT NULL,
    action_type TEXT NOT NULL,
    entity_id TEXT,
    payload JSONB NOT NULL,
    result TEXT NOT NULL
);
"""

async def prepare(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(DDL)

async def take_update(pool, update_id: int) -> bool:
    async with pool.acquire() as connection:
        result = await connection.fetchval(
            "INSERT INTO telegram_updates (update_id) VALUES ($1) "
            "ON CONFLICT (update_id) DO NOTHING RETURNING update_id", update_id
        )
    return result is not None

async def release_update(pool, update_id: int) -> None:
    async with pool.acquire() as connection:
        await connection.execute("DELETE FROM telegram_updates WHERE update_id=$1", update_id)

async def stage(pool, user_id: int, action_type: str, payload: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_actions(user_id,action_type,payload,created_at) "
            "VALUES($1,$2,$3::jsonb,NOW()) "
            "ON CONFLICT(user_id) DO UPDATE SET action_type=EXCLUDED.action_type, "
            "payload=EXCLUDED.payload,created_at=NOW()",
            user_id, action_type, json.dumps(payload, ensure_ascii=False)
        )

async def pending(pool, user_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action_type,payload FROM pending_actions "
            "WHERE user_id=$1 AND created_at > NOW() - INTERVAL '30 minutes'", user_id
        )
    return dict(row) if row else None

async def clear_pending(pool, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_actions WHERE user_id=$1", user_id)

async def audit(pool, user_id: int, action: str, entity_id: str, payload: dict, result: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO audit_events(user_id,action_type,entity_id,payload,result) "
            "VALUES($1,$2,$3,$4::jsonb,$5)",
            user_id, action, entity_id, json.dumps(payload, ensure_ascii=False), result
        )
