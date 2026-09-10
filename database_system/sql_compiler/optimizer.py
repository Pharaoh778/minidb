# -*- coding: utf-8 -*-
"""基于规则的查询优化器（Rule-Based Optimizer）。

课程 PPT 第 28~29 页要求：至少实现若干规则式优化，并能展示优化前后的差异。
本模块实现 5 条规则：

    1. 常量折叠   Constant Folding      —— WHERE age > 10 + 8  =>  WHERE age > 18
    2. 布尔化简   Boolean Simplification —— WHERE 1 = 1 AND ... =>  WHERE ...
    3. 列裁剪     Projection Pruning     —— SeqScan 只保留查询真正需要的列
    4. 谓词下推   Predicate Pushdown     —— 合取分解，使每个 Filter 紧贴 SeqScan
    5. 冗余节点消除 Redundant Node Elimination —— 去掉恒真 Filter、重复 Project/Sort

设计原则：只做「语义等价」的改写，保证优化前后查询结果一致。
"""

from .parser import (
    BinaryOp,
    ColumnRef,
    InExpr,
    IsNullExpr,
    LikeExpr,
    Literal,
    OrderByItem,
    SelectItem,
    UnaryOp,
)
from .planner import (
    DeletePlan,
    FilterPlan,
    InsertPlan,
    ProjectPlan,
    SeqScanPlan,
    SortPlan,
    UpdatePlan,
)

RULE_FOLD = "常量折叠"
RULE_BOOL = "布尔化简"
RULE_PRUNE = "列裁剪"
RULE_PUSHDOWN = "谓词下推"
RULE_CLEANUP = "冗余节点消除"


class _Unfoldable(object):
    """哨兵：表示该表达式不能折叠。"""


_UNFOLDABLE = _Unfoldable()


# ============================ 表达式层规则 ============================
def _record(applied, rule):
    if applied is not None and rule not in applied:
        applied.append(rule)


def _is_true(expr):
    return isinstance(expr, Literal) and expr.value is True


def _is_false(expr):
    return isinstance(expr, Literal) and expr.value is False


def _try_eval(op, left, right):
    """对两个常量求值；无法安全求值（除零、类型不兼容等）时返回哨兵。"""
    try:
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return _UNFOLDABLE if right == 0 else left / right
        if op == "%":
            return _UNFOLDABLE if right == 0 else left % right
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
    except (TypeError, ValueError, ZeroDivisionError):
        return _UNFOLDABLE
    return _UNFOLDABLE


def _simplify_boolean(expr, applied):
    """布尔化简：去掉恒真/恒假的合取、析取分支。"""
    if not isinstance(expr, BinaryOp):
        return expr
    left, right, op = expr.left, expr.right, expr.op

    if op == "AND":
        if _is_false(left) or _is_false(right):
            _record(applied, RULE_BOOL)
            return Literal(False)
        if _is_true(left):
            _record(applied, RULE_BOOL)
            return right
        if _is_true(right):
            _record(applied, RULE_BOOL)
            return left
    elif op == "OR":
        if _is_true(left) or _is_true(right):
            _record(applied, RULE_BOOL)
            return Literal(True)
        if _is_false(left):
            _record(applied, RULE_BOOL)
            return right
        if _is_false(right):
            _record(applied, RULE_BOOL)
            return left
    return expr


def _with_position(new_expr, old_expr):
    """改写表达式时保留源码位置，避免丢失错误定位信息。"""
    position = getattr(old_expr, "position", None)
    if position is not None:
        new_expr.position = position
    return new_expr


def fold_expression(expr, applied=None):
    """对表达式递归应用「常量折叠 + 布尔化简」。

    注意：本函数是**不可变**改写——不修改原 AST，
    这样才能同时保留「优化前 / 优化后」两棵计划树用于对比展示。
    """
    if isinstance(expr, BinaryOp):
        left = fold_expression(expr.left, applied)
        right = fold_expression(expr.right, applied)
        new_expr = _with_position(BinaryOp(expr.op, left, right), expr)
        if _is_literal(left) and _is_literal(right):
            value = _try_eval(expr.op, left.value, right.value)
            if not isinstance(value, _Unfoldable):
                _record(applied, RULE_FOLD)
                return _with_position(Literal(value), expr)
        return _simplify_boolean(new_expr, applied)

    if isinstance(expr, UnaryOp):
        operand = fold_expression(expr.operand, applied)
        if expr.op == "NOT" and _is_literal(operand):
            _record(applied, RULE_BOOL)
            return _with_position(Literal(not operand.value), expr)
        return _with_position(UnaryOp(expr.op, operand), expr)

    if isinstance(expr, InExpr):
        return _with_position(
            InExpr(fold_expression(expr.operand, applied),
                   [fold_expression(v, applied) for v in expr.values],
                   expr.negated), expr)

    if isinstance(expr, LikeExpr):
        return _with_position(
            LikeExpr(fold_expression(expr.operand, applied),
                     fold_expression(expr.pattern, applied),
                     expr.negated), expr)

    if isinstance(expr, IsNullExpr):
        return _with_position(
            IsNullExpr(fold_expression(expr.operand, applied), expr.negated), expr)

    return expr


def _is_literal(expr):
    """是否为可参与折叠的常量（NULL 不参与）。"""
    return isinstance(expr, Literal) and expr.value is not None


# ============================ 计划层辅助 ============================
def _split_conjunction(expr):
    """把 a AND b AND c 拆成 [a, b, c]。"""
    if isinstance(expr, BinaryOp) and expr.op == "AND":
        return _split_conjunction(expr.left) + _split_conjunction(expr.right)
    return [expr]


def _find_scan(node):
    """找到计划树中的 SeqScan 节点。"""
    if isinstance(node, SeqScanPlan):
        return node
    for child in node.children():
        found = _find_scan(child)
        if found is not None:
            return found
    return None


def _collect_expr_columns(expr, out):
    """收集表达式引用到的列名。"""
    if isinstance(expr, ColumnRef):
        out.add(expr.name)
    elif isinstance(expr, BinaryOp):
        _collect_expr_columns(expr.left, out)
        _collect_expr_columns(expr.right, out)
    elif isinstance(expr, UnaryOp):
        _collect_expr_columns(expr.operand, out)
    elif isinstance(expr, InExpr):
        _collect_expr_columns(expr.operand, out)
        for value in expr.values:
            _collect_expr_columns(value, out)
    elif isinstance(expr, LikeExpr):
        _collect_expr_columns(expr.operand, out)
        _collect_expr_columns(expr.pattern, out)
    elif isinstance(expr, IsNullExpr):
        _collect_expr_columns(expr.operand, out)


def _collect_columns(node, out):
    """收集整个计划引用到的列；遇到 SELECT * 返回 False（无法裁剪）。"""
    if isinstance(node, ProjectPlan):
        for item in node.items:
            if item.star:
                return False
            if item.expr is not None:
                _collect_expr_columns(item.expr, out)
    if isinstance(node, FilterPlan) and node.predicate is not None:
        _collect_expr_columns(node.predicate, out)
    if isinstance(node, SortPlan):
        for key in node.keys:
            _collect_expr_columns(key.expr, out)
    if isinstance(node, UpdatePlan):
        for _name, expr in node.assignments:
            _collect_expr_columns(expr, out)
        if node.predicate is not None:
            _collect_expr_columns(node.predicate, out)
    if isinstance(node, DeletePlan) and node.predicate is not None:
        _collect_expr_columns(node.predicate, out)
    if isinstance(node, InsertPlan):
        return False                      # 插入需要写入全部列

    for child in node.children():
        if not _collect_columns(child, out):
            return False
    return True


# ============================ 优化器 ============================
class Optimizer:
    """规则式优化器：optimize(plan) 返回语义等价的优化后计划。"""

    def __init__(self):
        self.applied = []

    # ---------------- 入口 ----------------
    def optimize(self, plan):
        if plan is None:
            return plan
        self.applied = []
        plan = self._fold(plan)
        plan = self._pushdown(plan)
        plan = self._prune(plan)
        plan = self._cleanup(plan)
        return plan

    def report(self):
        """本次优化生效的规则清单（供 EXPLAIN 展示）。"""
        if not self.applied:
            return "（无可应用的优化规则）"
        return "；".join(self.applied)

    # ---------------- 规则 1 & 2 ----------------
    def _fold_select_item(self, item):
        """折叠投影项表达式，返回新的 SelectItem（不改动原对象）。"""
        if item.expr is None:
            return item
        new_item = SelectItem(expr=fold_expression(item.expr, self.applied),
                              star=item.star, alias=item.alias)
        position = getattr(item, "position", None)
        if position is not None:
            new_item.position = position
        return new_item

    def _fold(self, node):
        for child in node.children():
            self._fold(child)

        if isinstance(node, FilterPlan) and node.predicate is not None:
            node.predicate = fold_expression(node.predicate, self.applied)
        elif isinstance(node, UpdatePlan):
            node.assignments = [(name, fold_expression(expr, self.applied))
                                for name, expr in node.assignments]
            if node.predicate is not None:
                node.predicate = fold_expression(node.predicate, self.applied)
        elif isinstance(node, DeletePlan) and node.predicate is not None:
            node.predicate = fold_expression(node.predicate, self.applied)
        elif isinstance(node, ProjectPlan):
            # 重建 SelectItem，避免改动与 AST 共享的对象
            node.items = [self._fold_select_item(item) for item in node.items]
        elif isinstance(node, SortPlan):
            node.keys = [OrderByItem(fold_expression(key.expr, self.applied),
                                     key.descending)
                         for key in node.keys]
        elif isinstance(node, InsertPlan):
            node.rows = [[fold_expression(v, self.applied) for v in row]
                         for row in node.rows]
        return node

    # ---------------- 规则 4：谓词下推 ----------------
    def _pushdown(self, node):
        if getattr(node, "child", None) is not None:
            node.child = self._pushdown(node.child)

        if isinstance(node, FilterPlan):
            parts = _split_conjunction(node.predicate)
            if len(parts) > 1:
                self.applied.append("%s（合取分解为 %d 个 Filter）"
                                    % (RULE_PUSHDOWN, len(parts)))
                inner = node.child
                for part in parts:
                    inner = FilterPlan(inner, part)
                return inner
        return node

    # ---------------- 规则 3：列裁剪 ----------------
    def _prune(self, node):
        scan = _find_scan(node)
        if scan is None:
            return node
        needed = set()
        if not _collect_columns(node, needed) or not needed:
            return node                   # SELECT * / INSERT：不裁剪
        columns = sorted(needed)
        if scan.required_columns != columns:
            scan.required_columns = columns
            self.applied.append("%s（SeqScan 保留 %d 列：%s）"
                                % (RULE_PRUNE, len(columns), ", ".join(columns)))
        return node

    # ---------------- 规则 5：冗余节点消除 ----------------
    def _cleanup(self, node):
        if getattr(node, "child", None) is not None:
            node.child = self._cleanup(node.child)

        if isinstance(node, FilterPlan) and _is_true(node.predicate):
            self.applied.append("%s（恒真的 Filter）" % RULE_CLEANUP)
            return node.child

        if isinstance(node, ProjectPlan) and isinstance(node.child, ProjectPlan):
            self.applied.append("%s（重复的 Project）" % RULE_CLEANUP)
            node.child = node.child.child

        if isinstance(node, SortPlan) and isinstance(node.child, SortPlan):
            self.applied.append("%s（重复的 Sort）" % RULE_CLEANUP)
            node.child = node.child.child

        if isinstance(node, SortPlan) and not node.keys:
            self.applied.append("%s（空的 Sort）" % RULE_CLEANUP)
            return node.child

        return node


def optimize(plan, optimizer=None):
    """便捷函数：优化一个逻辑计划，返回 (优化后计划, 优化器)。"""
    opt = optimizer or Optimizer()
    return opt.optimize(plan), opt
