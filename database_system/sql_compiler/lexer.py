# -*- coding: utf-8 -*-
"""词法分析器：把 SQL 文本切分为记号（Token）序列。"""

from ..utils.constants import (
    KEYWORDS,
    TT_EOF,
    TT_FLOAT,
    TT_IDENTIFIER,
    TT_INT,
    TT_KEYWORD,
    TT_OPERATOR,
    TT_PUNCT,
    TT_STRING,
)
from ..utils.helpers import format_table
from .errors import LexError  # noqa: F401（保持 from .lexer import LexError 可用）

# 长度优先匹配，避免 '<=' 被切成 '<' 和 '='
OPERATORS = ("<>", "!=", "<=", ">=", "=", "<", ">", "+", "-", "*", "/", "%")
PUNCTUATIONS = ("(", ")", ",", ";", ".")


def offset_to_position(text, offset):
    """把字符偏移量换算为 (行号, 列号)，均从 1 开始计数。"""
    line = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset) + 1
    return line, offset - line_start + 1


class Token:
    """词法记号。

    除种别码与词素值外，还记录源码位置（行号、列号），
    供语法 / 语义阶段的错误定位使用。
    """

    __slots__ = ("type", "value", "pos", "line", "column")

    def __init__(self, type_, value, pos=0, line=0, column=0):
        self.type = type_
        self.value = value
        self.pos = pos
        self.line = line
        self.column = column

    @property
    def lexeme(self):
        """词素值：源码中的原始字符串形式。"""
        if self.type == TT_EOF:
            return "<EOF>"
        if self.type == TT_STRING:
            return "'%s'" % self.value
        if self.value is None:
            return "NULL"
        return str(self.value)

    def as_tuple(self):
        """四元式：(种别码, 词素值, 行号, 列号)。"""
        return (self.type, self.lexeme, self.line, self.column)

    def is_keyword(self, *words):
        return self.type == TT_KEYWORD and self.value in words

    def is_punct(self, *chars):
        return self.type == TT_PUNCT and self.value in chars

    def is_op(self, *ops):
        return self.type == TT_OPERATOR and self.value in ops

    def __repr__(self):
        return "Token(%s, %r)" % (self.type, self.value)

    def __eq__(self, other):
        return (isinstance(other, Token)
                and self.type == other.type
                and self.value == other.value)


def tokenize(sql):
    """把 SQL 文本转换为 Token 列表（末尾附加 EOF）。"""
    tokens = []
    i, n = 0, len(sql)

    while i < n:
        ch = sql[i]

        # 空白
        if ch.isspace():
            i += 1
            continue

        # 注释：-- 行注释；/* */ 块注释
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise LexError("块注释未闭合", position=offset_to_position(sql, i))
            i = end + 2
            continue

        # 数字
        if ch.isdigit() or (ch in "+-" and i + 1 < n and sql[i + 1].isdigit()
                            and _expect_number_context(tokens)):
            start = i
            if ch in "+-":
                i += 1
            is_float = False
            while i < n and sql[i].isdigit():
                i += 1
            if i < n and sql[i] == "." and i + 1 < n and sql[i + 1].isdigit():
                is_float = True
                i += 1
                while i < n and sql[i].isdigit():
                    i += 1
            if i < n and sql[i] in "eE":
                j = i + 1
                if j < n and sql[j] in "+-":
                    j += 1
                if j < n and sql[j].isdigit():
                    is_float = True
                    i = j
                    while i < n and sql[i].isdigit():
                        i += 1
            text = sql[start:i]
            tokens.append(Token(TT_FLOAT if is_float else TT_INT,
                                float(text) if is_float else int(text), start))
            continue

        # 字符串字面量
        if ch in "'\"":
            quote = ch
            i += 1
            start = i
            buf = []
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:   # 转义 ''
                        buf.append(quote)
                        i += 2
                        continue
                    break
                buf.append(sql[i])
                i += 1
            if i >= n:
                raise LexError("字符串未闭合", position=offset_to_position(sql, start))
            i += 1
            tokens.append(Token(TT_STRING, "".join(buf), start))
            continue

        # 标识符 / 关键字
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            word = sql[start:i]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token(TT_KEYWORD, upper, start))
            else:
                tokens.append(Token(TT_IDENTIFIER, word.lower(), start))
            continue

        # 运算符
        matched = None
        for op in OPERATORS:
            if sql.startswith(op, i):
                matched = op
                break
        if matched:
            tokens.append(Token(TT_OPERATOR, matched, i))
            i += len(matched)
            continue

        # 分隔符
        if ch in PUNCTUATIONS:
            tokens.append(Token(TT_PUNCT, ch, i))
            i += 1
            continue

        raise LexError("无法识别的字符 %r" % ch, position=offset_to_position(sql, i))

    tokens.append(Token(TT_EOF, None, n))
    _assign_positions(sql, tokens)
    return tokens


def _assign_positions(sql, tokens):
    """为记号序列填充行号与列号（记号按位置递增，单趟扫描即可）。"""
    line, line_start, scanned = 1, 0, 0
    for token in tokens:
        while scanned < token.pos:
            if sql[scanned] == "\n":
                line += 1
                line_start = scanned + 1
            scanned += 1
        token.line = line
        token.column = token.pos - line_start + 1


def format_tokens(tokens):
    """把记号流格式化为 [种别码, 词素值, 行号, 列号] 四元式表格。"""
    rows = [list(token.as_tuple()) for token in tokens if token.type != TT_EOF]
    if not rows:
        return "(无记号)"
    return format_table(["种别码", "词素值", "行号", "列号"], rows)


def _expect_number_context(tokens):
    """判断 '+'/'-' 是否应作为数字的正负号（前一个记号是运算符、逗号或左括号等）。"""
    if not tokens:
        return True
    prev = tokens[-1]
    if prev.type in (TT_OPERATOR, TT_PUNCT):
        return True
    return prev.is_keyword("VALUES", "SET", "WHERE", "IN", "BY", "LIMIT")
