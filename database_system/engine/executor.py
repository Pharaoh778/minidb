# -*- coding: utf-8 -*-
"""执行引擎：以火山模型（Volcano / Iterator）执行逻辑计划。

支持的算子：CreateTable、DropTable、ShowTables、Describe、
Insert、SeqScan、Filter、Project、Sort、Limit、Update、Delete。
"""

import re

from .storage_engine import DuplicateKeyError, StorageEngine
from ..sql_compiler.parser import (
    BinaryOp,
    ColumnRef,
    InExpr,
    IsNullExpr,
    LikeExpr,
    Literal,
    UnaryOp,
)
from ..sql_compiler.planner import (
    CreateTablePlan,
    DeletePlan,
    DescribePlan,
    DropTablePlan,
    ExplainPlan,
    FilterPlan,
    InsertPlan,
    LimitPlan,
    ProjectPlan,
    SeqScanPlan,
    ShowTablesPlan,
    SortPlan,
    UpdatePlan,
    expr_to_str,
)
from ..utils.helpers import format_table


class ExecutionError(Exception):
    """执行期错误。"""


class Row:
    """一行数据：行地址 rid + 列值字典。"""

    __slots__ = ("rid", "values")

    def __init__(self, rid, values):
        self.rid = rid
        self.values = values

    def __repr__(self):
        return "Row(rid=%r, values=%r)" % (self.rid, self.values)


class Result:
    """SQL 执行结果。"""

    def __init__(self, columns=None, rows=None, message=None, rowcount=0):
        self.columns = columns or []
        self.rows = rows or []
        self.message = message
        self.rowcount = rowcount

    @property
    def is_query(self):
        return bool(self.columns)

    def to_table(self):
        return format_table(self.columns, self.rows)

    def __str__(self):
        if self.is_query:
            return self.to_table()
        return self.message or ""


# ============================ 表达式求值 ============================
def evaluate(expr, values):
    """在给定的一行上求值表达式（遵循 SQL 三值逻辑）。"""
    if isinstance(expr, Literal):
        return expr.value

    if isinstance(expr, ColumnRef):
        if expr.name in values:
            return values[expr.name]
        raise ExecutionError("未知的列名：%s" % expr.name)

    if isinstance(expr, BinaryOp):
        left = evaluate(expr.left, values)
        right = evaluate(expr.right, values)
        return _apply_binary(expr.op, left, right)

    if isinstance(expr, UnaryOp):
        operand = evaluate(expr.operand, values)
        if expr.op == "NOT":
            return None if operand is None else (not _truth(operand))
        if operand is None:
            return None
        if expr.op == "-":
            return -operand
        return +operand

    if isinstance(expr, IsNullExpr):
        operand = evaluate(expr.operand, values)
        return (operand is not None) if expr.negated else (operand is None)

    if isinstance(expr, InExpr):
        operand = evaluate(expr.operand, values)
        if operand is None:
            return None
        found_null = False
        for candidate in expr.values:
            value = evaluate(candidate, values)
            if value is None:
                found_null = True
            elif _values_equal(operand, value):
                return not expr.negated
        if found_null:
            return None
        return expr.negated

    if isinstance(expr, LikeExpr):
        operand = evaluate(expr.operand, values)
        pattern = evaluate(expr.pattern, values)
        if operand is None or pattern is None:
            return None
        matched = re.fullmatch(_like_to_regex(str(pattern)), str(operand)) is not None
        return (not matched) if expr.negated else matched

    raise ExecutionError("无法求值的表达式：%r" % expr)


def _like_to_regex(pattern):
    """把 SQL LIKE 模式转换为正则表达式。"""
    out = ["(?s)"]
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _values_equal(left, right):
    return left == right


def _truth(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _apply_binary(op, left, right):
    # 逻辑运算：三值逻辑
    if op == "AND":
        if left is None or right is None:
            if left is False or right is False:
                return False
            return None
        return _truth(left) and _truth(right)
    if op == "OR":
        if left is None or right is None:
            if left is True or right is True:
                return True
            return None
        return _truth(left) or _truth(right)

    # 比较与算术：NULL 传播
    if left is None or right is None:
        return None

    if op == "=":
        return left == right
    if op in ("!=", "<>"):
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right

    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            raise ExecutionError("除数为 0")
        return left / right
    if op == "%":
        if right == 0:
            raise ExecutionError("模运算的除数为 0")
        return left % right

    raise ExecutionError("不支持的运算符：%s" % op)


def is_true(value):
    """判断过滤条件是否成立（NULL 视为不成立）。"""
    return value is True or (value is not None and _truth(value))


# ============================ 算子 ============================
class Operator:
    """算子接口：open / next / close。"""

    def open(self):
        pass

    def next(self):
        return None

    def close(self):
        pass


class SeqScanOperator(Operator):
    """全表扫描。"""

    def __init__(self, engine, table_name):
        self.engine = engine
        self.table_name = table_name
        self._iterator = None

    def open(self):
        self._iterator = self.engine.scan_table(self.table_name)

    def next(self):
        if self._iterator is None:
            return None
        try:
            rid, values = next(self._iterator)
        except StopIteration:
            return None
        return Row(rid, values)

    def close(self):
        self._iterator = None


class FilterOperator(Operator):
    def __init__(self, child, predicate):
        self.child = child
        self.predicate = predicate

    def open(self):
        self.child.open()

    def next(self):
        while True:
            row = self.child.next()
            if row is None:
                return None
            if is_true(evaluate(self.predicate, row.values)):
                return row

    def close(self):
        self.child.close()


class ProjectOperator(Operator):
    def __init__(self, child, items):
        self.child = child
        self.items = items

    def open(self):
        self.child.open()

    def next(self):
        row = self.child.next()
        if row is None:
            return None
        if any(item.star for item in self.items):
            return Row(row.rid, dict(row.values))

        projected = {}
        for item in self.items:
            value = evaluate(item.expr, row.values)
            if item.alias:
                name = item.alias
            elif isinstance(item.expr, ColumnRef):
                name = item.expr.name
            else:
                name = expr_to_str(item.expr)
            projected[name] = value
        return Row(row.rid, projected)

    def close(self):
        self.child.close()


class SortOperator(Operator):
    def __init__(self, child, keys):
        self.child = child
        self.keys = keys
        self._rows = None
        self._index = 0

    def open(self):
        self.child.open()
        rows = []
        while True:
            row = self.child.next()
            if row is None:
                break
            rows.append(row)
        for key in reversed(self.keys):
            rows.sort(key=lambda r: _sort_key(evaluate(key.expr, r.values)),
                      reverse=key.descending)
        self._rows = rows
        self._index = 0

    def next(self):
        if self._rows is None or self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def close(self):
        self._rows = None
        self.child.close()


def _sort_key(value):
    """排序键：NULL 最小，数值次之，字符串最后。"""
    if value is None:
        return (0, 0.0, "")
    if isinstance(value, bool):
        return (1, float(value), "")
    if isinstance(value, (int, float)):
        return (1, float(value), "")
    return (2, 0.0, str(value))


class LimitOperator(Operator):
    def __init__(self, child, limit):
        self.child = child
        self.limit = limit
        self._count = 0

    def open(self):
        self.child.open()
        self._count = 0

    def next(self):
        if self._count >= self.limit:
            return None
        row = self.child.next()
        if row is None:
            return None
        self._count += 1
        return row

    def close(self):
        self.child.close()


# ============================ 执行引擎 ============================
class Executor:
    """把逻辑计划实例化为算子流水线并执行。"""

    def __init__(self, engine, logger=None):
        if not isinstance(engine, StorageEngine):
            raise TypeError("engine 必须是 StorageEngine 实例")
        self.engine = engine
        self.logger = logger

    # ---------------- 对外接口 ----------------
    def execute(self, plan):
        if isinstance(plan, ExplainPlan):
            return Result(message=plan.inner.describe())

        if isinstance(plan, CreateTablePlan):
            return self._execute_create(plan)
        if isinstance(plan, DropTablePlan):
            return self._execute_drop(plan)
        if isinstance(plan, ShowTablesPlan):
            return self._execute_show_tables()
        if isinstance(plan, DescribePlan):
            return self._execute_describe(plan)
        if isinstance(plan, InsertPlan):
            return self._execute_insert(plan)
        if isinstance(plan, UpdatePlan):
            return self._execute_update(plan)
        if isinstance(plan, DeletePlan):
            return self._execute_delete(plan)
        if isinstance(plan, (SeqScanPlan, FilterPlan, ProjectPlan, SortPlan, LimitPlan)):
            return self._execute_query(plan)
        raise ExecutionError("无法执行的计划：%r" % plan)

    # ---------------- DDL ----------------
    def _execute_create(self, plan):
        self.engine.create_table(plan.schema)
        return Result(message="表 %s 创建成功" % plan.schema.name)

    def _execute_drop(self, plan):
        self.engine.drop_table(plan.table_name)
        return Result(message="表 %s 已删除" % plan.table_name)

    def _execute_show_tables(self):
        tables = [name for name in self.engine.list_tables()]
        return Result(columns=["tables"], rows=[[name] for name in tables], rowcount=len(tables))

    def _execute_describe(self, plan):
        schema = self.engine.get_schema(plan.table_name)
        if schema is None:
            raise ExecutionError("表 %s 不存在" % plan.table_name)
        rows = [[col.name, col.type,
                 "NO" if col.not_null else "YES",
                 "PRI" if col.primary_key else ""]
                for col in schema.columns]
        return Result(columns=["field", "type", "null", "key"], rows=rows, rowcount=len(rows))

    # ---------------- DML ----------------
    def _execute_insert(self, plan):
        schema = self.engine.get_schema(plan.table_name)
        if schema is None:
            raise ExecutionError("表 %s 不存在" % plan.table_name)

        target_columns = ([schema.get_column(c) for c in plan.columns]
                          if plan.columns else list(schema.columns))
        count = 0
        for row_exprs in plan.rows:
            values = {}
            for column, expr in zip(target_columns, row_exprs):
                values[column.name] = evaluate(expr, {})
            try:
                self.engine.insert_row(plan.table_name, values)
            except DuplicateKeyError as exc:
                raise ExecutionError(str(exc))
            count += 1
        return Result(message="成功插入 %d 行" % count, rowcount=count)

    def _execute_update(self, plan):
        operator = self._build_operator(plan.child)
        operator.open()
        updated = 0
        try:
            while True:
                row = operator.next()
                if row is None:
                    break
                new_values = {}
                for column_name, expr in plan.assignments:
                    new_values[column_name] = evaluate(expr, row.values)
                self.engine.update_row(plan.table_name, row.rid, new_values)
                updated += 1
        finally:
            operator.close()
        return Result(message="成功更新 %d 行" % updated, rowcount=updated)

    def _execute_delete(self, plan):
        operator = self._build_operator(plan.child)
        operator.open()
        rids = []
        try:
            while True:
                row = operator.next()
                if row is None:
                    break
                rids.append(row.rid)
        finally:
            operator.close()

        for rid in rids:
            self.engine.delete_row(plan.table_name, rid)
        return Result(message="成功删除 %d 行" % len(rids), rowcount=len(rids))

    # ---------------- 查询 ----------------
    def _execute_query(self, plan):
        operator = self._build_operator(plan)
        operator.open()
        values_list = []
        try:
            while True:
                row = operator.next()
                if row is None:
                    break
                values_list.append(row.values)
        finally:
            operator.close()

        columns = self._output_columns(plan, values_list)
        rows = [[values.get(name) for name in columns] for values in values_list]
        return Result(columns=columns, rows=rows, rowcount=len(rows))

    def _output_columns(self, plan, values_list):
        node = plan
        while node is not None:
            if isinstance(node, ProjectPlan):
                return list(node.output_columns)
            node = node.children()[0] if node.children() else None
        return list(values_list[0].keys()) if values_list else []

    # ---------------- 算子构建 ----------------
    def _build_operator(self, plan):
        if isinstance(plan, SeqScanPlan):
            return SeqScanOperator(self.engine, plan.table_name)
        if isinstance(plan, FilterPlan):
            return FilterOperator(self._build_operator(plan.child), plan.predicate)
        if isinstance(plan, ProjectPlan):
            return ProjectOperator(self._build_operator(plan.child), plan.items)
        if isinstance(plan, SortPlan):
            return SortOperator(self._build_operator(plan.child), plan.keys)
        if isinstance(plan, LimitPlan):
            return LimitOperator(self._build_operator(plan.child), plan.limit)
        raise ExecutionError("无法构建算子的计划：%r" % plan)
