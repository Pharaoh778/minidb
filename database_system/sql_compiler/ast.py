# -*- coding: utf-8 -*-
"""AST 树形打印器。

课程 PPT 要求：语法分析的产物 AST 必须可见、可验证，
因此提供统一入口 format_ast()，把 AST 以缩进树的形式输出，
便于调试、测试用例断言与实验报告截图。
"""

from .parser import (
    BinaryOp,
    ColumnDef,
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
    OrderByItem,
    SelectItem,
    SelectStmt,
    ShowTablesStmt,
    UnaryOp,
    UpdateStmt,
)


class _Node:
    """打印用的辅助节点（用于 FROM / WHERE 这类非 AST 类的分组标签）。"""

    def __init__(self, label, children=None):
        self.label = label
        self.children = children or []


def _literal_text(value):
    if isinstance(value, str):
        return "'%s'" % value
    return repr(value)


def _describe(node):
    """把节点转换为 (标签, 子节点列表)。"""
    if isinstance(node, _Node):
        return node.label, node.children

    if isinstance(node, Literal):
        if node.value is None:
            return "Literal(NULL)", []
        if isinstance(node.value, bool):
            return "Literal(%s)" % ("TRUE" if node.value else "FALSE"), []
        return "Literal(%s)" % _literal_text(node.value), []

    if isinstance(node, ColumnRef):
        return "ColumnRef(%s)" % node.name, []

    if isinstance(node, BinaryOp):
        return "BinaryOp(%s)" % node.op, [node.left, node.right]

    if isinstance(node, UnaryOp):
        return "UnaryOp(%s)" % node.op, [node.operand]

    if isinstance(node, InExpr):
        label = "InExpr(%s)" % ("NOT IN" if node.negated else "IN")
        return label, [node.operand] + list(node.values)

    if isinstance(node, LikeExpr):
        label = "LikeExpr(%s)" % ("NOT LIKE" if node.negated else "LIKE")
        return label, [node.operand, node.pattern]

    if isinstance(node, IsNullExpr):
        label = "IsNullExpr(%s)" % ("IS NOT NULL" if node.negated else "IS NULL")
        return label, [node.operand]

    if isinstance(node, ColumnDef):
        flags = []
        if node.primary_key:
            flags.append("PRIMARY KEY")
        if node.not_null:
            flags.append("NOT NULL")
        suffix = " " + " ".join(flags) if flags else ""
        return "ColumnDef(%s %s%s)" % (node.name, node.type, suffix), []

    if isinstance(node, SelectItem):
        if node.star:
            return "SelectItem(*)", []
        if node.alias:
            return "SelectItem(AS %s)" % node.alias, [node.expr]
        return "SelectItem", [node.expr]

    if isinstance(node, OrderByItem):
        return "OrderByItem(%s)" % ("DESC" if node.descending else "ASC"), [node.expr]

    if isinstance(node, CreateTableStmt):
        label = "CreateTable(%s%s)" % (node.table_name,
                                       " IF NOT EXISTS" if node.if_not_exists else "")
        return label, list(node.columns)

    if isinstance(node, DropTableStmt):
        label = "DropTable(%s%s)" % (node.table_name,
                                     " IF EXISTS" if node.if_exists else "")
        return label, []

    if isinstance(node, ShowTablesStmt):
        return "ShowTables", []

    if isinstance(node, DescribeStmt):
        return "Describe(%s)" % node.table_name, []

    if isinstance(node, InsertStmt):
        children = []
        if node.columns:
            children.append(_Node("Columns(%s)" % ", ".join(node.columns)))
        else:
            children.append(_Node("Columns(全部列)"))
        for index, row in enumerate(node.rows, 1):
            children.append(_Node("Row %d" % index, list(row)))
        return "Insert(%s)" % node.table_name, children

    if isinstance(node, SelectStmt):
        children = [_Node("Projection", list(node.items)),
                    _Node("From(%s)" % node.table_name)]
        if node.where is not None:
            children.append(_Node("Where", [node.where]))
        if node.order_by:
            children.append(_Node("OrderBy", list(node.order_by)))
        if node.limit is not None:
            children.append(_Node("Limit(%d)" % node.limit))
        return "Select", children

    if isinstance(node, UpdateStmt):
        children = [_Node("Set", [_Node("Assign(%s)" % name, [expr])
                                  for name, expr in node.assignments])]
        if node.where is not None:
            children.append(_Node("Where", [node.where]))
        return "Update(%s)" % node.table_name, children

    if isinstance(node, DeleteStmt):
        children = [_Node("From(%s)" % node.table_name)]
        if node.where is not None:
            children.append(_Node("Where", [node.where]))
        return "Delete", children

    if isinstance(node, ExplainStmt):
        return "Explain", [node.statement]

    return repr(node), []


def _lines(node, level):
    label, children = _describe(node)
    prefix = "  " * level
    out = ["%s-> %s" % (prefix, label)]
    for child in children:
        out.append(_lines(child, level + 1))
    return "\n".join(out)


def format_ast(node):
    """把 AST 渲染为树形字符串（根节点无缩进前缀）。"""
    if node is None:
        return "(空)"
    return _lines(node, 0)


def print_ast(node):
    """直接打印 AST 树。"""
    print(format_ast(node))
