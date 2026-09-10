# -*- coding: utf-8 -*-
"""数据库系统端到端测试：建表、增删改查、持久化。"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.cli.main import MiniDB  # noqa: E402
from database_system.engine.executor import ExecutionError  # noqa: E402
from database_system.sql_compiler.lexer import LexError  # noqa: E402
from database_system.sql_compiler.parser import ParseError  # noqa: E402
from database_system.sql_compiler.semantic import SemanticError  # noqa: E402

SQL_ERRORS = (LexError, ParseError, SemanticError, ExecutionError)


class MiniDBTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = MiniDB(self.dir, pool_size=8)

    def tearDown(self):
        self.db.engine.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_sql(self, sql):
        """执行单条 SQL 并返回结果。"""
        results = self.db.execute(sql)
        self.assertEqual(len(results), 1)
        return results[0]

    def create_student(self):
        self.run_sql("CREATE TABLE student ("
                     "id INT PRIMARY KEY, name TEXT, score FLOAT, pass BOOL);")
        self.run_sql("INSERT INTO student VALUES "
                     "(1, 'alice', 90.5, TRUE), "
                     "(2, 'bob', 58.0, FALSE), "
                     "(3, 'carol', 75.0, TRUE);")


class TestDDL(MiniDBTestCase):
    def test_create_and_show(self):
        result = self.run_sql("CREATE TABLE t (id INT PRIMARY KEY, name TEXT);")
        self.assertIn("创建成功", result.message)
        self.assertIn("t", self.db.engine.list_tables())

        tables = self.run_sql("SHOW TABLES;")
        self.assertIn(["t"], tables.rows)

    def test_duplicate_table(self):
        self.run_sql("CREATE TABLE t (id INT);")
        with self.assertRaises(SemanticError):
            self.run_sql("CREATE TABLE t (id INT);")

    def test_if_not_exists(self):
        self.run_sql("CREATE TABLE t (id INT);")
        result = self.run_sql("CREATE TABLE IF NOT EXISTS t (id INT);")
        self.assertIn("跳过", result.message)

    def test_desc(self):
        self.run_sql("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL);")
        result = self.run_sql("DESC t;")
        self.assertEqual(result.columns, ["field", "type", "null", "key"])
        self.assertEqual(result.rows[0], ["id", "INT", "NO", "PRI"])
        self.assertEqual(result.rows[1][3], "")

    def test_drop_table(self):
        self.run_sql("CREATE TABLE t (id INT);")
        self.run_sql("DROP TABLE t;")
        self.assertNotIn("t", self.db.engine.list_tables())
        with self.assertRaises(SemanticError):
            self.run_sql("SELECT * FROM t;")


class TestDML(MiniDBTestCase):
    def setUp(self):
        super().setUp()
        self.create_student()

    def test_insert_and_select_all(self):
        result = self.run_sql("SELECT * FROM student;")
        self.assertEqual(result.columns, ["id", "name", "score", "pass"])
        self.assertEqual(result.rowcount, 3)
        names = [row[1] for row in result.rows]
        self.assertEqual(sorted(names), ["alice", "bob", "carol"])

    def test_select_columns_and_filter(self):
        result = self.run_sql("SELECT name, score FROM student WHERE score >= 60;")
        self.assertEqual(result.columns, ["name", "score"])
        self.assertEqual(result.rowcount, 2)
        self.assertEqual(sorted(row[0] for row in result.rows), ["alice", "carol"])

    def test_select_with_and_or(self):
        result = self.run_sql(
            "SELECT id FROM student WHERE score >= 60 AND pass = TRUE;")
        self.assertEqual([row[0] for row in result.rows], [1, 3])

    def test_order_by_and_limit(self):
        result = self.run_sql("SELECT name, score FROM student ORDER BY score DESC LIMIT 1;")
        self.assertEqual(result.rows[0][0], "alice")

    def test_insert_specified_columns(self):
        self.run_sql("INSERT INTO student (id, name, score) VALUES (4, 'dave', 66.0);")
        result = self.run_sql("SELECT name, pass FROM student WHERE id = 4;")
        self.assertEqual(result.rows[0], ["dave", None])

    def test_primary_key_conflict(self):
        with self.assertRaises(ExecutionError):
            self.run_sql("INSERT INTO student VALUES (1, 'dup', 1.0, TRUE);")

    def test_update(self):
        result = self.run_sql("UPDATE student SET score = 100.0 WHERE name = 'bob';")
        self.assertIn("更新 1 行", result.message)
        result = self.run_sql("SELECT score FROM student WHERE id = 2;")
        self.assertEqual(result.rows[0][0], 100.0)

    def test_delete(self):
        result = self.run_sql("DELETE FROM student WHERE score < 60;")
        self.assertIn("删除 1 行", result.message)
        result = self.run_sql("SELECT * FROM student;")
        self.assertEqual(result.rowcount, 2)

    def test_delete_all(self):
        self.run_sql("DELETE FROM student;")
        self.assertEqual(self.run_sql("SELECT * FROM student;").rowcount, 0)

    def test_null_semantics(self):
        self.run_sql("INSERT INTO student (id, name) VALUES (5, 'evan');")
        self.assertEqual(self.run_sql("SELECT id FROM student WHERE score IS NULL;").rows,
                         [[5]])
        self.assertEqual(self.run_sql("SELECT id FROM student WHERE score IS NOT NULL;").rowcount, 3)

    def test_like_and_in(self):
        self.assertEqual(self.run_sql("SELECT id FROM student WHERE name LIKE 'a%';").rows, [[1]])
        self.assertEqual(
            sorted(row[0] for row in self.run_sql("SELECT id FROM student WHERE id IN (1, 3);").rows),
            [1, 3])

    def test_expression_projection(self):
        result = self.run_sql("SELECT name, score + 5 FROM student WHERE id = 2;")
        self.assertEqual(result.rows[0][1], 63.0)


class TestPersistence(MiniDBTestCase):
    def test_data_survives_restart(self):
        self.create_student()
        self.db.engine.close()                  # 关闭前刷盘

        reopened = MiniDB(self.dir, pool_size=8)
        try:
            result = reopened.execute("SELECT name FROM student ORDER BY id;")[0]
            self.assertEqual([row[0] for row in result.rows], ["alice", "bob", "carol"])
            schema = reopened.engine.get_schema("student")
            self.assertEqual(schema.column_names, ["id", "name", "score", "pass"])
        finally:
            reopened.engine.close()

    def test_ddl_survives_restart(self):
        self.run_sql("CREATE TABLE t (id INT PRIMARY KEY, v TEXT);")
        self.db.engine.close()

        reopened = MiniDB(self.dir, pool_size=8)
        try:
            self.assertIn("t", reopened.engine.list_tables())
        finally:
            reopened.engine.close()


class TestBufferAndStorage(MiniDBTestCase):
    def test_multi_page_storage(self):
        self.run_sql("CREATE TABLE big (id INT PRIMARY KEY, blob TEXT);")
        for i in range(200):
            self.run_sql("INSERT INTO big VALUES (%d, '%s');" % (i, "x" * 80))
        result = self.run_sql("SELECT id FROM big;")
        self.assertEqual(result.rowcount, 200)

        stats = self.db.engine.stats()
        self.assertGreater(stats["disk_reads"], 0)
        self.assertGreater(stats["pool_size"], 0)

    def test_buffer_hit_rate(self):
        self.create_student()
        self.db.engine.buffer.reset_stats()
        self.run_sql("SELECT * FROM student;")
        self.run_sql("SELECT * FROM student;")
        stats = self.db.engine.stats()
        self.assertGreater(stats["hits"], 0)
        self.assertGreater(stats["hit_rate"], 0.0)


class TestErrors(MiniDBTestCase):
    def test_unknown_table(self):
        with self.assertRaises(SemanticError):
            self.run_sql("SELECT * FROM nope;")

    def test_syntax_error(self):
        with self.assertRaises(ParseError):
            self.run_sql("SELCT * FROM t;")

    def test_division_by_zero(self):
        self.run_sql("CREATE TABLE t (id INT, v INT);")
        self.run_sql("INSERT INTO t VALUES (1, 0);")
        with self.assertRaises(ExecutionError):
            self.run_sql("SELECT id / v FROM t;")

    def test_reserved_catalog_name(self):
        with self.assertRaises(Exception):
            self.run_sql("CREATE TABLE __catalog (id INT);")


if __name__ == "__main__":
    unittest.main()
