# -*- coding: utf-8 -*-
"""MiniDB —— 简化版数据库系统。

模块划分：
    sql_compiler  SQL 编译器（词法 / 语法 / 语义 / 执行计划 / 规则式优化）
    storage       存储系统（页式存储 / 文件管理 / 缓冲池）
    engine        数据库引擎（执行引擎 / 存储引擎 / 系统目录）
    cli           命令行接口（程序唯一入口）
    tests         单元测试
    utils         通用工具（常量 / 辅助函数）

一条 SQL 的完整链路：

    SQL 文本
      -> lexer.tokenize()         Token 流 [种别码, 词素值, 行号, 列号]
      -> parser.Parser()          AST 抽象语法树
      -> semantic.SemanticAnalyzer()   语义检查 + 维护 Catalog
      -> planner.Planner()        逻辑执行计划（算子树）
      -> optimizer.Optimizer()    规则式优化（5 条，语义等价改写）
      -> engine.Executor()        火山模型算子执行
      -> storage.BufferPool()     页缓存
      -> storage.FileManager()    页分配 / 读写 -> data/*.dat

启动：python -m database_system.cli.main
文法：见项目根目录 grammar.md
"""

__version__ = "1.1.0"
