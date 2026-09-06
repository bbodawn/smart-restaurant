"""LIVE E2E 测试基础设施（真实 MySQL + Redis）。

证据标签：本目录用例只有在 TEST_LIVE=1 时才运行并记为 LIVE E2E；
未开启 / 基础设施不可达时全部 skip，报告口径记为 NOT RUN（绝不假报 PASS）。

数据库隔离：
- 默认使用独立测试库 restaurant_it（从 sql/init.sql 重建），
  【不触碰】开发库 restaurant 与已有测试库 restaurant_test。
- Redis 用 db 0（LangGraph checkpoint 要求 db 0），不 flush，靠 uuid thread 隔离。

环境变量：
- TEST_LIVE=1  开启
- TEST_MYSQL_URL  默认 mysql+aiomysql://root:123456@127.0.0.1:3306/restaurant_it
- TEST_REDIS_URL  默认 redis://127.0.0.1:6379/0
"""
import asyncio
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiomysql
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = ROOT / "sql" / "init.sql"

MYSQL_URL = os.getenv("TEST_MYSQL_URL", "mysql+aiomysql://root:123456@127.0.0.1:3306/restaurant_it")
# 注意：LangGraph Redis checkpoint（redisvl）不允许在 db != 0 建索引，因此测试统一用 db 0，
# 与应用一致。会话内【不 flush】，靠 uuid thread_id 隔离，避免清空应用开发数据。
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
_ENABLED = os.getenv("TEST_LIVE") in ("1", "true", "True", "yes")


@pytest.fixture(autouse=True)
def _require_live():
    """整组兜底：未开启 TEST_LIVE 时，本目录所有用例一律 skip（报告口径 NOT RUN）。"""
    if not _ENABLED:
        pytest.skip("LIVE E2E 未开启：设置 TEST_LIVE=1（需要 MySQL + Redis）")


def _mysql_conn_params():
    u = urlsplit(MYSQL_URL.replace("mysql+aiomysql://", "mysql://", 1))
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 3306,
        "user": u.username or "root",
        "password": u.password or "",
        "dbname": (u.path or "").lstrip("/") or "restaurant_it",
    }


async def _bootstrap_db():
    p = _mysql_conn_params()
    conn = await aiomysql.connect(host=p["host"], port=p["port"], user=p["user"], password=p["password"], autocommit=True)
    try:
        cur = await conn.cursor()
        await cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{p['dbname']}` DEFAULT CHARACTER SET utf8mb4"
        )
        await cur.execute(f"USE `{p['dbname']}`")
        sql = INIT_SQL.read_text(encoding="utf-8-sig")
        for stmt in re.split(r";\s*\n", sql):
            s = stmt.strip()
            if not s or s.upper().startswith("CREATE DATABASE") or s.upper().startswith("USE "):
                continue
            await cur.execute(s)
        # 清掉 init.sql 的 10 条默认种子，避免干扰候选扫描；只保留本套件播种的食材
        for t in ("inbound_records", "purchase_order_items", "purchase_orders", "suppliers", "inventory", "ingredients"):
            await cur.execute(f"DELETE FROM `{t}`")
        await cur.close()
    finally:
        conn.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _live_prepare():
    """开启时：在共享事件循环内重置 restaurant_it 表结构与种子。
    Redis 用 db 0、不 flush（uuid 隔离），避免误清应用开发数据。"""
    if not _ENABLED:
        return
    await _bootstrap_db()


@pytest_asyncio.fixture
async def _engine():
    """函数级 async engine：与用例运行在同一事件循环，避免跨 loop 复用连接。"""
    if not _ENABLED:
        pytest.skip("LIVE E2E 未开启：设置 TEST_LIVE=1")
    # NullPool：每次会话用独立连接、随会话关闭，避免跨 pytest-asyncio 事件循环复用连接
    engine = create_async_engine(MYSQL_URL, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def _session_factory(_engine):
    if not _ENABLED:
        pytest.skip("LIVE E2E 未开启：设置 TEST_LIVE=1")
    yield async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(_session_factory):
    if not _ENABLED:
        pytest.skip("LIVE E2E 未开启：设置 TEST_LIVE=1")
    async with _session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def graph():
    """真实 AsyncRedisSaver 编译的主图（LIVE checkpoint）。"""
    if not _ENABLED:
        pytest.skip("LIVE E2E 未开启：设置 TEST_LIVE=1")
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    from app.graph.workflow import build_graph

    async with AsyncRedisSaver.from_conn_string(REDIS_URL) as saver:
        await saver.asetup()
        g = build_graph(saver)
        yield g


async def _seed(db: AsyncSession, *, stock, daily, safety, price, hist) -> dict:
    """在测试库插入一个独立食材（唯一名/唯一 ingredient_id），返回快照。"""
    name = f"LIVE-{uuid.uuid4().hex[:10]}"
    await db.execute(
        text("INSERT INTO ingredients (name, unit, category) VALUES (:n, 'kg', '测试')"),
        {"n": name},
    )
    ing_id = int((await db.execute(text("SELECT id FROM ingredients WHERE name = :n"), {"n": name})).scalar_one())
    await db.execute(
        text("""INSERT INTO inventory (ingredient_id, current_stock, daily_sales, safety_stock)
                VALUES (:i, :s, :d, :f)"""),
        {"i": ing_id, "s": stock, "d": daily, "f": safety},
    )
    await db.execute(
        text("""INSERT INTO suppliers (name, ingredient_id, current_price, historical_avg_price, rating)
                VALUES (:n, :i, :p, :h, 4.5)"""),
        {"n": name + "供应商", "i": ing_id, "p": price, "h": hist},
    )
    sup_id = int((await db.execute(text("SELECT id FROM suppliers WHERE ingredient_id = :i"), {"i": ing_id})).scalar_one())
    await db.commit()
    return {
        "ingredient_id": ing_id,
        "name": name,
        "supplier_id": sup_id,
        "current_stock": float(stock),
        "daily_sales": float(daily),
        "safety_stock": float(safety),
        "price": float(price),
        "historical_price": float(hist),
    }


@pytest.fixture
def seed_ingredient():
    """用法：ing = await seed_ingredient(db, stock=.., daily=.., safety=.., price=.., hist=..)"""
    return _seed
