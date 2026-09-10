# -*- coding: utf-8 -*-
"""语义分析器：存在性检查、类型检查、列数检查。"""

from .parser import (
    BinaryOp,
    ColumnRef,
    CreateTableStmt,
    DeleteStmt,
    DescribeStmt,
    DropTableStmt,
    ExplainStmt,
    InExpr,
    InsertStmt,
    IsNullExpr,
    LikeExpr,
    Literal,
    SelectStmt,
    ShowTablesStmt,
    UnaryOp,
    UpdateStmt,
)
from ..utils.constants import TYPE_BOOL, TYPE_FLOAT, TYPE_INT, TYPE_TEXT
from ..utils.helpers import coerce_value
from .errors import SemanticError  # noqa: F401（保持 from .semantic import SemanticError 可用）

NUMERIC_TYPES = (TYPE_INT, TYPE_FLOAT)


def position_of(node):
    """取 AST 节点上挂载的源码位置；节点未标注时返回 None。"""
    return getattr(node, "position", None)


class SemanticAnalyzer:
    """基于模式目录对 AST 做语义校验。"""

    def __init__(self, catalog):
        self.catalog = catalog

    # ---------------- 入口 ----------------
    def analyze(self, statement):
        return self._dispatch(statement)

    def analyze_all(self, statements):
        return [self._dispatch(stmt) for stmt in statements]

    def _dispatch(self, stmt):
        if isinstance(stmt, CreateTableStmt):
            return self._check_create(stmt)
        if isinstance(stmt, DropTableStmt):
            return self._check_drop(stmt)
        if isinstance(stmt, ShowTablesStmt):
            return stmt
        if isinstance(stmt, DescribeStmt):
            return self._check_describe(stmt)
        if isinstance(stmt, InsertStmt):
            return self._check_insert(stmt)
        if isinstance(stmt, SelectStmt):
            return self._check_select(stmt)
        if isinstance(stmt, UpdateStmt):
            return self._check_update(stmt)
        if isinstance(stmt, DeleteStmt):
            return self._check_delete(stmt)
        if isinstance(stmt, ExplainStmt):
            self._dispatch(stmt.statement)
            return stmt
        raise SemanticError("无法识别的语句类型：%r" % stmt, position=position_of(stmt))

    # ---------------- DDL ----------------
    def _check_create(self, stmt):
        if self.catalog.has_table(stmt.table_name):
            if stmt.if_not_exists:
                stmt.skipped = True
                return stmt
            raise SemanticError("表 %s 已存在" % stmt.table_name, position=position_of(stmt))
        stmt.skipped = False

        # 处理表级 PRIMARY KEY (col) 约束
        pk_columns = [c.name for c in stmt.columns if getattr(c, "is_pk_constraint", False)]
        stmt.columns = [c for c in stmt.columns if not getattr(c, "is_pk_constraint", False)]

        if not stmt.columns:
            raise SemanticError("表 %s 至少需要定义一列" % stmt.table_name,
                                position=position_of(stmt))

        seen = set()
        pk_count = 0
        for col in stmt.columns:
            if col.name in seen:
                raise SemanticError("列名重复：%s" % col.name, position=position_of(col))
            seen.add(col.name)
            if col.name in pk_columns:
                col.primary_key = True
                col.not_null = True
            if col.primary_key:
                pk_count += 1

        for name in pk_columns:
            if name not in seen:
                raise SemanticError("PRIMARY KEY 指向了不存在的列：%s" % name,
                                    position=position_of(stmt))
        if pk_count > 1:
            raise SemanticError("暂不支持复合主键（当前定义了 %d 个主键列）" % pk_count,
                                position=position_of(stmt))
        return stmt

    def _check_drop(self, stmt):
        if not self.catalog.has_table(stmt.table_name):
            if stmt.if_exists:
                stmt.skipped = True
                return stmt
            raise SemanticError("表 %s 不存在" % stmt.table_name, position=position_of(stmt))
        stmt.skipped = False
        return stmt

    def _check_describe(self, stmt):
        self._require_table(stmt.table_name, position_of(stmt))
        return stmt

    # ---------------- DML ----------------
    def _require_table(self, name, position=None):
        schema = self.catalog.get_table(name)
        if schema is None:
            raise SemanticError("表 %s 不存在" % name, position=position)
        return schema

    @staticmethod
    def _column_position(stmt, index):
        """取 INSERT 第 index 个列名的源码位置；取不到时退回语句位置。"""
        positions = getattr(stmt, "column_positions", None) or []
        if index < len(positions):
            return positions[index]
        return position_of(stmt)

    def _check_insert(self, stmt):
        schema = self._require_table(stmt.table_name, position_of(stmt))

        if stmt.columns:
            seen = set()
            for index, col_name in enumerate(stmt.columns):
                column_position = self._column_position(stmt, index)
                if col_name in seen:
                    raise SemanticError("插入列名重复：%s" % col_name,
                                        position=column_position)
                seen.add(col_name)
                if not schema.has_column(col_name):
                    raise SemanticError("表 %s 不存在列 %s" % (schema.name, col_name),
                                        position=column_position)

        expected = len(stmt.columns) if stmt.columns else len(schema.columns)
        for row_index, row in enumerate(stmt.rows, 1):
            if len(row) != expected:
                # 定位到出错的那组 VALUES（取该组第一个值的位置），而不是语句开头
                row_position = position_of(row[0]) if row else position_of(stmt)
                raise SemanticError(
                    "VALUES 第 %d 组值列数不匹配：期望 %d 列，实际 %d 列"
                    % (row_index, expected, len(row)), position=row_position)

        # 类型检查 + 常量折叠（把字面量转换为列类型）
        target_columns = ([schema.get_column(c) for c in stmt.columns]
                          if stmt.columns else list(schema.columns))
        for row in stmt.rows:
            for expr, column in zip(row, target_columns):
                self._check_value_type(expr, column)

        # 未显式赋值且 NOT NULL 的列
        if stmt.columns:
            missing = [c for c in schema.columns
                       if c.name not in seen and c.not_null and c.primary_key is False]
            for column in missing:
                if column.not_null:
                    raise SemanticError("列 %s 不允许为 NULL，必须显式赋值" % column.name,
                                        position=position_of(stmt))
        return stmt

    def _check_value_type(self, expr, column):
        if isinstance(expr, Literal):
            try:
                expr.value = coerce_value(column.type, expr.value, column.name)
            except ValueError as exc:
                raise SemanticError(str(exc), position=position_of(expr))
            if expr.value is None and column.not_null:
                raise SemanticError("列 %s 不允许为 NULL" % column.name,
                                    position=position_of(expr))
        elif isinstance(expr, ColumnRef):
            raise SemanticError("INSERT 的 VALUES 中暂不支持列引用：%s" % expr.name,
                                position=position_of(expr))
        else:
            raise SemanticError("INSERT 的 VALUES 只支持常量", position=position_of(expr))

    def _check_select(self, stmt):
        schema = self._require_table(stmt.table_name, position_of(stmt))
        for item in stmt.items:
            if item.star:
                continue
            self._check_expression(item.expr, schema)
        if stmt.where is not None:
            self._check_expression(stmt.where, schema, boolean_context=True)
        for order_item in stmt.order_by:
            self._check_expression(order_item.expr, schema)
        if stmt.limit is not None and stmt.limit < 0:
            raise SemanticError("LIMIT 不能为负数", position=position_of(stmt))
        return stmt

    def _check_update(self, stmt):
        schema = self._require_table(stmt.table_name, position_of(stmt))
        if not stmt.assignments:
            raise SemanticError("UPDATE 语句至少需要一项赋值", position=position_of(stmt))
        seen = set()
        for column_name, expr in stmt.assignments:
            if column_name in seen:
                raise SemanticError("重复赋值的列：%s" % column_name,
                                    position=position_of(stmt))
            seen.add(column_name)
            column = schema.get_column(column_name)
            if column is None:
                raise SemanticError("表 %s 不存在列 %s" % (schema.name, column_name),
                                    position=position_of(stmt))
            self._check_expression(expr, schema)
            value_type = self.infer_type(expr, schema)
            if value_type is not None and not self._compatible(column.type, value_type):
                raise SemanticError("列 %s 的类型 %s 与被赋值的 %s 不兼容"
                                    % (column.name, column.type, value_type),
                                    position=position_of(expr))
        if stmt.where is not None:
            self._check_expression(stmt.where, schema, boolean_context=True)
        return stmt

    def _check_delete(self, stmt):
        schema = self._require_table(stmt.table_name, position_of(stmt))
        if stmt.where is not None:
            self._check_expression(stmt.where, schema, boolean_context=True)
        return stmt

    # ---------------- 表达式检查 ----------------
    def _check_expression(self, expr, schema, boolean_context=False):
        if isinstance(expr, Literal):
            return
        if isinstance(expr, ColumnRef):
            if not schema.has_column(expr.name):
                raise SemanticError("表 %s 不存在列 %s" % (schema.name, expr.name),
                                    position=position_of(expr))
            return
        if isinstance(expr, BinaryOp):
            self._check_expression(expr.left, schema)
            self._check_expression(expr.right, schema)
            if expr.op in ("+", "-", "*", "/", "%"):
                for operand in (expr.left, expr.right):
                    operand_type = self.infer_type(operand, schema)
                    if operand_type is not None and operand_type not in NUMERIC_TYPES:
                        raise SemanticError("算术运算不支持 %s 类型的操作数" % operand_type,
                                            position=position_of(expr))
            else:
                left_type = self.infer_type(expr.left, schema)
                right_type = self.infer_type(expr.right, schema)
                if (left_type and right_type
                        and not self._comparable(left_type, right_type)):
                    raise SemanticError("无法比较 %s 与 %s 类型的值" % (left_type, right_type),
                                        position=position_of(expr))
            return
        if isinstance(expr, UnaryOp):
            self._check_expression(expr.operand, schema)
            if expr.op == "NOT":
                return
            operand_type = self.infer_type(expr.operand, schema)
            if operand_type is not None and operand_type not in NUMERIC_TYPES:
                raise SemanticError("一元 %s 只支持数值类型" % expr.op,
                                    position=position_of(expr))
            return
        if isinstance(expr, InExpr):
            self._check_expression(expr.operand, schema)
            for value in expr.values:
                self._check_expression(value, schema)
            return
        if isinstance(expr, LikeExpr):
            self._check_expression(expr.operand, schema)
            self._check_expression(expr.pattern, schema)
            return
        if isinstance(expr, IsNullExpr):
            self._check_expression(expr.operand, schema)
            return
        raise SemanticError("无法识别的表达式：%r" % expr, position=position_of(expr))

    def infer_type(self, expr, schema):
        """推断表达式类型，无法推断返回 None。"""
        if isinstance(expr, Literal):
            value = expr.value
            if value is None:
                return None
            if isinstance(value, bool):
                return TYPE_BOOL
            if isinstance(value, int):
                return TYPE_INT
            if isinstance(value, float):
                return TYPE_FLOAT
            return TYPE_TEXT
        if isinstance(expr, ColumnRef):
            column = schema.get_column(expr.name)
            return column.type if column else None
        if isinstance(expr, BinaryOp):
            if expr.op in ("+", "-", "*", "/", "%"):
                return TYPE_FLOAT
            return TYPE_BOOL
        if isinstance(expr, UnaryOp):
            if expr.op == "NOT":
                return TYPE_BOOL
            return self.infer_type(expr.operand, schema)
        if isinstance(expr, (InExpr, LikeExpr, IsNullExpr)):
            return TYPE_BOOL
        return None

    @staticmethod
    def _comparable(left, right):
        if left in NUMERIC_TYPES and right in NUMERIC_TYPES:
            return True
        return left == right

    @staticmethod
    def _compatible(target_type, value_type):
        if value_type is None:
            return True
        if target_type in NUMERIC_TYPES and value_type in NUMERIC_TYPES:
            return True
        if target_type == TYPE_BOOL:
            return value_type == TYPE_BOOL
        if target_type == TYPE_TEXT:
            return value_type == TYPE_TEXT
        return target_type == value_type
