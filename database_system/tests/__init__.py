# -*- coding: utf-8 -*-
"""测试包。

运行全部测试（在项目根目录执行）：

    python -m unittest discover -s database_system/tests -v

测试分层：

    test_sql.py             SQL 编译器基础用例
                            词法 5 例、语法 6 例、语义 6 例、执行计划 4 例

    test_storage.py         存储系统
                            页面 7 例、文件管理 4 例、缓冲池 6 例
                            （含 LRU / FIFO 淘汰、脏页回写、满池报错）

    test_db.py              端到端
                            DDL 5 例、DML 11 例、持久化 2 例、
                            缓冲与多页 2 例、错误处理 4 例

    test_compiler_edge.py   边界测试与 Fuzz 测试
                            - 词法错误：非法字符、字符串未闭合、块注释未闭合
                            - 语法错误：缺分号、括号不匹配、结构错误、LIMIT 非整数
                            - 语义错误：表/列不存在、类型不匹配、列数不一致
                            - 错误三元组 [错误类型, 位置, 原因] 的格式校验
                            - 边界：空输入、超长标识符、多语句、大小写、注释、转义引号
                            - 5 条优化规则逐一验证 + PPT 示例用例
                            - Fuzz：1200 次随机生成 / 变异 SQL，要求不崩溃

当前共 103 项测试，全部通过。

关注指标（课程 PPT 第 32 页）：
    Crash（崩溃）/ Wrong Accept（错误接受）
    / Wrong Reject（错误拒绝）/ Error Location（错误定位）
"""
