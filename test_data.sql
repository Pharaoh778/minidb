-- ============================================================
-- MiniDB 增删改查（CRUD）功能测试数据
-- ------------------------------------------------------------
-- 用途：为 DDL / INSERT / SELECT / UPDATE / DELETE 等功能的手工
--       测试与答辩演示，提供一套统一、可重复执行的基线数据。
--
-- 执行方式（推荐：一键运行，会自动校验各表行数）
--   run_test_data.bat                      -- Windows 双击即可
--   python run_test_data.py                -- 跨平台，数据写入 data/
--   python run_test_data.py --clean        -- 先清空数据目录，彻底重来
--   python run_test_data.py -d demo_data -s FIFO -p 8
--
-- 等价的底层命令（需在项目根目录下）：
--   python -m database_system.cli.main -f test_data.sql
--   python -m database_system.cli.main -f test_data.sql -d data
--
-- 脚本以 DROP TABLE IF EXISTS 开头，可反复执行，每次重置为初始数据。
-- 启用统计：进入 REPL 后输入 .stats，可观察缓冲池命中率随查询变化。
--
-- 表结构一览
--   student     12 行  基础主表：类型覆盖 / NULL / 中文 / 特殊字符串
--   course       6 行  独立表：验证多张表并存与各自查询
--   employee    10 行  更新删除重点表：含 NOT NULL 与可空列
--   crud_lab     8 行  专用试验表：UPDATE / DELETE 只在此表进行，不污染其他表
--   type_probe   4 行  类型边界：负数 / 极值 / 科学计数 / 空串 / NULL
--   seq_num     30 行  行数较多：ORDER BY / LIMIT / 多页扫描
-- ============================================================

-- ---------- 0. 清理旧数据（保证脚本可重复运行） ----------
DROP TABLE IF EXISTS student;
DROP TABLE IF EXISTS course;
DROP TABLE IF EXISTS employee;
DROP TABLE IF EXISTS crud_lab;
DROP TABLE IF EXISTS type_probe;
DROP TABLE IF EXISTS seq_num;

-- ---------- 1. student：基础主表 ----------
-- 覆盖点：主键、NOT NULL(name)、可空列、INT/FLOAT/TEXT/BOOL 四种类型、
--         中文、NULL 值、空字符串、字符串内单引号（''）、字符串内分号(;)
CREATE TABLE student (
    id INT PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    gender VARCHAR(10),
    age INT,
    score FLOAT,
    class_name VARCHAR(50),
    is_monitor BOOL,
    enroll_date VARCHAR(20),
    remark VARCHAR(200)
);

INSERT INTO student(id,name,gender,age,score,class_name,is_monitor,enroll_date,remark) VALUES
 (1,'张三','男',18,95.5,'一班',TRUE, '2023-09-01','英语课代表'),
 (2,'李四','男',19,88.0,'一班',FALSE,'2023-09-01',NULL),
 (3,'王五','男',20,76.5,'二班',FALSE,'2023-09-01','数学竞赛一等奖'),
 (4,'赵六','女',18,92.0,'一班',TRUE, '2023-09-02','班长;兼学习委员'),
 (5,'钱七','女',21,85.5,'二班',FALSE,'2023-09-02','Tom''s project partner'),
 (6,'孙八','男',22,NULL,'三班',FALSE,'2023-09-03','缺考'),
 (7,'周九','女',19,60.0,'三班',FALSE,'2023-09-03',''),
 (8,'吴十','男',20,100.0,'二班',TRUE,'2023-09-04','满分'),
 (9,'郑一','女',18,45.5,'一班',FALSE,'2023-09-04','需补考'),
 (10,'王二','男',23,78.0,NULL,TRUE,'2023-09-05','转专业生'),
 (11,'陈三','女',19,88.5,'三班',FALSE,'2023-09-05',NULL),
 (12,'刘四','男',20,NULL,'二班',FALSE,'2023-09-06','缓考');

-- ---------- 2. course：第二张独立表 ----------
CREATE TABLE course (
    id INT PRIMARY KEY,
    cname VARCHAR(50) NOT NULL,
    credit FLOAT,
    teacher VARCHAR(50),
    hours INT
);

INSERT INTO course(id,cname,credit,teacher,hours) VALUES
 (1,'数据库系统',3.5,'王老师',64),
 (2,'编译原理',4.0,'李老师',72),
 (3,'操作系统',3.5,'张老师',64),
 (4,'数据结构',4.5,'刘老师',80),
 (5,'计算机网络',3.0,'陈老师',56),
 (6,'软件工程',2.5,'赵老师',48);

-- ---------- 3. employee：UPDATE / DELETE 重点表 ----------
-- 覆盖点：NOT NULL、可空外键式列（bonus/phone 为 NULL）、多列赋值更新、范围删除
CREATE TABLE employee (
    id INT PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    dept VARCHAR(30),
    salary FLOAT,
    bonus FLOAT,
    hiredate VARCHAR(20),
    is_manager BOOL,
    phone VARCHAR(20)
);

INSERT INTO employee(id,name,dept,salary,bonus,hiredate,is_manager,phone) VALUES
 (1,'张伟','研发',18000.0,3000.0,'2020-03-01',TRUE, '13800000001'),
 (2,'李娜','研发',16500.5,NULL,  '2021-07-15',FALSE,'13800000002'),
 (3,'王强','销售',12000.0,8000.0,'2019-01-20',TRUE, NULL),
 (4,'赵敏','销售',11000.0,6500.0,'2022-05-10',FALSE,'13800000004'),
 (5,'孙悦','人事',9500.0, 1200.0,'2023-02-01',FALSE,'13800000005'),
 (6,'周杰','财务',10500.0,NULL,  '2021-11-11',TRUE, '13800000006'),
 (7,'吴迪','研发',20000.0,5000.0,'2018-08-08',FALSE,'13800000007'),
 (8,'郑爽','运营',8800.0, 900.0, '2023-06-30',FALSE,NULL),
 (9,'陈刚','研发',15000.0,2500.0,'2020-12-25',TRUE, '13800000009'),
 (10,'刘洋','运营',8000.0,NULL,  '2024-01-05',FALSE,'13800000010');

-- ---------- 4. crud_lab：专用试验表 ----------
-- UPDATE / DELETE 的破坏性测试只在这张表上做，保证 student/employee 数据干净
CREATE TABLE crud_lab (
    id INT PRIMARY KEY,
    label VARCHAR(50),
    qty INT,
    price FLOAT,
    tag VARCHAR(20)
);

INSERT INTO crud_lab(id,label,qty,price,tag) VALUES
 (1,'apple',10,3.5,'fruit'),
 (2,'banana',20,2.0,'fruit'),
 (3,'carrot',15,1.5,'veg'),
 (4,'donut',5,6.0,'snack'),
 (5,'egg',30,0.5,'protein'),
 (6,'fish',8,12.5,'protein'),
 (7,'grape',12,8.8,'fruit'),
 (8,'honey',3,25.0,NULL);

-- ---------- 5. type_probe：类型与边界 ----------
-- 覆盖点：0 值、负数、INT32 极值、高精度 FLOAT、科学计数法、空串、NULL
CREATE TABLE type_probe (
    id INT PRIMARY KEY,
    int_col INT,
    float_col FLOAT,
    text_col VARCHAR(100),
    bool_col BOOL
);

INSERT INTO type_probe(id,int_col,float_col,text_col,bool_col) VALUES
 (1,0,0.0,'',FALSE),
 (2,-12345,-0.001,'负数与极小数',TRUE),
 (3,2147483647,3.141592653589793,'整型最大值',TRUE),
 (4,-2147483647,1.5e3,NULL,FALSE);

-- ---------- 6. seq_num：行数较多的表 ----------
-- 覆盖点：ORDER BY 升降序、多键排序、LIMIT 截断、跨页扫描
CREATE TABLE seq_num (
    id INT PRIMARY KEY,
    square INT,
    val FLOAT
);

INSERT INTO seq_num(id,square,val) VALUES
 (1,1,2.5),     (2,4,5.0),     (3,9,7.5),     (4,16,10.0),   (5,25,12.5),
 (6,36,15.0),   (7,49,17.5),   (8,64,20.0),   (9,81,22.5),   (10,100,25.0),
 (11,121,27.5), (12,144,30.0), (13,169,32.5), (14,196,35.0), (15,225,37.5),
 (16,256,40.0), (17,289,42.5), (18,324,45.0), (19,361,47.5), (20,400,50.0),
 (21,441,52.5), (22,484,55.0), (23,529,57.5), (24,576,60.0), (25,625,62.5),
 (26,676,65.0), (27,729,67.5), (28,784,70.0), (29,841,72.5), (30,900,75.0);

-- ============================================================
-- 附：常用增删改查测试语句（仅作参考，本脚本不执行）
-- ------------------------------------------------------------
-- 【查询 SELECT】
--   SELECT * FROM student;                                        -- 12 行
--   SELECT name, score FROM student WHERE age > 19;               -- 类型过滤
--   SELECT * FROM student WHERE score IS NULL;                     -- 2 行（id=6,12）
--   SELECT * FROM student WHERE score IS NOT NULL AND age < 20;
--   SELECT * FROM student WHERE class_name = '一班' OR class_name = '二班';
--   SELECT * FROM student WHERE class_name IN ('一班','三班');
--   SELECT * FROM student WHERE name LIKE '王%';                   -- 前缀匹配
--   SELECT * FROM student WHERE name LIKE '%三';                   -- 后缀匹配
--   SELECT * FROM student WHERE remark LIKE '%课%';                -- 子串匹配
--   SELECT * FROM student WHERE remark = '班长;兼学习委员';        -- 含分号字符串
--   SELECT * FROM student WHERE remark = 'Tom''s project partner'; -- 转义单引号
--   SELECT * FROM student WHERE NOT is_monitor;
--   SELECT * FROM student ORDER BY score DESC;                     -- NULL 排最前
--   SELECT * FROM student ORDER BY class_name ASC, score DESC;     -- 多键排序
--   SELECT * FROM student ORDER BY score DESC LIMIT 5;
--   SELECT * FROM seq_num ORDER BY square DESC LIMIT 10;
--   SELECT id, age + 1 FROM student WHERE id = 1;                  -- 表达式投影(引用列)
--   SELECT * FROM employee WHERE salary > 10000 AND dept = '研发';
--   SELECT * FROM crud_lab WHERE qty * price > 50;                 -- 算术谓词
--
-- 【新增 INSERT】
--   INSERT INTO crud_lab(id,label,qty,price,tag) VALUES (9,'ice',7,4.5,'snack');
--   INSERT INTO student(id,name,age) VALUES (13,'新同学',18);       -- 省略可空列
--   INSERT INTO student(id,name,age) VALUES (14,'A',19),(15,'B',20);-- 多行批量
--
-- 【更新 UPDATE】
--   UPDATE crud_lab SET qty = 100 WHERE id = 1;                    -- 单列
--   UPDATE crud_lab SET qty = 0, price = 0.0 WHERE tag = 'veg';    -- 多列赋值
--   UPDATE crud_lab SET tag = 'fruit' WHERE tag IS NULL;           -- 更新 NULL 列
--   UPDATE crud_lab SET price = price * 2 WHERE id >= 5;           -- 基于自身计算
--   UPDATE student SET id = 2 WHERE id = 1;                        -- 预期: 主键冲突
--   UPDATE crud_lab SET label = 'x' WHERE id = 999;                -- 预期: 0 行受影响
--
-- 【删除 DELETE】
--   DELETE FROM crud_lab WHERE id = 8;                             -- 单行
--   DELETE FROM crud_lab WHERE tag = 'fruit';                      -- 多行
--   DELETE FROM crud_lab WHERE price < 2.0 AND qty > 10;           -- 复合条件
--   DELETE FROM crud_lab;                                          -- 条件省略：清空全表
--
-- 【元数据与计划】
--   SHOW TABLES;
--   DESC student;
--   EXPLAIN SELECT name FROM student WHERE 1 = 1 AND score > 10 + 8;
--   EXPLAIN SELECT * FROM employee WHERE dept = '研发' ORDER BY salary DESC LIMIT 3;
--
-- 【预期异常用例】
--   SELECT nope FROM student;                                      -- 语义错误：列不存在
--   SELECT * FROM nosuchtable;                                     -- 语义错误：表不存在
--   INSERT INTO student(id,name) VALUES (1,'重名');                -- 主键冲突
--   INSERT INTO student(id,age) VALUES (20,18);                    -- name 违反 NOT NULL
--   INSERT INTO student(id,name,age) VALUES (21,'x');              -- 语义错误：列数不匹配
--   CREATE TABLE student(id INT);                                  -- 表已存在
--   DROP TABLE nosuchtable;                                        -- 表不存在
--   UPDATE student SET nope = 1 WHERE id = 1;                      -- 语义错误：列不存在
-- ============================================================
