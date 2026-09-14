# -*- coding: utf-8 -*-
"""执行引擎：以火山模型（Volcano / Iterator）执行逻辑计划。

支持的算子：CreateTable、DropTable、ShowTables、Describe、
Insert、SeqScan、Filter、Project、Sort、Limit、Update、Delete。
"""

import operator
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
# 火山模型会把同一棵子树对每一行重复求值。若每次都递归解释一遍 AST，
# isinstance 分派、运算符字符串比较、投影列输出名拼接这些固定开销会淹没
# 真正的计算。这里在算子 open() 阶段做一次编译：把表达式树翻译成等价的
# 闭包树，运算符绑定、LIKE 正则、IN 候选常量、列名全部提前定下来，
# 之后每行只剩函数调用，谓词与投影求值通常能快 2~3 倍。


def evaluate(expr, values):
    """对外保留的一次性求值入口（编译后立即执行一次）。"""
    return compile_expr(expr)(values)


def compile_expr(expr):
    """把表达式树编译成闭包 f(values) -> value。

    生成的闭包与解释执行严格等价：NULL 传播、三值逻辑、除零报错保持一致。
    """
    if isinstance(expr, Literal):
        value = expr.value

        def _literal(values, _value=value):
            return _value

        return _literal

    if isinstance(expr, ColumnRef):
        name = expr.name

        def _column(values, _name=name):
            try:
                return values[_name]
            except KeyError:
                raise ExecutionError("未知的列名：%s" % _name)

        return _column

    if isinstance(expr, BinaryOp):
        builder = _BINARY_BUILDERS.get(expr.op)
        if builder is None:
            raise ExecutionError("不支持的运算符：%s" % expr.op)
        return builder(expr.left, expr.right)

    if isinstance(expr, UnaryOp):
        return _build_unary(expr)

    if isinstance(expr, IsNullExpr):
        return _build_is_null(expr)

    if isinstance(expr, InExpr):
        return _build_in(expr)

    if isinstance(expr, LikeExpr):
        return _build_like(expr)

    raise ExecutionError("无法求值的表达式：%r" % expr)


# ---------------- 二元运算：运算符在编译期绑定 ----------------
def _build_binary_nullable(py_op):
    """工厂：NULL 传播 + 一次原生运算（比较、加减乘）。"""

    def build(left, right):
        left_fn = compile_expr(left)
        right_fn = compile_expr(right)

        def _binary(values, _left=left_fn, _right=right_fn, _op=py_op):
            lv = _left(values)
            rv = _right(values)
            if lv is None or rv is None:
                return None
            return _op(lv, rv)

        return _binary

    return build


def _build_binary_div(left, right):
    left_fn = compile_expr(left)
    right_fn = compile_expr(right)

    def _div(values, _left=left_fn, _right=right_fn):
        lv = _left(values)
        rv = _right(values)
        if lv is None or rv is None:
            return None
        if rv == 0:
            raise ExecutionError("除数为 0")
        return lv / rv

    return _div


def _build_binary_mod(left, right):
    left_fn = compile_expr(left)
    right_fn = compile_expr(right)

    def _mod(values, _left=left_fn, _right=right_fn):
        lv = _left(values)
        rv = _right(values)
        if lv is None or rv is None:
            return None
        if rv == 0:
            raise ExecutionError("模运算的除数为 0")
        return lv % rv

    return _mod


def _build_binary_and(left, right):
    left_fn = compile_expr(left)
    right_fn = compile_expr(right)

    def _and(values, _left=left_fn, _right=right_fn, _truth_fn=_truth):
        lv = _left(values)
        # FALSE AND x 恒为 FALSE（x 是 NULL 也一样），可短路
        if lv is False:
            return False
        rv = _right(values)
        if lv is None or rv is None:
            if lv is False or rv is False:
                return False
            return None
        return _truth_fn(lv) and _truth_fn(rv)

    return _and


def _build_binary_or(left, right):
    left_fn = compile_expr(left)
    right_fn = compile_expr(right)

    def _or(values, _left=left_fn, _right=right_fn, _truth_fn=_truth):
        lv = _left(values)
        # TRUE OR x 恒为 TRUE（x 是 NULL 也一样），可短路
        if lv is True:
            return True
        rv = _right(values)
        if lv is None or rv is None:
            if lv is True or rv is True:
                return True
            return None
        return _truth_fn(lv) or _truth_fn(rv)

    return _or


_BINARY_BUILDERS = {
    "AND": _build_binary_and,
    "OR": _build_binary_or,
    "=": _build_binary_nullable(operator.eq),
    "!=": _build_binary_nullable(operator.ne),
    "<>": _build_binary_nullable(operator.ne),
    "<": _build_binary_nullable(operator.lt),
    "<=": _build_binary_nullable(operator.le),
    ">": _build_binary_nullable(operator.gt),
    ">=": _build_binary_nullable(operator.ge),
    "+": _build_binary_nullable(operator.add),
    "-": _build_binary_nullable(operator.sub),
    "*": _build_binary_nullable(operator.mul),
    "/": _build_binary_div,
    "%": _build_binary_mod,
}


# ---------------- 一元 / NULL / IN / LIKE ----------------
def _build_unary(expr):
    operand_fn = compile_expr(expr.operand)
    op = expr.op

    if op == "NOT":
        def _not(values, _operand=operand_fn, _truth_fn=_truth):
            value = _operand(values)
            return None if value is None else (not _truth_fn(value))

        return _not

    if op == "-":
        def _neg(values, _operand=operand_fn):
            value = _operand(values)
            return None if value is None else -value

        return _neg

    def _pos(values, _operand=operand_fn):
        value = _operand(values)
        return None if value is None else +value

    return _pos


def _build_is_null(expr):
    operand_fn = compile_expr(expr.operand)

    if expr.negated:
        def _not_null(values, _operand=operand_fn):
            return _operand(values) is not None

        return _not_null

    def _null(values, _operand=operand_fn):
        return _operand(values) is None

    return _null


def _build_in(expr):
    operand_fn = compile_expr(expr.operand)
    negated = expr.negated
    # 候选值通常全是字面量：预先取出，避免每行重建候选列表
    if all(isinstance(item, Literal) for item in expr.values):
        static = tuple(item.value for item in expr.values)
        dynamic = None
    else:
        static = None
        dynamic = [compile_expr(item) for item in expr.values]

    def _in(values, _operand=operand_fn, _static=static, _dynamic=dynamic,
            _equal=_values_equal, _negated=negated):
        operand = _operand(values)
        if operand is None:
            return None
        pool = _static if _static is not None else [fn(values) for fn in _dynamic]
        found_null = False
        for value in pool:
            if value is None:
                found_null = True
            elif _equal(operand, value):
                return not _negated
        return None if found_null else _negated

    return _in


def _build_like(expr):
    operand_fn = compile_expr(expr.operand)
    negated = expr.negated
    cache = {}

    # 模式通常是字面量：正则只编译一次，而不是每行编译一次
    if isinstance(expr.pattern, Literal):
        def _like_static(values, _operand=operand_fn,
                         _regex=re.compile(_like_to_regex(str(expr.pattern.value))),
                         _negated=negated):
            operand = _operand(values)
            if operand is None:
                return None
            matched = _regex.fullmatch(str(operand)) is not None
            return (not matched) if _negated else matched

        return _like_static

    pattern_fn = compile_expr(expr.pattern)

    def _like_dynamic(values, _operand=operand_fn, _pattern=pattern_fn,
                      _cache=cache, _negated=negated):
        operand = _operand(values)
        pattern = _pattern(values)
        if operand is None or pattern is None:
            return None
        regex = _cache.get(pattern)
        if regex is None:
            regex = _cache[pattern] = re.compile(_like_to_regex(str(pattern)))
        matched = regex.fullmatch(str(operand)) is not None
        return (not matched) if _negated else matched

    return _like_dynamic


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
    """全表扫描。

    columns 给出需要解码的列（执行期列裁剪）；为 None 时解码全部列。
    """

    def __init__(self, engine, table_name, columns=None):
        self.engine = engine
        self.table_name = table_name
        self.columns = columns
        self._iterator = ()
        self._next_row = None

    def open(self):
        self._iterator = self.engine.scan_table(self.table_name, self.columns)
        self._next_row = self._iterator.__next__

    def next(self):
        next_row = self._next_row
        if next_row is None:
            return None
        try:
            rid, values = next_row()
        except StopIteration:
            return None
        return Row(rid, values)

    def close(self):
        self._iterator = ()
        self._next_row = None


class FilterScanOperator(Operator):
    """SeqScan 与 Filter 的物理融合算子。

    逻辑上等价于 Filter(SeqScan)，但把谓词求值放进扫描循环内部：
    被淘汰的行不再构造 Row 对象、也不必再经过一层算子调用；
    多个相邻 Filter 会合成一次合取判断，命中的第一个 False 立即退出。
    """

    def __init__(self, engine, table_name, predicates, columns=None):
        self.engine = engine
        self.table_name = table_name
        self.predicates = predicates or []
        self.columns = columns
        self._iterator = ()
        self._predicates = ()

    def open(self):
        self._predicates = tuple(compile_expr(p) for p in self.predicates)
        self._iterator = self.engine.scan_table(self.table_name, self.columns)

    def next(self):
        predicates = self._predicates
        for rid, values in self._iterator:
            keep = True
            for predicate in predicates:
                if not is_true(predicate(values)):
                    keep = False
                    break
            if keep:
                return Row(rid, values)
        return None

    def close(self):
        self._iterator = ()
        self._predicates = ()


class FilterOperator(Operator):
    """通用过滤算子：仅当子算子不是 SeqScan 时才需要它。"""

    def __init__(self, child, predicate):
        self.child = child
        self.predicate = predicate
        self._predicate = None
        self._child_next = None

    def open(self):
        self.child.open()
        self._predicate = compile_expr(self.predicate)
        self._child_next = self.child.next

    def next(self):
        child_next = self._child_next
        predicate = self._predicate
        while True:
            row = child_next()
            if row is None:
                return None
            if is_true(predicate(row.values)):
                return row

    def close(self):
        self.child.close()
        self._child_next = None


def _projection_name(item):
    """投影列的输出名（每行拼一次字符串太贵，这里只算一次）。"""
    if item.alias:
        return item.alias
    if isinstance(item.expr, ColumnRef):
        return item.expr.name
    return expr_to_str(item.expr)


class ProjectOperator(Operator):
    def __init__(self, child, items):
        self.child = child
        self.items = items
        # 输出名、是否有 *、表达式闭包都只算一次：它们与具体行无关
        self._has_star = any(item.star for item in items)
        self._projections = [(_projection_name(item), compile_expr(item.expr))
                             for item in items if item.expr is not None]
        self._child_next = None

    def open(self):
        self.child.open()
        self._child_next = self.child.next

    def next(self):
        row = self._child_next()
        if row is None:
            return None
        values = row.values
        if self._has_star:
            return Row(row.rid, dict(values))

        projected = {}
        for name, fn in self._projections:
            projected[name] = fn(values)
        return Row(row.rid, projected)

    def close(self):
        self.child.close()
        self._child_next = None


class SortOperator(Operator):
    def __init__(self, child, keys):
        self.child = child
        self.keys = keys
        self._rows = None
        self._index = 0
        # 排序键表达式提前编译：多趟排序会重复用它求值
        self._key_functions = [(compile_expr(key.expr), key.descending)
                               for key in reversed(keys)]

    def open(self):
        self.child.open()
        child_next = self.child.next
        rows = []
        while True:
            row = child_next()
            if row is None:
                break
            rows.append(row)
        for fn, descending in self._key_functions:
            rows.sort(key=lambda row, _fn=fn: _sort_key(_fn(row.values)),
                      reverse=descending)
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
        self._child_next = None

    def open(self):
        self.child.open()
        self._count = 0
        self._child_next = self.child.next

    def next(self):
        if self._count >= self.limit:
            return None
        row = self._child_next()
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
        empty = {}
        for row_exprs in plan.rows:
            values = {}
            for column, expr in zip(target_columns, row_exprs):
                values[column.name] = evaluate(expr, empty)
            try:
                self.engine.insert_row(plan.table_name, values)
            except DuplicateKeyError as exc:
                raise ExecutionError(str(exc))
            count += 1
        return Result(message="成功插入 %d 行" % count, rowcount=count)

    def _execute_update(self, plan):
        operator = self._build_operator(plan.child)
        compiled = [(column_name, compile_expr(expr))
                    for column_name, expr in plan.assignments]
        operator.open()
        updated = 0
        try:
            while True:
                row = operator.next()
                if row is None:
                    break
                values = row.values
                new_values = {column_name: fn(values) for column_name, fn in compiled}
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
            return SeqScanOperator(self.engine, plan.table_name,
                                   self._scan_columns(plan))
        if isinstance(plan, FilterPlan):
            # 把相邻的 Filter 链整条收集起来：底部是 SeqScan 时融合成一个算子，
            # 避免逐层传递 Row，也省掉每层一次生成器进出。
            predicates = []
            node = plan
            while isinstance(node, FilterPlan):
                predicates.append(node.predicate)
                node = node.child
            if isinstance(node, SeqScanPlan):
                return FilterScanOperator(self.engine, node.table_name, predicates,
                                          self._scan_columns(node))
            operator = self._build_operator(node)
            for predicate in reversed(predicates):
                operator = FilterOperator(operator, predicate)
            return operator
        if isinstance(plan, ProjectPlan):
            return ProjectOperator(self._build_operator(plan.child), plan.items)
        if isinstance(plan, SortPlan):
            return SortOperator(self._build_operator(plan.child), plan.keys)
        if isinstance(plan, LimitPlan):
            return LimitOperator(self._build_operator(plan.child), plan.limit)
        raise ExecutionError("无法构建算子的计划：%r" % plan)

    def _scan_columns(self, plan):
        """扫描时真正需要解码的列（执行期的投影下推）。

        优化器早已把上层引用到的列算进 required_columns，但算子原先并不理会，
        扫描仍然逐行解码全部列。这里把扫描算子接上去，让 RecordCodec 直接
        跳过无用列的字节（跳过 TEXT 只需读它的长度前缀）。
        """
        columns = plan.required_columns
        if not columns:
            return None
        schema = self.engine.get_schema(plan.table_name)
        if schema is None or any(schema.get_column(name) is None for name in columns):
            return None           # 计划不可信时退回全列扫描，保证结果正确
        return list(columns)
