# MiniSQL 文法说明（SQL 子集）

> 本文件是《大型平台软件设计实习》编译器模块的**正式交付物之一**。
> 文法定义与 `database_system/sql_compiler/parser.py` 的实现严格一一对应，
> 每一条产生式都标注了对应的实现函数，便于答辩时对照讲解。

## 0. 记法约定

| 符号 | 含义 |
|---|---|
| `::=` | 定义为 |
| `{ X }` | X 重复 0 次或多次 |
| `[ X ]` | X 可选（出现 0 或 1 次） |
| `A \| B` | A 或 B |
| 大写单词 | 终结符（关键字） |
| `<尖括号>` | 非终结符 |
| 引号中的符号 | 字面终结符，如 `"("` |

---

## 1. 程序结构

```
<program>       ::= { <statement> ";" } 
<statement>     ::= <create_table>
                  | <drop_table>
                  | <show_tables>
                  | <describe>
                  | <insert>
                  | <select>
                  | <update>
                  | <delete>
                  | <explain>
<explain>       ::= "EXPLAIN" <statement>
```

> 实现：`Parser.parse()` 解析单条语句，`Parser.parse_all()` 解析以分号分隔的多条语句。
> 语句之间**必须**用分号分隔；最后一条语句的分号可省略。

---

## 2. 数据定义语句（DDL）

### 2.1 CREATE TABLE

```
<create_table>  ::= "CREATE" "TABLE" [ "IF" "NOT" "EXISTS" ] <identifier>
                    "(" <column_def> { "," <column_def> } ")"

<column_def>    ::= <identifier> <type> [ "(" <integer> ")" ] { <column_constraint> }
                  | "PRIMARY" "KEY" "(" <identifier> ")"          -- 表级主键约束

<column_constraint> ::= "PRIMARY" "KEY" | "NOT" "NULL"

<type>          ::= "INT" | "INTEGER" | "FLOAT" | "DOUBLE" | "REAL"
                  | "TEXT" | "VARCHAR" | "CHAR" | "BOOL" | "BOOLEAN"
```

说明：
- 至少需要定义一列；
- `VARCHAR(n)` 中的 `n` 必须是整数字面量；
- 表级 `PRIMARY KEY(col)` 在语义分析阶段合并到对应列上；
- 当前**不支持复合主键**（语义分析阶段会报错）。

> 实现：`_parse_create_table()` / `_parse_column_def()`

### 2.2 DROP TABLE

```
<drop_table>    ::= "DROP" "TABLE" [ "IF" "EXISTS" ] <identifier>
```

> 实现：`_parse_drop_table()`

### 2.3 SHOW TABLES / DESC

```
<show_tables>   ::= "SHOW" "TABLES"
<describe>      ::= ( "DESC" | "DESCRIBE" ) <identifier>
```

> 实现：`_parse_show()` / `_parse_describe()`

---

## 3. 数据操作语句（DML）

### 3.1 INSERT

```
<insert>        ::= "INSERT" "INTO" <identifier>
                    [ "(" <identifier> { "," <identifier> } ")" ]
                    "VALUES" <value_tuple> { "," <value_tuple> }

<value_tuple>   ::= "(" <expr> { "," <expr> } ")"
```

说明：省略列名时按表定义顺序插入全部列；支持一次插入多行。

> 实现：`_parse_insert()` / `_parse_value_tuple()`

### 3.2 SELECT

```
<select>        ::= "SELECT" <select_item> { "," <select_item> }
                    "FROM" <identifier>
                    [ "WHERE" <expr> ]
                    [ "ORDER" "BY" <order_item> { "," <order_item> } ]
                    [ "LIMIT" <integer> ]

<select_item>   ::= "*" | <expr> [ "AS" <identifier> ]
<order_item>    ::= <expr> [ "ASC" | "DESC" ]
```

> 实现：`_parse_select()` / `_parse_select_item()` / `_parse_order_by_item()`

### 3.3 UPDATE

```
<update>        ::= "UPDATE" <identifier>
                    "SET" <assignment> { "," <assignment> }
                    [ "WHERE" <expr> ]

<assignment>    ::= <identifier> "=" <expr>
```

> 实现：`_parse_update()`

### 3.4 DELETE

```
<delete>        ::= "DELETE" "FROM" <identifier> [ "WHERE" <expr> ]
```

> 实现：`_parse_delete()`

---

## 4. 表达式文法

采用**递归下降**分析，按优先级由低到高分层。
下式中的左递归（`E ::= E "+" E`）在实现中被改写为**循环迭代**，
这正是课程要求的「消除左递归」变换。

```
<expr>              ::= <or_expr>

<or_expr>           ::= <and_expr> { "OR" <and_expr> }

<and_expr>          ::= <not_expr> { "AND" <not_expr> }

<not_expr>          ::= "NOT" <not_expr>
                      | <comparison>

<comparison>        ::= <additive> { <comparison_tail> }
<comparison_tail>   ::= <comp_op> <additive>
                      | "IS" [ "NOT" ] "NULL"
                      | [ "NOT" ] "IN" "(" <expr> { "," <expr> } ")"
                      | [ "NOT" ] "LIKE" <additive>

<comp_op>           ::= "=" | "!=" | "<>" | "<" | "<=" | ">" | ">="

<additive>          ::= <multiplicative> { ( "+" | "-" ) <multiplicative> }

<multiplicative>    ::= <unary> { ( "*" | "/" | "%" ) <unary> }

<unary>             ::= ( "+" | "-" ) <unary>
                      | <primary>

<primary>           ::= "(" <expr> ")"
                      | <literal>
                      | "NULL" | "TRUE" | "FALSE"
                      | <identifier> [ "." <identifier> ]
```

**优先级（由低到高）**

```
OR  <  AND  <  NOT  <  比较(= != <> < <= > >=, IS, IN, LIKE)
   <  + -   <  * / %   <  一元 + -   <  括号 / 字面量 / 列引用
```

示例：`WHERE a = 1 OR b = 2 AND c = 3` 解析为

```
        OR
       /  \
   a = 1   AND
          /   \
      b = 2   c = 3
```

（AND 优先级高于 OR，与课程 PPT 第 21 页一致）

> 实现：`_parse_expression()` → `_parse_or()` → `_parse_and()` → `_parse_not()`
> → `_parse_comparison()` → `_parse_additive()` → `_parse_multiplicative()`
> → `_parse_unary()` → `_parse_primary()`

---

## 5. 词法规则

```
<keyword>       ::= "CREATE" | "TABLE" | "DROP" | "INSERT" | "INTO" | "VALUES"
                  | "SELECT" | "FROM" | "WHERE" | "UPDATE" | "SET" | "DELETE"
                  | "SHOW" | "TABLES" | "DESC" | "DESCRIBE" | "EXPLAIN"
                  | "PRIMARY" | "KEY" | "NOT" | "NULL" | "AND" | "OR" | "IS"
                  | "IN" | "LIKE" | "ORDER" | "BY" | "ASC" | "LIMIT" | "IF"
                  | "EXISTS" | "TRUE" | "FALSE"
                  | <type 关键字>

<identifier>    ::= ( 字母 | "_" ) { 字母 | 数字 | "_" | "$" }

<integer>       ::= 数字 { 数字 }

<float>         ::= 数字 { 数字 } "." 数字 { 数字 } [ ("e"|"E") [ "+"|"-" ] 数字 { 数字 } ]
                  | 数字 { 数字 } ("e"|"E") [ "+"|"-" ] 数字 { 数字 }

<string>        ::= "'" { 任意字符 | "''" } "'"
                  | '"' { 任意字符 | '""' } '"'

<operator>      ::= "<>" | "!=" | "<=" | ">=" | "=" | "<" | ">"
                  | "+" | "-" | "*" | "/" | "%"

<delimiter>     ::= "(" | ")" | "," | ";" | "."

<comment>       ::= "--" { 任意字符 } 换行符
                  | "/*" { 任意字符 } "*/"
```

**词法约定**

1. **关键字大小写不敏感**：`select` / `SELECT` / `SeLeCt` 等价，词法阶段统一转为大写；
2. **标识符转为小写**：`Student` 与 `student` 视为同一张表；
3. **贪心匹配**：多字符运算符（如 `<=`、`!=`）优先于单字符匹配，避免把 `<=` 切成 `<` 和 `=`；
4. **字符串转义**：`''` 表示一个单引号字符（`'Tom''s book'`）；
5. **正负号歧义**：`+` / `-` 只有在前一个记号是运算符、分隔符或 `VALUES / SET / WHERE / IN / BY / LIMIT` 等关键字时，才作为数字的正负号；
6. **非法输入**：非法字符、未闭合字符串、未闭合块注释均抛出 `LexError`，输出 `[错误类型, 位置, 原因说明]`。

**Token 结构（四元式）**

```
Token ::= ( 种别码, 词素值, 行号, 列号 )
```

种别码取值：`KEYWORD` / `IDENTIFIER` / `INT_LITERAL` / `FLOAT_LITERAL` /
`STRING_LITERAL` / `OPERATOR` / `PUNCTUATION` / `EOF`。

行号、列号均从 1 开始计数，用于语法与语义阶段的错误定位。

> 实现：`lexer.py` 的 `tokenize()`、`Token.as_tuple()`、`format_tokens()`

---

## 6. 语义规则

| 规则 | 检查内容 | 实现位置 |
|---|---|---|
| 表存在性 | 表是否已创建 / 是否重复创建 | `_require_table()` / `_check_create()` |
| 列存在性 | SELECT / WHERE / SET 引用的列是否存在 | `_check_expression()` |
| 名字绑定 | 标识符 → Catalog 中的列定义 | `Catalog.get_column()` |
| 类型一致性 | 字面量能否转换为列类型；比较/算术运算的类型是否可兼容 | `coerce_value()` / `_comparable()` |
| 列数 / 列序 | INSERT 的值个数与列数是否一致 | `_check_insert()` |
| 非空约束 | NOT NULL / PRIMARY KEY 列是否被赋值 | `_check_insert()` |
| 其他 | LIMIT 非负、UPDATE 至少一项赋值、列名不重复 | `_check_select()` / `_check_update()` |

语义错误统一输出：

```
[语义错误, 第 N 行第 M 列, 原因说明]
```

---

## 7. 执行计划算子

语义分析通过后，由 `Planner` 把 AST 转换为逻辑算子树：

| 算子 | 含义 |
|---|---|
| `CreateTable` / `DropTable` | 建表 / 删表并注册元数据 |
| `Insert` | 向目标表写入记录 |
| `SeqScan` | 顺序扫描表的所有数据页 |
| `Filter` | 执行 WHERE 条件 |
| `Project` | 按 SELECT 指定列投影 |
| `Sort` / `Limit` | 排序 / 限制返回行数 |
| `Update` / `Delete` | 更新 / 删除记录 |
| `ShowTables` / `Describe` / `Explain` | 元数据查询与计划展示 |

SELECT 的执行顺序为：`SeqScan → Filter → Sort → Project → Limit`。

---

## 8. 优化规则

`optimizer.py` 实现 5 条**语义等价**的规则式优化：

| 规则 | 说明 | 示例 |
|---|---|---|
| 常量折叠 | 编译期计算常量表达式 | `age > 10 + 8` → `age > 18` |
| 布尔化简 | 去掉恒真 / 恒假的合取、析取分支 | `1 = 1 AND p` → `p` |
| 列裁剪 | 标注 SeqScan 真正需要的列 | `SeqScan(student)[age, name]` |
| 谓词下推 | 合取分解，使每个 Filter 紧贴 SeqScan | `Filter(a AND b)` → `Filter(b) → Filter(a)` |
| 冗余节点消除 | 去掉恒真 Filter、重复 Project / Sort | `Filter(TRUE)` → 删除 |

演示（课程 PPT 第 29 页示例）：

```sql
SELECT name FROM student WHERE 1 = 1 AND age > 10 + 8;
```

```
优化前：                          优化后：
-> Project(name)                  -> Project(name)
  -> Filter((1=1) AND (age>(10+8)))   -> Filter((age > 18))
    -> SeqScan(student)                 -> SeqScan(student)[age, name]
```

---

## 9. 不支持的语法

- `JOIN`（多表连接）
- `GROUP BY` / `HAVING` / 聚合函数（`COUNT` / `SUM` / `AVG` 等）
- `DISTINCT`、子查询、`UNION`
- 索引（`CREATE INDEX`）、视图、事务（`BEGIN` / `COMMIT` / `ROLLBACK`）
- 多列（复合）主键
