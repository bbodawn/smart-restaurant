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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE inventory (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    ingredient_id BIGINT NOT NULL UNIQUE,
    current_stock DECIMAL(12,2) NOT NULL DEFAULT 0,
    daily_sales DECIMAL(12,2) NOT NULL DEFAULT 0,
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

INSERT INTO ingredients (name, unit)
VALUES
('猪肉', 'kg'),
('鸡肉', 'kg'),
('大米', 'kg');

INSERT INTO inventory (ingredient_id, current_stock, daily_sales)
VALUES
(1, 100.00, 50.00),
(2, 200.00, 50.00),
(3, 500.00, 100.00);

-- 注意：猪肉价格设定为 31.00，历史均价 25.00，偏离度 24% (>20%) 以确保测试时稳定触发 HITL
INSERT INTO suppliers (name, ingredient_id, current_price, historical_avg_price, rating)
VALUES
('华东肉类供应商', 1, 31.00, 25.00, 4.80),
('优鲜禽业', 2, 18.00, 18.00, 4.70),
('金禾粮油', 3, 5.50, 5.20, 4.90);
