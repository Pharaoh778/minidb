# -*- coding: utf-8 -*-
"""通用工具包。

本包不依赖任何业务模块，可被任意层调用（因此必须避免反向依赖）。

constants.py —— 全局常量，分五类：

    存储相关      PAGE_SIZE(4096)、页头 20B 的各字段偏移、
                  文件头（第 0 页）各字段偏移、文件魔数 b"MDBF"
    数据类型      INT / FLOAT / TEXT / BOOL，以及 SQL 类型别名映射
                  （INTEGER→INT、VARCHAR→TEXT、DOUBLE→FLOAT ……）
    系统目录      __catalog 表名及其两个列名（table_name、definition）
    默认配置      数据目录 data、缓冲池 64 页、默认策略 LRU
    词法记号      39 个 SQL 关键字、8 种 Token 类型
                  （KEYWORD / IDENTIFIER / INT_LITERAL / FLOAT_LITERAL /
                   STRING_LITERAL / OPERATOR / PUNCTUATION / EOF）

helpers.py —— 通用辅助函数：

    ensure_dir()     确保目录存在（不存在则创建）
    get_logger()     获取全局唯一 logger（含时间戳与级别）
    coerce_value()   字面量 → 列类型的强制转换，语义分析的
                     类型一致性检查依赖它；失败抛 ValueError
    format_value()   单个值的显示形式（NULL / true / 浮点 %g）
    format_table()   结果集 → ASCII 表格，自动按中文宽度对齐

维护约定：
    修改本包时需注意——constants.py 被编译器与存储层共同使用，
    前 22 行的存储常量属于存储模块，其余属于编译器模块，
    改动前请与对应模块的负责人确认。
"""
