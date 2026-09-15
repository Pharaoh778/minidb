# -*- coding: utf-8 -*-
"""SQL 编译器：词法分析、语法分析、语义分析、执行计划生成与优化。"""

from .ast import format_ast
from .catalog import Catalog, Column, TableSchema
from .errors import CompileError, LexError, ParseError, PlanError, SemanticError
from .lexer import LexError, Token, format_tokens, tokenize
from .optimizer import Optimizer
from .parser import Parser, ParseError
from .planner import Planner
from .semantic import SemanticAnalyzer, SemanticError

__all__ = [
    # 模式目录
    "Catalog", "Column", "TableSchema",
    # 词法分析
    "Token", "tokenize", "format_tokens", "LexError",
    # 语法分析
    "Parser", "ParseError", "format_ast",
    # 语义分析
    "SemanticAnalyzer", "SemanticError",
    # 执行计划与优化
    "Planner", "Optimizer", "PlanError",
    # 错误基类
    "CompileError",
]
