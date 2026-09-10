# -*- coding: utf-8 -*-
"""SQL 编译器测试：词法分析、语法分析、语义分析、执行计划生成。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.sql_compiler.catalog import Catalog, Column, TableSchema  # noqa: E402
from database_system.sql_compiler.lexer import tokenize  # noqa: E402
from database_system.sql_compiler.parser import (  # noqa: E402
    BinaryOp, ColumnRef, CreateTableStmt, DeleteStmt, InsertStmt, Literal,
    Parser, ParseError, SelectStmt, UpdateStmt,
)
from database_system.sql_compiler.planner import (  # noqa: E402
    FilterPlan, InsertPlan, Planner, ProjectPlan, SeqScanPlan, SortPlan, LimitPlan,
)
from database_system.sql_compiler.semantic import SemanticAnalyzer, SemanticError  # noqa: E402


def make_catalog():
    catalog = Catalog()
    catalog.create_table(TableSchema("student", [
        Column("id", "INT", primary_key=True),
        Column("name", "TEXT", length=20),
        Column("score", "FLOAT"),
    ]))
    return catalog


class TestLexer(unittest.TestCase):
    def test_keywords_and_identifiers(self):
        tokens = tokenize("SELECT id FROM student;")
        types = [t.type for t in tokens]
        values = [t.value for t in tokens]
        self.assertIn("KEYWORD", types)
        self.assertIn("IDENTIFIER", types)
        self.assertIn("SELECT", values)          # 关键字统一规范化为大写
        self.assertIn("student", values)         # 标识符统一规范化为小写
        self.assertEqual(values[-1], None)       # EOF

    def test_literals(self):
        tokens = tokenize("18 3.14 'hello'")
        self.assertEqual(tokens[0].value, 18)
        self.assertEqual(tokens[1].value, 3.14)
        self.assertEqual(tokens[2].value, "hello")

    def test_negative_number(self):
        tokens = tokenize("(-5)")
        self.assertEqual(tokens[0].value, "(")
        self.assertEqual(tokens[1].value, -5)

    def test_operators(self):
        tokens = tokenize("a<=1 and b>=2 and c<>3")
        ops = [t.value for t in tokens if t.type == "OPERATOR"]
        self.assertEqual(ops, ["<=", ">=", "<>"])

    def test_comment_skipped(self):
        tokens = tokenize("-- 注释\nSELECT 1;")
        values = [t.value for t in tokens]
        self.assertIn("SELECT", values)
        self.assertNotIn("注释", values)


class TestParser(unittest.TestCase):
    def test_create_table(self):
        stmt = Parser("CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20) NOT NULL);").parse()
        self.assertIsInstance(stmt, CreateTableStmt)
        self.assertEqual(stmt.table_name, "t")
        self.assertEqual(len(stmt.columns), 2)
        self.assertTrue(stmt.columns[0].primary_key)
        self.assertEqual(stmt.columns[1].type, "VARCHAR")
        self.assertEqual(stmt.columns[1].length, 20)
        self.assertTrue(stmt.columns[1].not_null)

    def test_insert_multiple_rows(self):
        stmt = Parser("INSERT INTO t (id, name) VALUES (1, 'a'), (2, 'b');").parse()
        self.assertIsInstance(stmt, InsertStmt)
        self.assertEqual(stmt.columns, ["id", "name"])
        self.assertEqual(len(stmt.rows), 2)
        self.assertEqual(stmt.rows[0][0].value, 1)
        self.assertEqual(stmt.rows[1][1].value, "b")

    def test_select_with_where_order_limit(self):
        stmt = Parser("SELECT id, name FROM t WHERE score >= 60.0 ORDER BY id DESC LIMIT 5;").parse()
        self.assertIsInstance(stmt, SelectStmt)
        self.assertEqual(len(stmt.items), 2)
        self.assertIsInstance(stmt.where, BinaryOp)
        self.assertEqual(stmt.where.op, ">=")
        self.assertEqual(stmt.limit, 5)
        self.assertTrue(stmt.order_by[0].descending)

    def test_update_and_delete(self):
        update = Parser("UPDATE t SET name = 'x', score = 1 WHERE id = 1;").parse()
        self.assertIsInstance(update, UpdateStmt)
        self.assertEqual([name for name, _ in update.assignments], ["name", "score"])

        delete = Parser("DELETE FROM t WHERE id = 1;").parse()
        self.assertIsInstance(delete, DeleteStmt)
        self.assertIsInstance(delete.where, BinaryOp)

    def test_expression_precedence(self):
        stmt = Parser("SELECT * FROM t WHERE score > 1 + 2 * 3;").parse()
        where = stmt.where
        self.assertEqual(where.op, ">")
        self.assertEqual(where.right.op, "+")
        self.assertEqual(where.right.right.op, "*")

    def test_syntax_error(self):
        with self.assertRaises(ParseError):
            Parser("SELECT FROM t;").parse()
        with self.assertRaises(ParseError):
            Parser("CREATE TABLE t (id INT;)").parse()


class TestSemantic(unittest.TestCase):
    def setUp(self):
        self.catalog = make_catalog()
        self.analyzer = SemanticAnalyzer(self.catalog)

    def _parse(self, sql):
        return Parser(sql).parse()

    def test_table_not_exists(self):
        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("SELECT * FROM no_such_table;"))

    def test_column_not_exists(self):
        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("SELECT age FROM student;"))
        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("SELECT * FROM student WHERE age > 1;"))

    def test_duplicate_table(self):
        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("CREATE TABLE student (id INT);"))

    def test_duplicate_column(self):
        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("CREATE TABLE t (id INT, id TEXT);"))

    def test_column_count_mismatch(self):
        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("INSERT INTO student VALUES (1, 'a');"))

    def test_type_check_and_coercion(self):
        stmt = self._parse("INSERT INTO student VALUES (1, 'tom', 88.5);")
        self.analyzer.analyze(stmt)
        self.assertEqual(stmt.rows[0][0].value, 1)
        self.assertEqual(stmt.rows[0][2].value, 88.5)

        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("INSERT INTO student VALUES (1, 'tom', 'abc');"))

    def test_null_in_primary_key(self):
        with self.assertRaises(SemanticError):
            self.analyzer.analyze(self._parse("INSERT INTO student VALUES (NULL, 'tom', 60);"))


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.catalog = make_catalog()
        self.analyzer = SemanticAnalyzer(self.catalog)
        self.planner = Planner(self.catalog)

    def _plan(self, sql):
        stmt = Parser(sql).parse()
        self.analyzer.analyze(stmt)
        return self.planner.build(stmt)

    def test_create_plan(self):
        plan = self._plan("CREATE TABLE course (cid INT PRIMARY KEY, title TEXT);")
        self.assertEqual(plan.schema.name, "course")
        self.assertEqual(plan.schema.column_names, ["cid", "title"])

    def test_select_plan_shape(self):
        plan = self._plan("SELECT id FROM student WHERE score > 60 ORDER BY id LIMIT 3;")
        # Limit -> Project -> Sort -> Filter -> SeqScan
        self.assertIsInstance(plan, LimitPlan)
        self.assertIsInstance(plan.child, ProjectPlan)
        self.assertIsInstance(plan.child.child, SortPlan)
        self.assertIsInstance(plan.child.child.child, FilterPlan)
        self.assertIsInstance(plan.child.child.child.child, SeqScanPlan)

    def test_insert_plan(self):
        plan = self._plan("INSERT INTO student (id, name) VALUES (1, 'a');")
        self.assertIsInstance(plan, InsertPlan)
        self.assertEqual(plan.columns, ["id", "name"])
        self.assertEqual(len(plan.rows), 1)

    def test_explain_output(self):
        plan = self._plan("SELECT name FROM student WHERE id = 1;")
        text = plan.describe()
        self.assertIn("Project", text)
        self.assertIn("Filter", text)
        self.assertIn("SeqScan", text)


if __name__ == "__main__":
    unittest.main()
