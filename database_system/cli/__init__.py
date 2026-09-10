# -*- coding: utf-8 -*-
"""命令行接口（CLI）——**程序唯一入口**所在。

职责：
    1. 解析命令行参数（数据目录 / 缓冲池大小 / 淘汰策略 / 执行方式）
    2. 提供交互式 REPL，读取用户输入并回显执行结果
    3. 用 `MiniDB` 类把「编译器 + 执行引擎 + 存储引擎」串联起来
    4. 统一错误输出：预期错误只提示，未预期错误提示加 `-v` 看堆栈
    5. 提供编译器各阶段的可视化调试命令，便于演示与报告截图

启动方式（必须在项目根目录 d:\\DBMS 下执行）：

    python -m database_system.cli.main
    python -m database_system.cli.main -e "SELECT * FROM student;"
    python -m database_system.cli.main -f script.sql
    python -m database_system.cli.main --help

常用参数：
    -d / --data-dir     数据目录（默认 data）
    -p / --pool-size    缓冲池页数（默认 64）
    -s / --strategy     缓存淘汰策略，LRU 或 FIFO（默认 LRU）
    -f / --file         执行 SQL 脚本后退出
    -e / --execute      执行一条语句后退出
    -v / --verbose      输出调试日志（可看到缓存页淘汰）
    --no-optimize       关闭规则式查询优化（默认开启）

支持的 SQL 语句：
    CREATE TABLE / DROP TABLE / SHOW TABLES / DESC
    INSERT / SELECT（含 WHERE、ORDER BY、LIMIT）/ UPDATE / DELETE / EXPLAIN

内置命令（不以分号结尾）：
    .help              显示帮助
    .tables            列出所有表
    .stats             显示缓冲池统计（命中率、磁盘读写、淘汰次数）
    .flush             把脏页刷回磁盘
    .tokens <SQL>      词法分析：Token 四元式 [种别码, 词素值, 行号, 列号]
    .ast <SQL>         语法分析：抽象语法树 AST
    .explain <SQL>     执行计划：优化前 / 优化后对比
    .exit / .quit      退出（正常退出会自动刷盘）

主要组成（均在 cli/main.py）：
    MiniDB          把编译器与引擎串起来的数据库实例
    report_error()  统一的错误输出
    build_arg_parser() / main()   参数解析与启动分发
"""
