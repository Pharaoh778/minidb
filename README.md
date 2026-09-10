# MiniDB —— 小型数据库系统

《大型平台软件设计实习》小组项目。使用 **纯 Python 标准库**实现，贯通编译原理、操作系统与数据库三门课程的核心知识。

- SQL 编译器：词法分析 → 语法分析 → 语义分析 → 执行计划生成
- 存储系统：4KB 页式存储 + LRU/FIFO 缓冲池
- 数据库：执行引擎 + 存储引擎 + 系统目录，支持数据持久化

**零第三方依赖**，仅需 Python 3.8+。

---

## 一、快速开始

### 创建虚拟环境（推荐）

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 运行

```powershell
# 交互式命令行
python -m database_system.cli.main

# 指定数据目录 / 缓冲池大小 / 淘汰策略
python -m database_system.cli.main -d data -p 8 -s LRU

# 执行单条语句
python -m database_system.cli.main -e "SHOW TABLES;"

# 执行 SQL 脚本
python -m database_system.cli.main -f script.sql

# 打开详细日志（可看到缓存页淘汰过程）
python -m database_system.cli.main -v

# 查看全部参数
python -m database_system.cli.main --help
```

### 运行测试

```powershell
python -m unittest discover -s database_system/tests -v
```

测试共 103 项，覆盖：

| 文件 | 覆盖内容 |
|---|---|
| `test_sql.py` | 词法 / 语法 / 语义 / 执行计划的基础用例 |
| `test_storage.py` | 页面结构、文件管理、缓冲池与淘汰策略 |
| `test_db.py` | 端到端：DDL / DML / 持久化 / 错误处理 |
| `test_compiler_edge.py` | 词法与语法错误、语义错误三元组、边界用例（空输入 / 超长标识符 / 多语句 / 大小写）、优化规则、**Fuzz 随机测试** |

### 使用 VS Code 调试

按 `F5`，选择 `MiniDB：交互式启动` 即可断点调试（配置见 `.vscode/launch.json`）。

---

## 二、完整演示流程

```sql
CREATE TABLE student(id INT, name VARCHAR, age INT);

INSERT INTO student(id,name,age) VALUES (1,'Alice',20);
INSERT INTO student(id,name,age) VALUES (2,'Bob',17),(3,'Cindy',22);

SELECT * FROM student;
SELECT id,name FROM student WHERE age > 18;

DELETE FROM student WHERE id = 1;

SELECT * FROM student;
```

验证持久化：退出程序后重新执行 `python -m database_system.cli.main -e "SELECT * FROM student;"`，数据依然存在。

---

## 三、内置命令

不以分号结尾，直接输入即可。

| 命令 | 作用 |
|---|---|
| `.help` | 显示帮助 |
| `.tables` | 列出所有表 |
| `.stats` | 显示缓冲池统计（命中率、磁盘读写、淘汰次数） |
| `.flush` | 强制将脏页刷盘 |
| `.tokens <SQL>` | 打印词法分析得到的 Token 四元式 `[种别码, 词素值, 行号, 列号]` |
| `.ast <SQL>` | 打印语法分析得到的抽象语法树 AST |
| `.explain <SQL>` | 对比展示**优化前 / 优化后**的执行计划 |
| `.exit` / `.quit` | 退出（正常退出会自动刷盘） |

SQL 语句必须以 `;` 结尾；未输入分号时按回车会进入 `...` 续行模式。

调试命令也支持 `-e` 方式调用，便于截图：

```powershell
python -m database_system.cli.main -e ".tokens SELECT id FROM student WHERE score > 60;"
python -m database_system.cli.main -e ".ast SELECT id FROM student WHERE score > 60;"
python -m database_system.cli.main -e ".explain SELECT name FROM student WHERE 1 = 1 AND score > 10 + 8;"
```

### 编译各阶段输出示例

**① Token 四元式**

```
+-------------+---------+------+------+
| 种别码      | 词素值  | 行号 | 列号 |
+-------------+---------+------+------+
| KEYWORD     | SELECT  | 1    | 1    |
| IDENTIFIER  | id      | 1    | 8    |
| KEYWORD     | FROM    | 1    | 11   |
| IDENTIFIER  | student | 1    | 16   |
+-------------+---------+------+------+
```

**② AST**

```
-> Select
  -> Projection
    -> SelectItem
      -> ColumnRef(id)
  -> From(student)
  -> Where
    -> BinaryOp(>)
      -> ColumnRef(score)
      -> Literal(60)
```

**③ 执行计划（优化前 / 优化后）**

```
优化前：                                优化后：
-> Project(name)                        -> Project(name)
  -> Filter(((1 = 1) AND (score > …)))    -> Filter((score > 18))
    -> SeqScan(student)                     -> SeqScan(student)[name, score]
```

**④ 错误信息**：三个阶段统一按 `[错误类型, 位置, 原因说明]` 输出

```
Error: [语义错误, 第 1 行第 8 列, 表 student 不存在列 nope]
```

---

## 四、目录结构

```
DBMS/
├── README.md                   本文件
├── grammar.md                  SQL 子集文法（编译器模块的正式交付物）
├── requirements.txt            依赖说明（零依赖）
├── docs/                       课程资料（指导书、课程 PPT）
├── data/                       运行时数据文件（.dat，自动生成）
└── database_system/
    ├── cli/
    │   └── main.py             命令行交互 / REPL（程序唯一入口）
    ├── sql_compiler/           SQL 编译器
    │   ├── lexer.py            词法分析（Token 含行号/列号）
    │   ├── parser.py           语法分析（递归下降）→ AST
    │   ├── semantic.py         语义分析（存在性/类型/列数检查）
    │   ├── planner.py          执行计划生成
    │   ├── optimizer.py        规则式查询优化（5 条规则）
    │   ├── ast.py              AST 树形打印器
    │   ├── errors.py           统一错误模型 [错误类型, 位置, 原因]
    │   └── catalog.py          模式目录（内存模型）
    ├── engine/
    │   ├── executor.py         执行引擎（火山模型算子）
    │   ├── storage_engine.py   存储引擎（Row ↔ Page 序列化）
    │   └── catalog_manager.py  系统目录持久化（__catalog 特殊表）
    ├── storage/
    │   ├── page.py             页面结构（4KB）
    │   ├── file_manager.py     文件管理（页分配/释放/读写）
    │   └── buffer.py           缓冲池（LRU / FIFO）
    ├── tests/                  单元测试
    └── utils/                  常量与辅助函数
```

---

## 五、支持的 SQL

| 语句 | 支持情况 |
|---|---|
| `CREATE TABLE` | 支持 `IF NOT EXISTS`、`PRIMARY KEY`、`NOT NULL`、`VARCHAR(n)` |
| `DROP TABLE` | 支持 `IF EXISTS` |
| `INSERT INTO ... VALUES` | 支持指定列、多行批量插入 |
| `SELECT` | 支持投影列 / `*`、`AS` 别名、`WHERE`、`ORDER BY`、`LIMIT`、表达式 |
| `UPDATE ... SET` | 支持 |
| `DELETE FROM` | 支持 |
| `SHOW TABLES` / `DESC` / `EXPLAIN` | 支持 |

`WHERE` 支持 `= != <> < <= > >=`、`AND`、`OR`、`NOT`、`IS NULL`、`IN`、`LIKE`，以及 `+ - * / %` 算术运算。

暂不支持：`JOIN`、`GROUP BY`、聚合函数、子查询、索引、事务。

完整的文法定义见 [`grammar.md`](grammar.md)。

---

## 六、查询优化

`sql_compiler/optimizer.py` 实现了 5 条**语义等价**的规则式优化：

| 规则 | 说明 | 示例 |
|---|---|---|
| 常量折叠 | 编译期计算常量表达式 | `score > 10 + 8` → `score > 18` |
| 布尔化简 | 去掉恒真 / 恒假的合取、析取分支 | `1 = 1 AND p` → `p` |
| 列裁剪 | 标注 SeqScan 真正需要的列 | `SeqScan(student)[name, score]` |
| 谓词下推 | 合取分解，使每个 Filter 紧贴 SeqScan | `Filter(a AND b)` → `Filter(b) → Filter(a)` |
| 冗余节点消除 | 去掉恒真 Filter、重复 Project / Sort | `Filter(TRUE)` → 删除 |

优化默认开启，可用 `--no-optimize` 关闭；用 `.explain <SQL>` 查看优化前后对比。

---

## 七、关键实现参数

| 参数 | 值 |
|---|---|
| 页面大小 | 4096 字节 |
| 页头 | 20 字节 |
| 槽目录项 | 4 字节（offset + length） |
| 单条记录上限 | 4072 字节 |
| 默认缓冲池 | 64 页 |
| 替换策略 | LRU / FIFO 可切换 |

---

## 八、常见问题

**Q：提示找不到 python？**
确认 Python 已加入 PATH，或使用完整路径
`D:\python\python.exe -m database_system.cli.main`。
注意必须在项目根目录 `d:\DBMS` 下执行，否则 `database_system` 包找不到。

**Q：中文输出乱码？**
在终端执行 `chcp 65001` 切换到 UTF-8，或设置环境变量 `PYTHONIOENCODING=utf-8`。

**Q：数据文件在哪？**
默认在 `data/` 目录，每张表一个 `.dat` 文件，系统目录为 `__catalog.dat`。删除 `data/*.dat` 即可完全重置。
