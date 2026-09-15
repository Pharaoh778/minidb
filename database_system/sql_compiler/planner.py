# -*- coding: utf-8 -*-
"""执行计划生成器：把 AST 转换为逻辑执行计划（关系代数算子树）。"""

from .catalog import Column, TableSchema
from .errors import PlanError
from .parser import (
    BinaryOp,
    ColumnRef,
    CreateTableStmt,
    DeleteStmt,
    DescribeStmt,
    DropTableStmt,
    ExplainStmt,
    InsertStmt,
    Literal,
    SelectStmt,
    ShowTablesStmt,
    UpdateStmt,
    UnaryOp,
)
from ..utils.constants import TYPE_INT


class Plan:
    """逻辑计划节点基类。"""

    def children(self):
        return []

    def label(self):
        return self.__class__.__name__

    def describe(self, level=0):
        indent = "  " * level
        lines = ["%s-> %s" % (indent, self.label())]
        for child in self.children():
            lines.append(child.describe(level + 1))
        return "\n".join(lines)

    def __str__(self):
        return self.describe()


class CreateTablePlan(Plan):
    def __init__(self, schema):
        self.schema = schema

    def label(self):
        return "CreateTable(%s)" % self.schema.name


class DropTablePlan(Plan):
    def __init__(self, table_name):
        self.table_name = table_name

    def label(self):
        return "DropTable(%s)" % self.table_name


class ShowTablesPlan(Plan):
    def label(self):
        return "ShowTables"


class DescribePlan(Plan):
    def __init__(self, table_name):
        self.table_name = table_name

    def label(self):
        return "Describe(%s)" % self.table_name


class InsertPlan(Plan):
    def __init__(self, table_name, columns, rows):
        self.table_name = table_name
        self.columns = columns
        self.rows = rows

    def label(self):
        target = "(%s)" % ", ".join(self.columns) if self.columns else "(全部列)"
        return "Insert(%s) %s, %d 行" % (self.table_name, target, len(self.rows))


class SeqScanPlan(Plan):
    """全表扫描。

    required_columns：由优化器的「列裁剪」规则填写，
    表示本次查询真正需要的列（None 表示读取全部列）。
    """

    def __init__(self, table_name, alias=None):
        self.table_name = table_name
        self.alias = alias
        self.required_columns = None

    def label(self):
        if self.required_columns:
            return "SeqScan(%s)[%s]" % (self.table_name, ", ".join(self.required_columns))
        return "SeqScan(%s)" % self.table_name


class FilterPlan(Plan):
    def __init__(self, child, predicate):
        self.child = child
        self.predicate = predicate

    def children(self):
        return [self.child]

    def label(self):
        return "Filter(%s)" % expr_to_str(self.predicate)


class ProjectPlan(Plan):
    def __init__(self, child, items, output_columns):
        self.child = child
        self.items = items            # List[SelectItem]
        self.output_columns = output_columns

    def children(self):
        return [self.child]

    def label(self):
        if any(item.star for item in self.items):
            return "Project(*)"
        return "Project(%s)" % ", ".join(self.output_columns)


class SortPlan(Plan):
    def __init__(self, child, keys):
        self.child = child
        self.keys = keys              # List[OrderByItem]

    def children(self):
        return [self.child]

    def label(self):
        text = ", ".join("%s %s" % (expr_to_str(k.expr), "DESC" if k.descending else "ASC")
                         for k in self.keys)
        return "Sort(%s)" % text


class LimitPlan(Plan):
    def __init__(self, child, limit):
        self.child = child
        self.limit = limit

    def children(self):
        return [self.child]

    def label(self):
        return "Limit(%d)" % self.limit


class UpdatePlan(Plan):
    def __init__(self, table_name, assignments, predicate, child):
        self.table_name = table_name
        self.assignments = assignments
        self.predicate = predicate
        self.child = child

    def children(self):
        return [self.child]

    def label(self):
        text = ", ".join("%s=%s" % (name, expr_to_str(expr)) for name, expr in self.assignments)
        return "Update(%s SET %s)" % (self.table_name, text)


class DeletePlan(Plan):
    def __init__(self, table_name, predicate, child):
        self.table_name = table_name
        self.predicate = predicate
        self.child = child

    def children(self):
        return [self.child]

    def label(self):
        return "Delete(%s)" % self.table_name


class ExplainPlan(Plan):
    def __init__(self, inner):
        self.inner = inner

    def children(self):
        return [self.inner]

    def label(self):
        return "Explain"


def expr_to_str(expr):
    """表达式的可读形式。"""
    if expr is None:
        return "TRUE"
    if isinstance(expr, Literal):
        return "NULL" if expr.value is None else repr(expr.value)
    if isinstance(expr, ColumnRef):
        return expr.name
    if isinstance(expr, BinaryOp):
        return "(%s %s %s)" % (expr_to_str(expr.left), expr.op, expr_to_str(expr.right))
    if isinstance(expr, UnaryOp):
        return "(%s %s)" % (expr.op, expr_to_str(expr.operand))
    return str(expr)


class Planner:
    """把语义分析后的 AST 转换为逻辑执行计划。"""

    def __init__(self, catalog):
        self.catalog = catalog

    def build(self, statement):
        if isinstance(statement, CreateTableStmt):
            return self._plan_create(statement)
        if isinstance(statement, DropTableStmt):
            return DropTablePlan(statement.table_name)
        if isinstance(statement, ShowTablesStmt):
            return ShowTablesPlan()
        if isinstance(statement, DescribeStmt):
            return DescribePlan(statement.table_name)
        if isinstance(statement, InsertStmt):
            return InsertPlan(statement.table_name, statement.columns, statement.rows)
        if isinstance(statement, SelectStmt):
            return self._plan_select(statement)
        if isinstance(statement, UpdateStmt):
            return self._plan_update(statement)
        if isinstance(statement, DeleteStmt):
            return self._plan_delete(statement)
        if isinstance(statement, ExplainStmt):
            return ExplainPlan(self.build(statement.statement))
        raise PlanError("无法生成执行计划的语句：%r" % (statement,),
                        position=getattr(statement, "position", None))

    # ---------------- 各类语句 ----------------
    @staticmethod
    def _plan_create(stmt):
        columns = [Column(c.name, c.type, c.length, c.primary_key, c.not_null)
                   for c in stmt.columns]
        return CreateTablePlan(TableSchema(stmt.table_name, columns))

    def _plan_select(self, stmt):
        # 执行顺序：SeqScan -> Filter -> Sort -> Project -> Limit
        node = SeqScanPlan(stmt.table_name)
        if stmt.where is not None:
            node = FilterPlan(node, stmt.where)
        if stmt.order_by:
            node = SortPlan(node, stmt.order_by)     # 排序在投影之前，可按任意列排序
        node = ProjectPlan(node, stmt.items, self._output_columns(stmt))
        if stmt.limit is not None:
            node = LimitPlan(node, stmt.limit)
        return node

    def _output_columns(self, stmt):
        names = []
        schema = self.catalog.get_table(stmt.table_name)
        for item in stmt.items:
            if item.star:
                if schema is not None:
                    names.extend(schema.column_names)
                else:
                    names.append("*")
            elif item.alias:
                names.append(item.alias)
            elif isinstance(item.expr, ColumnRef):
                names.append(item.expr.name)
            else:
                names.append(expr_to_str(item.expr))
        return names

    def _plan_update(self, stmt):
        scan = SeqScanPlan(stmt.table_name)
        child = FilterPlan(scan, stmt.where) if stmt.where is not None else scan
        return UpdatePlan(stmt.table_name, stmt.assignments, stmt.where, child)

    def _plan_delete(self, stmt):
        scan = SeqScanPlan(stmt.table_name)
        child = FilterPlan(scan, stmt.where) if stmt.where is not None else scan
        return DeletePlan(stmt.table_name, stmt.where, child)


def make_schema(stmt):
    """由 CREATE TABLE 语句构造 TableSchema（供存储引擎直接调用）。"""
    columns = [Column(c.name, c.type, c.length, c.primary_key, c.not_null)
               for c in stmt.columns]
    return TableSchema(stmt.table_name, columns)
