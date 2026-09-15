# -*- coding: utf-8 -*-
"""编译阶段（词法 / 语法 / 语义）的统一错误模型。

指导书与课程 PPT 要求：错误信息按三元组输出

    [错误类型, 位置, 原因说明]

其中「位置」以「第 N 行第 M 列」表示，便于定位与调试。
"""


def format_position(position):
    """把 (line, column) 二元组格式化为可读文本。"""
    if not position:
        return "未知位置"
    line, column = position
    return "第 %d 行第 %d 列" % (line, column)


class CompileError(Exception):
    """编译阶段错误基类。

    用法：
        raise CompileError("原因说明", position=(line, column))
    也可以只给原因（位置显示为「未知位置」）。
    """

    default_type = "编译错误"

    def __init__(self, reason, position=None, error_type=None):
        super().__init__(reason)
        self.reason = reason
        self.position = position
        self.error_type = error_type or self.default_type

    def as_tuple(self):
        """返回指导书要求的 [错误类型, 位置, 原因说明] 三元组。"""
        return (self.error_type, format_position(self.position), self.reason)

    def __str__(self):
        return "[%s, %s, %s]" % (self.error_type,
                                 format_position(self.position),
                                 self.reason)


class LexError(CompileError):
    """词法错误：非法字符、未闭合字符串 / 注释等。"""

    default_type = "词法错误"


class ParseError(CompileError):
    """语法错误：结构不符合 SQL 子集文法。"""

    default_type = "语法错误"


class SemanticError(CompileError):
    """语义错误：表/列不存在、类型不匹配、列数不一致等。"""

    default_type = "语义错误"


class PlanError(CompileError):
    """计划生成错误：语句本身合法，但无法转换为执行计划。

    对应指导书「遇到不支持的语法或缺失的语义信息时，应给出错误提示」。
    """

    default_type = "计划生成错误"
