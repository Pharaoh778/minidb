-- ============================================================
-- MiniDB 增删改查（CRUD）全流程演示脚本
-- ------------------------------------------------------------
-- 与 test_data.sql 的区别：
--   test_data.sql  基线数据脚本，只建表 + 插入，可反复执行重置
--   test_crud.sql  演示脚本，真跑 SELECT / UPDATE / DELETE，会改数据
--
-- 执行方式：
--   python run_test_data.py test_crud.sql -d demo_data
--   （用独立目录，避免污染 data/ 里的基线数据）
--
-- 脚本结束后 crud_demo 应剩余 2 行，启动器会自动校验。
-- ============================================================

-- ---------- 0. 清理（保证可重复运行） ----------
DROP TABLE IF EXISTS crud_demo;

-- ---------- 1. 建表 CREATE ----------
CREATE TABLE crud_demo (
    id INT PRIMARY KEY,
    label VARCHAR(50) NOT NULL,
    qty INT,
    price FLOAT,
    tag VARCHAR(20),
    in_stock BOOL
);

-- ---------- 2. 增 INSERT ----------
-- 多行批量插入；含中文、NULL、BOOL、各种数值
INSERT INTO crud_demo(id,label,qty,price,tag,in_stock) VALUES
 (1,'apple',10,3.5,'fruit',TRUE),
 (2,'banana',20,2.0,'fruit',TRUE),
 (3,'胡萝卜',15,1.5,'veg',TRUE),
 (4,'donut',5,6.0,'snack',FALSE),
 (5,'egg',30,0.5,'protein',TRUE),
 (6,'fish',8,12.5,'protein',TRUE),
 (7,'grape',12,8.8,'fruit',FALSE),
 (8,'honey',3,25.0,NULL,TRUE);

-- ---------- 3. 查 SELECT ----------
SELECT * FROM crud_demo;                                    -- 全表：8 行

SELECT id, label, price FROM crud_demo WHERE price > 5.0;   -- 投影 + 比较：id=6,7,8
SELECT * FROM crud_demo WHERE tag = 'fruit';                -- 等值：id=1,2,7
SELECT * FROM crud_demo WHERE qty >= 10 AND in_stock;       -- 复合条件
SELECT * FROM crud_demo WHERE tag IN ('fruit','veg');       -- IN：id=1,2,3,7
SELECT * FROM crud_demo WHERE label LIKE 'a%';              -- LIKE 前缀：apple
SELECT * FROM crud_demo WHERE tag IS NULL;                  -- IS NULL：id=8
SELECT * FROM crud_demo WHERE tag IS NOT NULL AND qty < 10; -- IS NOT NULL + 比较
SELECT id, label, qty * price AS amount FROM crud_demo WHERE id <= 3;  -- 表达式投影(带别名)
SELECT * FROM crud_demo ORDER BY price DESC;                -- 降序
SELECT * FROM crud_demo ORDER BY tag ASC, price DESC;       -- 多键排序
SELECT * FROM crud_demo ORDER BY price DESC LIMIT 3;        -- LIMIT 截断

-- ---------- 4. 改 UPDATE ----------
UPDATE crud_demo SET qty = 100 WHERE id = 1;                -- 单列，影响 1 行
SELECT id, label, qty FROM crud_demo WHERE id = 1;

UPDATE crud_demo SET qty = 0, price = 0.0 WHERE tag = 'veg';-- 多列赋值，影响 1 行（id=3）
SELECT id, label, qty, price FROM crud_demo WHERE id = 3;

UPDATE crud_demo SET price = price * 2 WHERE id >= 5;       -- 基于自身计算，影响 4 行
SELECT id, label, price FROM crud_demo WHERE id >= 5;

UPDATE crud_demo SET tag = 'fruit' WHERE tag IS NULL;       -- 更新 NULL 列，影响 1 行（id=8）
SELECT id, label, tag FROM crud_demo WHERE id = 8;

-- ---------- 5. 删 DELETE ----------
DELETE FROM crud_demo WHERE id = 4;                         -- 单行删除
SELECT * FROM crud_demo;                                    -- 剩 7 行

DELETE FROM crud_demo WHERE tag = 'fruit';                  -- 多行删除：id=1,2,7,8
SELECT * FROM crud_demo;                                    -- 剩 3 行：3,5,6

DELETE FROM crud_demo WHERE price < 2.0 AND qty > 10;       -- 复合条件：仅 id=5 满足
SELECT * FROM crud_demo;                                    -- 最终剩 2 行：id=3,6

-- ---------- 6. 元数据 ----------
SHOW TABLES;
DESC crud_demo;

-- ============================================================
-- 附：预期报错的用例（放入脚本会导致执行失败，故仅作参考）
-- ------------------------------------------------------------
--   UPDATE crud_demo SET id = 6 WHERE id = 3;               -- 主键冲突（id=6 已存在）
--   UPDATE crud_demo SET nope = 1 WHERE id = 3;             -- 语义错误：列不存在
--   INSERT INTO crud_demo(id,label) VALUES (6,'重复');      -- 主键冲突
--   INSERT INTO crud_demo(id,qty) VALUES (99,1);            -- label 违反 NOT NULL
--   SELECT nope FROM crud_demo;                             -- 语义错误：列不存在
--   DELETE FROM crud_demo WHERE id = 999;                   -- 合法但影响 0 行
-- ============================================================
