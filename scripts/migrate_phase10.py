"""Phase 10 Schema Migration（销售订单域 + 统一库存流水）。

为已有开发库增量落地 Phase 10 新表与种子；幂等、可重入、不删除任何既有数据：
- stock_movements     统一库存流水（Section 2 已建）
- dishes / dish_bom  菜品主数据 + BOM（只引用 ingredients）
- sales_orders / sales_order_items  销售订单头/明细

用法：
    python scripts/migrate_phase10.py [dbname]
环境：DB_HOST / DB_USER / DB_PASSWORD（默认 127.0.0.1 / root / 123456）

设计说明：
- CREATE TABLE IF NOT EXISTS：重复执行安全；已存在则跳过，绝不清表。
- dishes seed：仅当 dishes 为空时插入 6 道默认菜 + BOM（避免重复、不动既有数据）。
- 不触碰既有表结构与数据（Phase 8 采购闭环 / Phase 9 RBAC 不受影响）。
- init.sql 是全量重建（live 测试库 restaurant_it 用）；本脚本是增量迁移（dev 库用）。
"""
import os
import sys

import aiomysql

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "123456")
DB_NAME = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DB_NAME", "restaurant")

DDL_STOCK_MOVEMENTS = """
CREATE TABLE IF NOT EXISTS stock_movements (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    ingredient_id BIGINT NOT NULL,
    change_qty DECIMAL(12,4) NOT NULL,
    balance_after DECIMAL(12,4) NOT NULL,
    movement_type VARCHAR(24) NOT NULL COMMENT 'INBOUND / ORDER_SALE / SIMULATION_SALE / MANUAL_ADJUST',
    reference_type VARCHAR(24) NOT NULL COMMENT 'SALES_ORDER / PURCHASE_ORDER',
    reference_id BIGINT NOT NULL COMMENT '指向 reference_type 对应记录 id',
    virtual_date DATE NOT NULL,
    note VARCHAR(255) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_movement_ingredient FOREIGN KEY (ingredient_id) REFERENCES ingredients(id),
    INDEX idx_movement_type (movement_type),
    INDEX idx_movement_ing_date (ingredient_id, virtual_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

DDL_DISHES = """
CREATE TABLE IF NOT EXISTS dishes (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(50) NOT NULL UNIQUE,
    category VARCHAR(50) NULL,
    price DECIMAL(12,2) NOT NULL,
    simulation_daily_qty INT NOT NULL DEFAULT 10,
    available TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

DDL_DISH_BOM = """
CREATE TABLE IF NOT EXISTS dish_bom (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    dish_id BIGINT NOT NULL,
    ingredient_id BIGINT NOT NULL,
    qty_per_serving DECIMAL(12,4) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_bom_dish FOREIGN KEY (dish_id) REFERENCES dishes(id),
    CONSTRAINT fk_bom_ingredient FOREIGN KEY (ingredient_id) REFERENCES ingredients(id),
    CONSTRAINT uq_bom_dish_ing UNIQUE (dish_id, ingredient_id),
    CONSTRAINT chk_bom_qty CHECK (qty_per_serving > 0),
    INDEX idx_bom_dish (dish_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

DDL_SALES_ORDERS = """
CREATE TABLE IF NOT EXISTS sales_orders (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_no VARCHAR(64) NOT NULL UNIQUE,
    order_type VARCHAR(30) NOT NULL COMMENT 'ORDER_SALE / SIMULATION_SALE',
    status VARCHAR(20) NOT NULL DEFAULT 'COMPLETED',
    total_amount DECIMAL(12,2) NOT NULL,
    virtual_date DATE NOT NULL,
    created_by VARCHAR(50) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_so_order_type CHECK (order_type IN ('ORDER_SALE','SIMULATION_SALE')),
    INDEX idx_sales_order_type (order_type),
    INDEX idx_sales_order_date (virtual_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

DDL_SALES_ORDER_ITEMS = """
CREATE TABLE IF NOT EXISTS sales_order_items (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id BIGINT NOT NULL,
    dish_id BIGINT NOT NULL,
    qty INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    subtotal DECIMAL(12,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_soi_order FOREIGN KEY (order_id) REFERENCES sales_orders(id),
    CONSTRAINT fk_soi_dish FOREIGN KEY (dish_id) REFERENCES dishes(id),
    CONSTRAINT uq_soi_order_dish UNIQUE (order_id, dish_id),
    CONSTRAINT chk_soi_qty CHECK (qty > 0),
    INDEX idx_soi_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# dishes + dish_bom 种子：只在 dishes 为空时插入（幂等；名字引 ingredients 防 id 错位）
DISH_SEEDS = [
    # (菜名, 分类, 售价, simulation_daily_qty, [(ingredient_name, qty_per_serving)])
    ("土豆炖牛肉", "热菜", 38.00, 20, [("原切雪花牛肉", 0.15), ("高山土豆", 0.20)]),
    ("白灼基围虾", "热菜", 48.00, 12, [("冰鲜基围虾", 0.25)]),
    ("清炒有机菜心", "素菜", 12.00, 15, [("有机菜心", 0.30), ("非转基因大豆油", 0.02)]),
    ("东北米饭", "主食", 3.00, 40, [("东北大米", 0.20)]),
    ("香煎鸡胸肉", "热菜", 22.00, 18, [("鲜嫩鸡胸肉", 0.20), ("非转基因大豆油", 0.02)]),
    ("京葱炒肉", "热菜", 32.00, 16, [("优质猪肉", 0.20), ("招牌特调酱油", 0.02)]),
]


async def _table_exists(cur, name: str) -> bool:
    await cur.execute(
        """SELECT COUNT(*) FROM information_schema.tables
           WHERE table_schema = %s AND table_name = %s""",
        (DB_NAME, name),
    )
    row = await cur.fetchone()
    return bool(row and row[0] > 0)


async def _ensure_table(cur, name: str, ddl: str) -> None:
    if await _table_exists(cur, name):
        print(f"[step] {name} already exists (skip)")
    else:
        await cur.execute(ddl)
        print(f"[step] created {name}")


async def _seed_dishes(cur) -> None:
    await cur.execute("SELECT COUNT(*) FROM dishes")
    row = await cur.fetchone()
    if row and row[0] > 0:
        print("[step] dishes already seeded (skip)")
        return
    for name, cat, price, sim_qty, bom in DISH_SEEDS:
        await cur.execute(
            "INSERT INTO dishes (name, category, price, simulation_daily_qty) VALUES (%s,%s,%s,%s)",
            (name, cat, price, sim_qty),
        )
        await cur.execute("SELECT id FROM dishes WHERE name=%s", (name,))
        dish_id = (await cur.fetchone())[0]
        for ing_name, qty in bom:
            await cur.execute("SELECT id FROM ingredients WHERE name=%s", (ing_name,))
            ing_row = await cur.fetchone()
            if ing_row is None:
                print(f"[BLOCK] ingredient {ing_name!r} 不存在，无法建 BOM（dish={name}）。")
                sys.exit(2)
            await cur.execute(
                "INSERT INTO dish_bom (dish_id, ingredient_id, qty_per_serving) VALUES (%s,%s,%s)",
                (dish_id, ing_row[0], qty),
            )
    print("[step] seeded 6 default dishes + dish_bom")


async def run():
    conn = await aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME, autocommit=True)
    cur = await conn.cursor()
    print(f"[migrate] target db={DB_NAME}")

    await _ensure_table(cur, "stock_movements", DDL_STOCK_MOVEMENTS)
    await _ensure_table(cur, "dishes", DDL_DISHES)
    await _ensure_table(cur, "dish_bom", DDL_DISH_BOM)
    await _ensure_table(cur, "sales_orders", DDL_SALES_ORDERS)
    await _ensure_table(cur, "sales_order_items", DDL_SALES_ORDER_ITEMS)
    await _seed_dishes(cur)

    conn.close()
    print("[migrate] OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
