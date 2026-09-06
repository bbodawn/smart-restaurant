"""Phase 6-B-1 Schema Migration（v2.1 冻结方案）。

严格顺序（只对目标库执行）：
    Preflight(SELECT)
    → ADD source NULL（若缺）
    → Backfill AUTO / MANUAL（按 order_no 规则；无法确定 → BLOCK，绝不映射成 AUTO）
    → Validate（NULL / 非法值 >0 → BLOCK）
    → ALTER source SET NOT NULL
    → ADD CHECK(source IN ('AUTO','MANUAL'))
    → purchase_order_items 加 UNIQUE(order_id, ingredient_id)（先查重）
    → 新建 inbound_records（若缺）

用法：
    python scripts/migrate_phase6b1.py [dbname]
环境：DB_HOST/DB_USER/DB_PASSWORD（默认 127.0.0.1 / root / 123456）
只允许在 preflight 全部分类成功时继续；任何 UNCLASSIFIED 都 BLOCK。
"""
import os
import re
import sys

import aiomysql

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "123456")
DB_NAME = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DB_NAME", "restaurant")

AUTO_PREFIX = "PO-AUTO-"
MANUAL_RE = re.compile(r"^PO-[0-9A-F]{16}$")


async def _scalar(cur, sql, args=None):
    await cur.execute(sql, args or ())
    row = await cur.fetchone()
    return row[0] if row else None


async def _table_constraint(cur, table, name):
    return await _scalar(
        cur,
        """SELECT COUNT(*) FROM information_schema.table_constraints
           WHERE constraint_schema = %s AND table_name = %s AND constraint_name = %s""",
        (DB_NAME, table, name),
    )


async def _column_exists(cur, table, column):
    return await _scalar(
        cur,
        """SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = %s AND table_name = %s AND column_name = %s""",
        (DB_NAME, table, column),
    )


async def run():
    conn = await aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME, autocommit=True)
    cur = await conn.cursor()
    print(f"[migrate] target db={DB_NAME}")

    # 1. Preflight
    await cur.execute("SELECT id, order_no FROM purchase_orders ORDER BY id")
    rows = await cur.fetchall()
    total = len(rows)
    auto = manual = unclassified = 0
    for _id, no in rows:
        if no.startswith(AUTO_PREFIX):
            auto += 1
        elif MANUAL_RE.match(no):
            manual += 1
        else:
            unclassified += 1
            print(f"[preflight] UNCLASSIFIED id={_id} order_no={no!r}")
    print(f"[preflight] total={total} AUTO={auto} MANUAL={manual} UNCLASSIFIED={unclassified}")
    if unclassified > 0:
        print("[BLOCK] 存在无法分类的历史订单；migration 停止，等待人工确认。不得映射成 AUTO。")
        sys.exit(2)

    # 2. ADD source NULL
    if not await _column_exists(cur, "purchase_orders", "source"):
        await cur.execute("ALTER TABLE purchase_orders ADD COLUMN source VARCHAR(10) NULL")
        print("[step] added purchase_orders.source (NULL)")
    else:
        print("[step] purchase_orders.source already exists")

    # 3. Backfill
    await cur.execute(
        "UPDATE purchase_orders SET source='AUTO' WHERE source IS NULL AND order_no LIKE %s", ("PO-AUTO-%",))
    await cur.execute(
        "UPDATE purchase_orders SET source='MANUAL' WHERE source IS NULL AND order_no REGEXP %s",
        (r"^PO-[0-9A-F]{16}$",))
    print("[step] backfill done (AUTO=PO-AUTO-% , MANUAL=^PO-[0-9A-F]{16}$)")

    # 4. Validate
    bad = await _scalar(
        cur,
        "SELECT COUNT(*) FROM purchase_orders WHERE source IS NULL OR source NOT IN ('AUTO','MANUAL')")
    print(f"[validate] NULL/invalid source = {bad}")
    if bad > 0:
        print("[BLOCK] source 校验未通过（存在 NULL 或非法值）。")
        sys.exit(2)

    # 5. NOT NULL
    await cur.execute("ALTER TABLE purchase_orders MODIFY COLUMN source VARCHAR(10) NOT NULL")
    print("[step] source SET NOT NULL")

    # 6. CHECK
    if not await _table_constraint(cur, "purchase_orders", "chk_po_source"):
        await cur.execute(
            "ALTER TABLE purchase_orders ADD CONSTRAINT chk_po_source CHECK (source IN ('AUTO','MANUAL'))")
        print("[step] added chk_po_source")
    else:
        print("[step] chk_po_source already exists")

    # 7. purchase_order_items UNIQUE
    dup = await _scalar(
        cur,
        """SELECT COUNT(*) FROM (SELECT order_id, ingredient_id FROM purchase_order_items
           GROUP BY order_id, ingredient_id HAVING COUNT(*)>1) t""")
    print(f"[items] duplicate (order_id,ingredient_id) groups = {dup}")
    if dup > 0:
        print("[BLOCK] 存在同单同食材多行，无法安全加 UNIQUE。")
        sys.exit(2)
    if not await _table_constraint(cur, "purchase_order_items", "uq_po_item"):
        await cur.execute(
            "ALTER TABLE purchase_order_items ADD CONSTRAINT uq_po_item UNIQUE (order_id, ingredient_id)")
        print("[step] added uq_po_item")
    else:
        print("[step] uq_po_item already exists")

    # 8. inbound_records
    if not await _table_constraint(cur, "inbound_records", "uq_inbound_order_item") or \
       not await _scalar(cur, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s AND table_name='inbound_records'", (DB_NAME,)):
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS inbound_records (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                record_no VARCHAR(64) NOT NULL,
                order_id BIGINT NOT NULL,
                order_item_id BIGINT NOT NULL,
                ingredient_id BIGINT NOT NULL,
                inbound_qty DECIMAL(12,2) NOT NULL,
                unit_price DECIMAL(12,2) NOT NULL,
                total_price DECIMAL(12,2) NOT NULL,
                supplier_id BIGINT NOT NULL,
                inbound_virtual_date DATE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_inbound_record_no UNIQUE (record_no),
                CONSTRAINT uq_inbound_order_item UNIQUE (order_item_id),
                CONSTRAINT chk_inbound_qty CHECK (inbound_qty > 0),
                CONSTRAINT chk_inbound_prices CHECK (unit_price >= 0 AND total_price >= 0),
                CONSTRAINT fk_inbound_order FOREIGN KEY (order_id) REFERENCES purchase_orders(id),
                CONSTRAINT fk_inbound_order_item FOREIGN KEY (order_item_id) REFERENCES purchase_order_items(id),
                CONSTRAINT fk_inbound_ingredient FOREIGN KEY (ingredient_id) REFERENCES ingredients(id),
                CONSTRAINT fk_inbound_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
                INDEX idx_inbound_order (order_id),
                INDEX idx_inbound_ingredient (ingredient_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        print("[step] created inbound_records")
    else:
        print("[step] inbound_records already exists")

    # 9. 最终校验
    nulls = await _scalar(cur, "SELECT COUNT(*) FROM purchase_orders WHERE source IS NULL")
    invalid = await _scalar(cur, "SELECT COUNT(*) FROM purchase_orders WHERE source NOT IN ('AUTO','MANUAL')")
    print(f"[final] source NULL={nulls} invalid={invalid}")
    conn.close()
    print("[migrate] OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
