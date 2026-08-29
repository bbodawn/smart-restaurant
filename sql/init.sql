CREATE DATABASE IF NOT EXISTS restaurant
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE restaurant;

SET FOREIGN_KEY_CHECKS = 0;

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
    total_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    risk_reason VARCHAR(500) NULL,
    risk_analysis_report TEXT NULL,
    demand_reasoning TEXT NULL,
    suspended_virtual_date DATE NULL,
    completed_at DATE NULL,
    idempotency_key VARCHAR(128) NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

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

    INDEX idx_item_order (order_id)
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
