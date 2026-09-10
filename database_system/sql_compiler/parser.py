# -*- coding: utf-8 -*-
"""语法分析器：递归下降分析，构建抽象语法树（AST）。"""

from .errors import ParseError  # noqa: F401（保持 from .parser import ParseError 可用）
from .lexer import tokenize
from ..utils.constants import (
    TT_EOF,
    TT_FLOAT,
    TT_IDENTIFIER,
    TT_INT,
    TT_KEYWORD,
    TT_OPERATOR,
    TT_PUNCT,
    TT_STRING,
)


# ============================ AST 节点：表达式 ============================
class Expr:
    pass


class Literal(Expr):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return "Literal(%r)" % (self.value,)


class ColumnRef(Expr):
    def __init__(self, name):
        self.name = str(name).lower()

    def __repr__(self):
        return "Col(%s)" % self.name


class BinaryOp(Expr):
    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right

    def __repr__(self):
        return "(%r %s %r)" % (self.left, self.op, self.right)


class UnaryOp(Expr):
    def __init__(self, op, operand):
        self.op = op
        self.operand = operand

    def __repr__(self):
        return "(%s %r)" % (self.op, self.operand)


class InExpr(Expr):
    def __init__(self, operand, values, negated=False):
        self.operand = operand
        self.values = values
        self.negated = negated

    def __repr__(self):
        return "(%r %sIN %r)" % (self.operand, "NOT " if self.negated else "", self.values)


class LikeExpr(Expr):
    def __init__(self, operand, pattern, negated=False):
        self.operand = operand
        self.pattern = pattern
        self.negated = negated

    def __repr__(self):
        return "(%r %sLIKE %r)" % (self.operand, "NOT " if self.negated else "", self.pattern)


class IsNullExpr(Expr):
    def __init__(self, operand, negated=False):
        self.operand = operand
        self.negated = negated

    def __repr__(self):
        return "(%r IS %sNULL)" % (self.operand, "NOT " if self.negated else "")


# ============================ AST 节点：语句 ============================
class Statement:
    pass


class ColumnDef:
    def __init__(self, name, type_name, length=0, primary_key=False, not_null=False):
        self.name = str(name).lower()
        self.type = str(type_name).upper()
        self.length = int(length or 0)
        self.primary_key = primary_key
        self.not_null = not_null or primary_key

    def __repr__(self):
        return "ColumnDef(%s %s%s)" % (self.name, self.type,
                                       " PK" if self.primary_key else "")


class CreateTableStmt(Statement):
    def __init__(self, table_name, columns, if_not_exists=False):
        self.table_name = table_name
        self.columns = columns
        self.if_not_exists = if_not_exists

    def __repr__(self):
        return "CreateTable(%s, %r)" % (self.table_name, self.columns)


class DropTableStmt(Statement):
    def __init__(self, table_name, if_exists=False):
        self.table_name = table_name
        self.if_exists = if_exists


class ShowTablesStmt(Statement):
    pass


class DescribeStmt(Statement):
    def __init__(self, table_name):
        self.table_name = table_name


class InsertStmt(Statement):
    def __init__(self, table_name, columns, rows, column_positions=None):
        self.table_name = table_name
        self.columns = columns            # None 表示按表定义顺序全列插入
        self.rows = rows                  # List[List[Expr]]
        # 每个列名对应的源码位置，与 columns 一一对应，用于精确报错
        self.column_positions = column_positions or []

    def __repr__(self):
        return "Insert(%s, cols=%r, %d rows)" % (self.table_name, self.columns, len(self.rows))


class SelectItem:
    def __init__(self, expr=None, star=False, alias=None):
        self.expr = expr
        self.star = star
        self.alias = alias

    def __repr__(self):
        return "*" if self.star else ("%r AS %s" % (self.expr, self.alias) if self.alias else repr(self.expr))


class OrderByItem:
    def __init__(self, expr, descending=False):
        self.expr = expr
        self.descending = descending


class SelectStmt(Statement):
    def __init__(self, items, table_name, where=None, order_by=None, limit=None):
        self.items = items
        self.table_name = table_name
        self.where = where
        self.order_by = order_by or []
        self.limit = limit

    def __repr__(self):
        return "Select(%r, from=%s, where=%r)" % (self.items, self.table_name, self.where)


class UpdateStmt(Statement):
    def __init__(self, table_name, assignments, where=None):
        self.table_name = table_name
        self.assignments = assignments    # List[(column_name, Expr)]
        self.where = where


class DeleteStmt(Statement):
    def __init__(self, table_name, where=None):
        self.table_name = table_name
        self.where = where


class ExplainStmt(Statement):
    def __init__(self, statement):
        self.statement = statement


# ============================ 解析器 ============================
class Parser:
    """递归下降语法分析器。"""

    TYPE_KEYWORDS = {
        "INT", "INTEGER", "FLOAT", "DOUBLE", "REAL", "TEXT",
        "VARCHAR", "CHAR", "BOOL", "BOOLEAN",
    }

    def __init__(self, sql):
        self.sql = sql
        self.tokens = tokenize(sql)
        self.pos = 0

    # ---------------- 位置辅助 ----------------
    def _pos(self, token=None):
        """取记号的 (行号, 列号)，默认取当前记号。"""
        token = token if token is not None else self.current
        return (token.line, token.column)

    @staticmethod
    def _mark(node, position):
        """给 AST 节点挂上源码位置，供语义分析阶段报错定位。"""
        if node is not None:
            node.position = position
        return node

    # ---------------- 记号流操作 ----------------
    @property
    def current(self):
        return self.tokens[self.pos]

    def _peek(self, offset=0):
        index = self.pos + offset
        return self.tokens[min(index, len(self.tokens) - 1)]

    def _advance(self):
        token = self.tokens[self.pos]
        if token.type != TT_EOF:
            self.pos += 1
        return token

    def _accept_keyword(self, *words):
        if self.current.is_keyword(*words):
            return self._advance().value
        return None

    def _expect_keyword(self, *words):
        token = self.current
        if not token.is_keyword(*words):
            raise ParseError("期望关键字 %s，实际得到 %r"
                             % (" / ".join(words), token.value),
                             position=self._pos(token))
        return self._advance().value

    def _accept_punct(self, *chars):
        if self.current.is_punct(*chars):
            return self._advance().value
        return None

    def _expect_punct(self, char):
        token = self.current
        if not token.is_punct(char):
            raise ParseError("期望 %r，实际得到 %r" % (char, token.value),
                             position=self._pos(token))
        return self._advance().value

    def _accept_op(self, *ops):
        if self.current.is_op(*ops):
            return self._advance().value
        return None

    def _expect_identifier(self, what="标识符"):
        token = self.current
        if token.type != TT_IDENTIFIER:
            raise ParseError("期望%s，实际得到 %r" % (what, token.value),
                             position=self._pos(token))
        return self._advance().value

    # ---------------- 入口 ----------------
    def parse(self):
        """解析一条语句。"""
        stmt = self._parse_statement()
        self._accept_punct(";")
        if self.current.type != TT_EOF:
            raise ParseError("语句结束后存在多余内容：%r" % (self.current.value,),
                             position=self._pos())
        return stmt

    def parse_all(self):
        """解析以分号分隔的多条语句。"""
        statements = []
        while self.current.type != TT_EOF:
            if self._accept_punct(";"):
                continue
            statements.append(self._parse_statement())
            if self.current.type != TT_EOF and not self._accept_punct(";"):
                raise ParseError("语句之间需要用分号分隔", position=self._pos())
        return statements

    # ---------------- 语句 ----------------
    def _parse_statement(self):
        token = self.current
        if token.type != TT_KEYWORD:
            raise ParseError("语句必须以关键字开头，实际得到 %r" % (token.value,),
                             position=self._pos(token))

        keyword = token.value
        if keyword == "CREATE":
            return self._parse_create_table()
        if keyword == "DROP":
            return self._parse_drop_table()
        if keyword == "SHOW":
            return self._parse_show()
        if keyword in ("DESC", "DESCRIBE"):
            return self._parse_describe()
        if keyword == "INSERT":
            return self._parse_insert()
        if keyword == "SELECT":
            return self._parse_select()
        if keyword == "UPDATE":
            return self._parse_update()
        if keyword == "DELETE":
            return self._parse_delete()
        if keyword == "EXPLAIN":
            self._advance()
            return self._mark(ExplainStmt(self._parse_statement()), self._pos(token))
        raise ParseError("不支持的语句：%s" % keyword, position=self._pos(token))

    def _parse_create_table(self):
        start = self.current
        self._expect_keyword("CREATE")
        self._expect_keyword("TABLE")
        if_not_exists = False
        if self.current.is_keyword("IF"):
            self._advance()
            self._expect_keyword("NOT")
            self._expect_keyword("EXISTS")
            if_not_exists = True
        name = self._expect_identifier("表名")
        self._expect_punct("(")

        columns = []
        while True:
            columns.append(self._parse_column_def())
            if self._accept_punct(","):
                continue
            break
        self._expect_punct(")")
        return self._mark(CreateTableStmt(name, columns, if_not_exists), self._pos(start))

    def _parse_column_def(self):
        token = self.current
        # 支持表级约束：PRIMARY KEY (col)
        if token.is_keyword("PRIMARY"):
            self._advance()
            self._expect_keyword("KEY")
            self._expect_punct("(")
            pk_col = self._expect_identifier("主键列名")
            self._expect_punct(")")
            return self._mark(self._pk_marker(pk_col), self._pos(token))

        name = self._expect_identifier("列名")
        if self.current.type != TT_KEYWORD or self.current.value not in self.TYPE_KEYWORDS:
            raise ParseError("列 %s 缺少有效的类型声明" % name, position=self._pos())
        type_name = self._advance().value

        length = 0
        if self._accept_punct("("):
            value = self.current
            if value.type != TT_INT:
                raise ParseError("类型长度必须为整数", position=self._pos(value))
            self._advance()
            length = value.value
            self._expect_punct(")")

        primary_key = False
        not_null = False
        while self.current.type == TT_KEYWORD:
            if self._accept_keyword("PRIMARY"):
                self._expect_keyword("KEY")
                primary_key = True
            elif self._accept_keyword("NOT"):
                self._expect_keyword("NULL")
                not_null = True
            else:
                break
        return self._mark(ColumnDef(name, type_name, length, primary_key, not_null),
                          self._pos(token))

    @staticmethod
    def _pk_marker(pk_col):
        """表级 PRIMARY KEY 约束：返回一个特殊标记列定义，稍后合并到对应列。"""
        col = ColumnDef(pk_col, "INT", primary_key=True)
        col.is_pk_constraint = True
        return col

    def _parse_drop_table(self):
        start = self.current
        self._expect_keyword("DROP")
        self._expect_keyword("TABLE")
        if_exists = False
        if self.current.is_keyword("IF"):
            self._advance()
            self._expect_keyword("EXISTS")
            if_exists = True
        name = self._expect_identifier("表名")
        return self._mark(DropTableStmt(name, if_exists), self._pos(start))

    def _parse_show(self):
        start = self.current
        self._expect_keyword("SHOW")
        self._expect_keyword("TABLES")
        return self._mark(ShowTablesStmt(), self._pos(start))

    def _parse_describe(self):
        start = self.current
        self._advance()
        name = self._expect_identifier("表名")
        return self._mark(DescribeStmt(name), self._pos(start))

    def _parse_insert(self):
        start = self.current
        self._expect_keyword("INSERT")
        self._expect_keyword("INTO")
        table = self._expect_identifier("表名")

        columns = None
        column_positions = None
        if self._accept_punct("("):
            columns = []
            column_positions = []
            while True:
                token = self.current
                columns.append(self._expect_identifier("列名"))
                column_positions.append(self._pos(token))
                if not self._accept_punct(","):
                    break
            self._expect_punct(")")

        self._expect_keyword("VALUES")
        rows = [self._parse_value_tuple()]
        while self._accept_punct(","):
            rows.append(self._parse_value_tuple())
        return self._mark(InsertStmt(table, columns, rows, column_positions),
                          self._pos(start))

    def _parse_value_tuple(self):
        self._expect_punct("(")
        values = [self._parse_expression()]
        while self._accept_punct(","):
            values.append(self._parse_expression())
        self._expect_punct(")")
        return values

    def _parse_select(self):
        start = self.current
        self._expect_keyword("SELECT")
        items = [self._parse_select_item()]
        while self._accept_punct(","):
            items.append(self._parse_select_item())

        self._expect_keyword("FROM")
        table = self._expect_identifier("表名")

        where = None
        if self._accept_keyword("WHERE"):
            where = self._parse_expression()

        order_by = []
        if self._accept_keyword("ORDER"):
            self._expect_keyword("BY")
            order_by.append(self._parse_order_by_item())
            while self._accept_punct(","):
                order_by.append(self._parse_order_by_item())

        limit = None
        if self._accept_keyword("LIMIT"):
            token = self.current
            if token.type != TT_INT:
                raise ParseError("LIMIT 后必须跟整数", position=self._pos(token))
            self._advance()
            limit = token.value

        return self._mark(SelectStmt(items, table, where, order_by, limit), self._pos(start))

    def _parse_select_item(self):
        if self.current.is_op("*") and self.current.type == TT_OPERATOR:
            self._advance()
            return SelectItem(star=True)
        expr = self._parse_expression()
        alias = None
        if self._accept_keyword("AS"):
            alias = self._expect_identifier("别名")
        return SelectItem(expr=expr, alias=alias)

    def _parse_order_by_item(self):
        expr = self._parse_expression()
        descending = bool(self._accept_keyword("DESC"))
        if not descending:
            self._accept_keyword("ASC")
        return OrderByItem(expr, descending)

    def _parse_update(self):
        start = self.current
        self._expect_keyword("UPDATE")
        table = self._expect_identifier("表名")
        self._expect_keyword("SET")

        assignments = []
        while True:
            column = self._expect_identifier("列名")
            if not self.current.is_op("="):
                raise ParseError("SET 子句中缺少 =", position=self._pos())
            self._advance()
            assignments.append((column, self._parse_expression()))
            if not self._accept_punct(","):
                break

        where = None
        if self._accept_keyword("WHERE"):
            where = self._parse_expression()
        return self._mark(UpdateStmt(table, assignments, where), self._pos(start))

    def _parse_delete(self):
        start = self.current
        self._expect_keyword("DELETE")
        self._expect_keyword("FROM")
        table = self._expect_identifier("表名")
        where = None
        if self._accept_keyword("WHERE"):
            where = self._parse_expression()
        return self._mark(DeleteStmt(table, where), self._pos(start))

    # ---------------- 表达式（优先级由低到高）----------------
    def _parse_expression(self):
        return self._parse_or()

    def _parse_or(self):
        left = self._parse_and()
        while self.current.is_keyword("OR"):
            token = self._advance()
            left = self._mark(BinaryOp("OR", left, self._parse_and()), self._pos(token))
        return left

    def _parse_and(self):
        left = self._parse_not()
        while self.current.is_keyword("AND"):
            token = self._advance()
            left = self._mark(BinaryOp("AND", left, self._parse_not()), self._pos(token))
        return left

    def _parse_not(self):
        if self.current.is_keyword("NOT"):
            token = self._advance()
            return self._mark(UnaryOp("NOT", self._parse_not()), self._pos(token))
        return self._parse_comparison()

    def _parse_comparison(self):
        left = self._parse_additive()
        while True:
            token = self.current
            if token.type == TT_OPERATOR and token.value in ("=", "!=", "<>", "<", "<=", ">", ">="):
                self._advance()
                op = "=" if token.value == "=" else token.value
                left = self._mark(BinaryOp(op, left, self._parse_additive()), self._pos(token))
                continue
            if token.is_keyword("IS"):
                self._advance()
                negated = bool(self._accept_keyword("NOT"))
                self._expect_keyword("NULL")
                left = self._mark(IsNullExpr(left, negated), self._pos(token))
                continue
            if token.is_keyword("IN"):
                self._advance()
                self._expect_punct("(")
                values = [self._parse_expression()]
                while self._accept_punct(","):
                    values.append(self._parse_expression())
                self._expect_punct(")")
                left = self._mark(InExpr(left, values, negated=False), self._pos(token))
                continue
            if token.is_keyword("NOT") and self._peek(1).is_keyword("IN"):
                start = self._advance()
                self._advance()
                self._expect_punct("(")
                values = [self._parse_expression()]
                while self._accept_punct(","):
                    values.append(self._parse_expression())
                self._expect_punct(")")
                left = self._mark(InExpr(left, values, negated=True), self._pos(start))
                continue
            if token.is_keyword("LIKE"):
                self._advance()
                pattern = self._parse_additive()
                left = self._mark(LikeExpr(left, pattern, negated=False), self._pos(token))
                continue
            if token.is_keyword("NOT") and self._peek(1).is_keyword("LIKE"):
                start = self._advance()
                self._advance()
                left = self._mark(LikeExpr(left, self._parse_additive(), negated=True),
                                  self._pos(start))
                continue
            break
        return left

    def _parse_additive(self):
        left = self._parse_multiplicative()
        while self.current.type == TT_OPERATOR and self.current.value in ("+", "-"):
            token = self._advance()
            left = self._mark(BinaryOp(token.value, left, self._parse_multiplicative()),
                              self._pos(token))
        return left

    def _parse_multiplicative(self):
        left = self._parse_unary()
        while self.current.type == TT_OPERATOR and self.current.value in ("*", "/", "%"):
            token = self._advance()
            left = self._mark(BinaryOp(token.value, left, self._parse_unary()),
                              self._pos(token))
        return left

    def _parse_unary(self):
        if self.current.type == TT_OPERATOR and self.current.value in ("+", "-", "%"):
            token = self._advance()
            if token.value == "%":
                raise ParseError("非法的单目运算符 %%", position=self._pos(token))
            return self._mark(UnaryOp(token.value, self._parse_unary()), self._pos(token))
        return self._parse_primary()

    def _parse_primary(self):
        token = self.current

        if token.type == TT_PUNCT and token.value == "(":
            self._advance()
            expr = self._parse_expression()
            self._expect_punct(")")
            return expr

        if token.type in (TT_INT, TT_FLOAT, TT_STRING):
            self._advance()
            return self._mark(Literal(token.value), self._pos(token))

        if token.type == TT_KEYWORD:
            if token.value == "NULL":
                self._advance()
                return self._mark(Literal(None), self._pos(token))
            if token.value == "TRUE":
                self._advance()
                return self._mark(Literal(True), self._pos(token))
            if token.value == "FALSE":
                self._advance()
                return self._mark(Literal(False), self._pos(token))
            if token.value == "NOT":
                self._advance()
                return self._mark(UnaryOp("NOT", self._parse_primary()), self._pos(token))

        if token.type == TT_IDENTIFIER:
            self._advance()
            # 支持 t.col 形式
            if self._accept_punct("."):
                column = self._expect_identifier("列名")
                return self._mark(ColumnRef(column), self._pos(token))
            return self._mark(ColumnRef(token.value), self._pos(token))

        raise ParseError("无法解析的记号 %r" % (token.value,), position=self._pos(token))


def parse_sql(sql):
    """便捷函数：解析单条 SQL。"""
    return Parser(sql).parse()
