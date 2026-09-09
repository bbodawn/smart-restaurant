CREATE DATABASE IF NOT EXISTS restaurant
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE restaurant;

SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS sales_order_items;
DROP TABLE IF EXISTS dish_bom;
DROP TABLE IF EXISTS sales_orders;
DROP TABLE IF EXISTS dishes;
DROP TABLE IF EXISTS stock_movements;
DROP TABLE IF EXISTS inbound_records;
DROP TABLE IF EXISTS purchase_order_items;
DROP TABLE IF EXISTS purchase_orders;
DROP TABLE IF EXISTS suppliers;
DROP TABLE IF EXISTS inventory;
DROP TABLE IF EXISTS ingredients;

SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE ingredients (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(50) NOT NULL UNIQUE,
    unit VARCHAR(20) NOT NULL,
    category VARCHAR(50) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE inventory (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    ingredient_id BIGINT NOT NULL UNIQUE,
    current_stock DECIMAL(12,2) NOT NULL DEFAULT 0,
    daily_sales DECIMAL(12,2) NOT NULL DEFAULT 0,
    safety_stock DECIMAL(12,2) NOT NULL DEFAULT 30.00,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_inventory_ingredient
        FOREIGN KEY (ingredient_id)
        REFERENCES ingredients(id)
) ENGINE=InnoDB;

CREATE TABLE suppliers (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    ingredient_id BIGINT NOT NULL,
    current_price DECIMAL(12,2) NOT NULL,
    historical_avg_price DECIMAL(12,2) NOT NULL,
    rating DECIMAL(3,2) DEFAULT 5.00,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_supplier_ingredient
        FOREIGN KEY (ingredient_id)
        REFERENCES ingredients(id),

    INDEX idx_supplier_ingredient (ingredient_id)
) ENGINE=InnoDB;

CREATE TABLE purchase_orders (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_no VARCHAR(64) NOT NULL UNIQUE,
    thread_id VARCHAR(128) NOT NULL UNIQUE,
    status VARCHAR(30) NOT NULL DEFAULT 'RUNNING',
    source VARCHAR(10) NOT NULL COMMENT '采购来源: AUTO / MANUAL（Phase 6-B-1）',
    total_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    risk_reason VARCHAR(500) NULL,
    risk_analysis_report TEXT NULL,
    demand_reasoning TEXT NULL,
    suspended_virtual_date DATE NULL,
    completed_at DATE NULL,
    approval_reason VARCHAR(255) NULL,
    approved_virtual_date DATE NULL,
    rejected_virtual_date DATE NULL,
    agent5_summary TEXT NULL COMMENT 'REVIEW 决策时 Agent5 快照(Phase 6-C)',
    agent5_risk_level VARCHAR(16) NULL,
    agent5_risk_analysis TEXT NULL,
    agent5_recommendation TEXT NULL,
    idempotency_key VARCHAR(128) NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT chk_po_source CHECK (source IN ('AUTO','MANUAL')),
    INDEX idx_purchase_status (status),
    INDEX idx_purchase_thread (thread_id)
) ENGINE=InnoDB;

CREATE TABLE purchase_order_items (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id BIGINT NOT NULL,
    ingredient_id BIGINT NOT NULL,
    quantity DECIMAL(12,2) NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    total_price DECIMAL(12,2) NOT NULL,
    supplier_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_item_order
        FOREIGN KEY (order_id)
        REFERENCES purchase_orders(id),

    CONSTRAINT fk_item_ingredient
        FOREIGN KEY (ingredient_id)
        REFERENCES ingredients(id),

    CONSTRAINT fk_item_supplier
        FOREIGN KEY (supplier_id)
        REFERENCES suppliers(id),

    CONSTRAINT uq_po_item UNIQUE (order_id, ingredient_id),

    INDEX idx_item_order (order_id)
) ENGINE=InnoDB;

CREATE TABLE inbound_records (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    record_no VARCHAR(64) NOT NULL COMMENT '入库单号 = INBOUND-YYYYMMDD-order_item_id(8位)',
    order_id BIGINT NOT NULL COMMENT '冗余父订单引用（一致性由 service 单点构造保证）',
    order_item_id BIGINT NOT NULL COMMENT '对应采购明细，1:1',
    ingredient_id BIGINT NOT NULL COMMENT '冗余，便于直查',
    inbound_qty DECIMAL(12,2) NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    total_price DECIMAL(12,2) NOT NULL,
    supplier_id BIGINT NOT NULL COMMENT '快照冗余',
    inbound_virtual_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_inbound_record_no UNIQUE (record_no),
    CONSTRAINT uq_inbound_order_item UNIQUE (order_item_id),
    CONSTRAINT chk_inbound_qty CHECK (inbound_qty > 0),
    CONSTRAINT chk_inbound_prices CHECK (unit_price >= 0 AND total_price >= 0),
    CONSTRAINT fk_inbound_order
        FOREIGN KEY (order_id) REFERENCES purchase_orders(id),
    CONSTRAINT fk_inbound_order_item
        FOREIGN KEY (order_item_id) REFERENCES purchase_order_items(id),
    CONSTRAINT fk_inbound_ingredient
        FOREIGN KEY (ingredient_id) REFERENCES ingredients(id),
    CONSTRAINT fk_inbound_supplier
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id),

    INDEX idx_inbound_order (order_id),
    INDEX idx_inbound_ingredient (ingredient_id)
) ENGINE=InnoDB;

-- 统一库存流水（Phase 10）：入/出库唯一事实记录，change_qty 正入负出，
-- balance_after 为该笔后的库存快照；movement_type 表达库存变化原因，
-- reference_type+reference_id 指向触发事件（SALES_ORDER / PURCHASE_ORDER）。
CREATE TABLE stock_movements (
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

    CONSTRAINT fk_movement_ingredient
        FOREIGN KEY (ingredient_id) REFERENCES ingredients(id),

    INDEX idx_movement_type (movement_type),
    INDEX idx_movement_ing_date (ingredient_id, virtual_date)
) ENGINE=InnoDB;

-- 菜品主数据（Phase 10）：售价 + 冷启动模拟销量基数；BOM 只引用 ingredients
CREATE TABLE dishes (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(50) NOT NULL UNIQUE,
    category VARCHAR(50) NULL,
    price DECIMAL(12,2) NOT NULL,
    simulation_daily_qty INT NOT NULL DEFAULT 10 COMMENT 'SIMULATION_SALE 冷启动每日模拟销量',
    available TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- 菜品 BOM：dish × ingredient，qty_per_serving 按食材自身单位（kg/L），每 1 份用量
CREATE TABLE dish_bom (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    dish_id BIGINT NOT NULL,
    ingredient_id BIGINT NOT NULL,
    qty_per_serving DECIMAL(12,4) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_bom_dish
        FOREIGN KEY (dish_id) REFERENCES dishes(id),
    CONSTRAINT fk_bom_ingredient
        FOREIGN KEY (ingredient_id) REFERENCES ingredients(id),

    CONSTRAINT uq_bom_dish_ing UNIQUE (dish_id, ingredient_id),
    CONSTRAINT chk_bom_qty CHECK (qty_per_serving > 0),

    INDEX idx_bom_dish (dish_id)
) ENGINE=InnoDB;

-- 销售订单头（Phase 10）：ORDER_SALE 真实点单 / SIMULATION_SALE 快进模拟，即时成交 COMPLETED
CREATE TABLE sales_orders (
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
) ENGINE=InnoDB;

-- 销售订单明细（菜品级；unit_price 售价快照，防历史漂移）
CREATE TABLE sales_order_items (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id BIGINT NOT NULL,
    dish_id BIGINT NOT NULL,
    qty INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    subtotal DECIMAL(12,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_soi_order
        FOREIGN KEY (order_id) REFERENCES sales_orders(id),
    CONSTRAINT fk_soi_dish
        FOREIGN KEY (dish_id) REFERENCES dishes(id),

    CONSTRAINT uq_soi_order_dish UNIQUE (order_id, dish_id),
    CONSTRAINT chk_soi_qty CHECK (qty > 0),

    INDEX idx_soi_order (order_id)
) ENGINE=InnoDB;

-- ============ Day 4 种子数据：10 种典型餐饮食材 ============
INSERT INTO ingredients (name, unit, category)
VALUES
('东北大米', 'kg', '主食'),
('面粉', 'kg', '主食'),
('优质猪肉', 'kg', '生鲜肉类'),
('鲜嫩鸡胸肉', 'kg', '生鲜肉类'),
('原切雪花牛肉', 'kg', '生鲜肉类'),
('冰鲜基围虾', 'kg', '水产海鲜'),
('有机菜心', 'kg', '蔬菜时蔬'),
('高山土豆', 'kg', '蔬菜时蔬'),
('非转基因大豆油', 'L', '调料'),
('招牌特调酱油', 'L', '调料');

-- 库存：当前库存、日消耗、安全线（默认已为健康状态：current_stock > safety_stock）
INSERT INTO inventory (ingredient_id, current_stock, daily_sales, safety_stock)
VALUES
(1, 500.00, 20.00, 50.00),
(2, 200.00, 10.00, 30.00),
(3, 120.00, 50.00, 30.00),
(4, 150.00, 30.00, 30.00),
(5, 80.00, 15.00, 20.00),
(6, 60.00, 12.00, 15.00),
(7, 40.00, 15.00, 10.00),
(8, 100.00, 15.00, 20.00),
(9, 100.00, 5.00, 20.00),
(10, 50.00, 3.00, 10.00);

-- 供应商：猪肉 / 牛的现价相对历史均价偏离 >20%，快速/大额场景下可稳定触发 HITL
INSERT INTO suppliers (name, ingredient_id, current_price, historical_avg_price, rating)
VALUES
('金禾粮油', 1, 6.00, 5.80, 4.90),
('金麦面业', 2, 5.00, 4.80, 4.70),
('华东肉类供应商', 3, 32.00, 25.00, 4.80),
('优鲜禽业', 4, 18.00, 17.50, 4.70),
('澳洲和牛直供', 5, 88.00, 70.00, 4.90),
('南海水产', 6, 65.00, 63.00, 4.60),
('绿蔬农场', 7, 8.00, 7.50, 4.80),
('高原薯业', 8, 4.00, 3.80, 4.50),
('中粮油脂', 9, 12.00, 11.50, 4.90),
('粤珍调味', 10, 15.00, 14.50, 4.70);

-- ============ Phase 10 种子菜品：6 道（BOM 只引用以上 10 种食材） ============
INSERT INTO dishes (name, category, price, simulation_daily_qty) VALUES
('土豆炖牛肉', '热菜', 38.00, 20),
('白灼基围虾', '热菜', 48.00, 12),
('清炒有机菜心', '素菜', 12.00, 15),
('东北米饭', '主食', 3.00, 40),
('香煎鸡胸肉', '热菜', 22.00, 18),
('京葱炒肉', '热菜', 32.00, 16);

-- BOM（每 1 份用量，单位跟随食材：kg / L）
INSERT INTO dish_bom (dish_id, ingredient_id, qty_per_serving) VALUES
((SELECT id FROM dishes WHERE name='土豆炖牛肉'),  (SELECT id FROM ingredients WHERE name='原切雪花牛肉'), 0.15),
((SELECT id FROM dishes WHERE name='土豆炖牛肉'),  (SELECT id FROM ingredients WHERE name='高山土豆'),     0.20),
((SELECT id FROM dishes WHERE name='白灼基围虾'),  (SELECT id FROM ingredients WHERE name='冰鲜基围虾'),   0.25),
((SELECT id FROM dishes WHERE name='清炒有机菜心'),(SELECT id FROM ingredients WHERE name='有机菜心'),     0.30),
((SELECT id FROM dishes WHERE name='清炒有机菜心'),(SELECT id FROM ingredients WHERE name='非转基因大豆油'), 0.02),
((SELECT id FROM dishes WHERE name='东北米饭'),    (SELECT id FROM ingredients WHERE name='东北大米'),     0.20),
((SELECT id FROM dishes WHERE name='香煎鸡胸肉'),  (SELECT id FROM ingredients WHERE name='鲜嫩鸡胸肉'),   0.20),
((SELECT id FROM dishes WHERE name='香煎鸡胸肉'),  (SELECT id FROM ingredients WHERE name='非转基因大豆油'), 0.02),
((SELECT id FROM dishes WHERE name='京葱炒肉'),    (SELECT id FROM ingredients WHERE name='优质猪肉'),     0.20),
((SELECT id FROM dishes WHERE name='京葱炒肉'),    (SELECT id FROM ingredients WHERE name='招牌特调酱油'), 0.02);
