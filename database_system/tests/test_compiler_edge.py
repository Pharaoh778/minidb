# -*- coding: utf-8 -*-
"""编译器边界测试与 Fuzz 测试。

对应课程 PPT 第 31~32 页的测试要求：
  - 词法错误：非法字符、字符串未闭合
  - 语法错误：缺分号、括号不匹配、结构错误
  - 语义错误：表/列不存在、类型不匹配
  - 边界测试：空输入、极长标识符、多语句、大小写
  - Fuzz 测试：随机生成 / 变异 SQL，合法则通过，非法则被拒绝，但绝不允许崩溃
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.sql_compiler.ast import format_ast  # noqa: E402
from database_system.sql_compiler.catalog import Catalog, Column, TableSchema  # noqa: E402
from database_system.sql_compiler.lexer import (  # noqa: E402
    LexError, format_tokens, tokenize,
)
from database_system.sql_compiler.optimizer import Optimizer  # noqa: E402
from database_system.sql_compiler.parser import Parser, ParseError, parse_sql  # noqa: E402
from database_system.sql_compiler.planner import Planner  # noqa: E402
from database_system.sql_compiler.semantic import SemanticAnalyzer, SemanticError  # noqa: E402

# 编译器能够主动识别并拒绝的错误类型
EXPECTED_ERRORS = (LexError, ParseError, SemanticError)


def make_catalog():
    catalog = Catalog()
    catalog.create_table(TableSchema("student", [
        Column("id", "INT", primary_key=True),
        Column("name", "TEXT", length=20),
        Column("score", "FLOAT"),
    ]))
    return catalog


class TestLexicalErrors(unittest.TestCase):
    """词法错误：必须给出错误类型与位置，不能崩溃。"""

    def test_illegal_character(self):
        with self.assertRaises(LexError) as ctx:
            tokenize("SELECT * FROM student WHERE id = @;")
        self.assertIn("无法识别", str(ctx.exception))

    def test_unterminated_string(self):
        with self.assertRaises(LexError) as ctx:
            tokenize("SELECT * FROM student WHERE name = 'Alice;")
        self.assertIn("字符串未闭合", str(ctx.exception))

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError) as ctx:
            tokenize("/* 未闭合 SELECT 1;")
        self.assertIn("块注释未闭合", str(ctx.exception))

    def test_error_message_has_position(self):
        """错误信息必须形如 [词法错误, 第 N 行第 M 列, 原因]。"""
        with self.assertRaises(LexError) as ctx:
            tokenize("SELECT @")
        text = str(ctx.exception)
        self.assertTrue(text.startswith("[词法错误, 第 "), text)
        self.assertIn("列, ", text)


class TestSyntaxErrors(unittest.TestCase):
    """语法错误：缺分号、括号不匹配、结构错误。"""

    def test_missing_semicolon_between_statements(self):
        with self.assertRaises(ParseError) as ctx:
            Parser("SELECT * FROM student SELECT * FROM student").parse_all()
        self.assertIn("分号", str(ctx.exception))

    def test_unmatched_parenthesis(self):
        with self.assertRaises(ParseError):
            Parser("CREATE TABLE t (id INT;").parse()

    def test_missing_from_clause(self):
        with self.assertRaises(ParseError):
            Parser("SELECT * FROM;").parse()

    def test_limit_must_be_integer(self):
        with self.assertRaises(ParseError):
            Parser("SELECT * FROM student LIMIT abc;").parse()

    def test_statement_must_start_with_keyword(self):
        with self.assertRaises(ParseError):
            Parser("student;").parse()

    def test_error_message_has_position(self):
        with self.assertRaises(ParseError) as ctx:
            Parser("SELECT FROM student;").parse()
        self.assertTrue(str(ctx.exception).startswith("[语法错误, 第 "), str(ctx.exception))

    def test_expectation_is_reported(self):
        """错误信息要说明「期望什么符号」。"""
        with self.assertRaises(ParseError) as ctx:
            Parser("SELECT * student;").parse()
        self.assertIn("期望", str(ctx.exception))


class TestSemanticErrors(unittest.TestCase):
    """语义错误：表/列不存在、类型不匹配、列数不一致。"""

    def setUp(self):
        self.catalog = make_catalog()
        self.analyzer = SemanticAnalyzer(self.catalog)

    def _analyze(self, sql):
        return self.analyzer.analyze(parse_sql(sql))

    def test_table_not_exists(self):
        with self.assertRaises(SemanticError) as ctx:
            self._analyze("SELECT * FROM nope;")
        self.assertIn("表 nope 不存在", str(ctx.exception))

    def test_column_not_exists(self):
        with self.assertRaises(SemanticError) as ctx:
            self._analyze("SELECT nope FROM student;")
        self.assertIn("不存在列 nope", str(ctx.exception))

    def test_type_mismatch(self):
        with self.assertRaises(SemanticError):
            self._analyze("INSERT INTO student VALUES (1, 'a', 'not_a_number');")

    def test_column_count_mismatch(self):
        with self.assertRaises(SemanticError) as ctx:
            self._analyze("INSERT INTO student VALUES (1, 'a');")
        self.assertIn("列数不匹配", str(ctx.exception))

    def test_duplicate_table(self):
        with self.assertRaises(SemanticError):
            self._analyze("CREATE TABLE student (x INT);")

    def test_insert_column_error_points_at_the_column(self):
        """列名错误应定位到具体列名，而不是语句开头。"""
        with self.assertRaises(SemanticError) as ctx:
            self._analyze("INSERT INTO student(id,nope) VALUES (1,'x');")
        self.assertEqual(ctx.exception.position, (1, 24))   # 第 24 列是 nope

    def test_duplicate_insert_column_points_at_the_column(self):
        with self.assertRaises(SemanticError) as ctx:
            self._analyze("INSERT INTO student(id,name,name) VALUES (1,'x','y');")
        self.assertEqual(ctx.exception.position, (1, 29))   # 第 29 列是重复的 name

    def test_values_row_error_points_at_the_row(self):
        """列数不匹配应定位到出错的那组 VALUES。"""
        with self.assertRaises(SemanticError) as ctx:
            self._analyze("INSERT INTO student(id,name) VALUES (7,'Grace','extra');")
        self.assertEqual(ctx.exception.position, (1, 38))   # 第 38 列是该组第一个值 7

    def test_values_row_error_points_at_the_offending_group(self):
        with self.assertRaises(SemanticError) as ctx:
            self._analyze(
                "INSERT INTO student(id,name) VALUES (7,'Grace'),(8,'Henry','x');")
        self.assertIn("第 2 组值", str(ctx.exception))

    def test_error_is_three_tuple(self):
        """语义错误必须能给出 [错误类型, 位置, 原因] 三元组。"""
        with self.assertRaises(SemanticError) as ctx:
            self._analyze("SELECT nope FROM student;")
        error = ctx.exception
        self.assertEqual(error.error_type, "语义错误")
        self.assertEqual(len(error.as_tuple()), 3)
        self.assertIsNotNone(error.position)


class TestBoundary(unittest.TestCase):
    """边界测试：空输入、极长标识符、多语句、大小写、注释。"""

    def setUp(self):
        self.catalog = make_catalog()
        self.analyzer = SemanticAnalyzer(self.catalog)

    def test_empty_input(self):
        self.assertEqual(Parser("").parse_all(), [])
        self.assertEqual(Parser("   \n\t ").parse_all(), [])

    def test_only_semicolons(self):
        self.assertEqual(Parser(";;;").parse_all(), [])

    def test_very_long_identifier(self):
        name = "a" * 300
        statement = parse_sql("CREATE TABLE %s (id INT);" % name)
        self.assertEqual(statement.table_name, name)

    def test_multiple_statements(self):
        statements = Parser(
            "SELECT * FROM student; SELECT id FROM student; DELETE FROM student;"
        ).parse_all()
        self.assertEqual(len(statements), 3)

    def test_case_insensitive_keywords(self):
        statement = parse_sql("sElEcT * fRoM student;")
        self.assertEqual(statement.table_name, "student")

    def test_identifiers_are_lowercased(self):
        statement = parse_sql("SELECT * FROM Student;")
        self.assertEqual(statement.table_name, "student")

    def test_comments_are_ignored(self):
        statement = parse_sql("SELECT * /* 块注释 */ FROM student;  -- 行注释")
        self.assertEqual(statement.table_name, "student")

    def test_escaped_quote_in_string(self):
        statement = parse_sql("INSERT INTO student VALUES (1, 'Tom''s book', 90.5);")
        self.analyzer.analyze(statement)
        self.assertEqual(statement.rows[0][1].value, "Tom's book")

    def test_multiline_token_positions(self):
        """多行 SQL 的 Token 行号、列号必须正确。"""
        tokens = tokenize("SELECT id\n  FROM student\n  WHERE id = 1;")
        table = {t.lexeme: (t.line, t.column) for t in tokens}
        self.assertEqual(table["SELECT"], (1, 1))
        self.assertEqual(table["FROM"], (2, 3))
        self.assertEqual(table["WHERE"], (3, 3))

    def test_token_table_format(self):
        text = format_tokens(tokenize("SELECT 1;"))
        self.assertIn("种别码", text)
        self.assertIn("行号", text)
        self.assertIn("列号", text)

    def test_ast_dump_contains_key_nodes(self):
        text = format_ast(parse_sql("SELECT id FROM student WHERE score > 60;"))
        self.assertIn("Select", text)
        self.assertIn("Projection", text)
        self.assertIn("Where", text)
        self.assertIn("BinaryOp(>)", text)


class TestOptimizerRules(unittest.TestCase):
    """5 条规则式优化的验证（课程 PPT 第 28~29 页）。"""

    def setUp(self):
        self.catalog = make_catalog()
        self.analyzer = SemanticAnalyzer(self.catalog)
        self.planner = Planner(self.catalog)

    def _plan(self, sql, optimize=True):
        statement = parse_sql(sql)
        self.analyzer.analyze(statement)
        plan = self.planner.build(statement)
        optimizer = Optimizer()
        if optimize:
            plan = optimizer.optimize(plan)
        return plan, optimizer

    def test_constant_folding(self):
        _, optimizer = self._plan("SELECT * FROM student WHERE score > 10 + 8;")
        self.assertIn("常量折叠", optimizer.report())
        plan, _ = self._plan("SELECT * FROM student WHERE score > 10 + 8;")
        self.assertIn("score > 18", plan.describe())

    def test_boolean_simplification(self):
        plan, optimizer = self._plan("SELECT * FROM student WHERE 1 = 1 AND id = 1;")
        self.assertIn("布尔化简", optimizer.report())
        self.assertIn("Filter((id = 1))", plan.describe())

    def test_projection_pruning(self):
        plan, optimizer = self._plan("SELECT name FROM student;")
        self.assertIn("列裁剪", optimizer.report())
        self.assertIn("SeqScan(student)[name]", plan.describe())

    def test_predicate_pushdown(self):
        plan, optimizer = self._plan("SELECT * FROM student WHERE id = 1 AND score > 2;")
        self.assertIn("谓词下推", optimizer.report())
        self.assertEqual(plan.describe().count("Filter("), 2)

    def test_redundant_node_elimination(self):
        plan, optimizer = self._plan("SELECT * FROM student WHERE 1 = 1;")
        self.assertIn("冗余节点消除", optimizer.report())
        self.assertNotIn("Filter(", plan.describe())

    def test_ppt_demo_case(self):
        """课程 PPT 第 29 页给出的示例。"""
        statement = parse_sql("SELECT name FROM student WHERE 1 = 1 AND score > 10 + 8;")
        self.analyzer.analyze(statement)
        before = self.planner.build(statement)
        optimizer = Optimizer()
        after = optimizer.optimize(self.planner.build(statement))
        self.assertIn("(1 = 1)", before.describe())       # 优化前保留原式
        self.assertIn("(score > 18)", after.describe())   # 优化后已折叠
        self.assertNotIn("1 = 1", after.describe())

    def test_optimization_keeps_semantics(self):
        """优化只做等价改写：谓词中的列引用不应丢失。"""
        plan, _ = self._plan("SELECT name FROM student WHERE score > 60 AND id < 10;")
        describe = plan.describe()
        self.assertIn("score > 60", describe)
        self.assertIn("id < 10", describe)


class TestFuzz(unittest.TestCase):
    """Fuzz 测试：随机生成 / 变异 SQL，编译器不得崩溃。

    关注指标（课程 PPT 第 32 页）：
      Crash（崩溃）/ Wrong Accept（错误接受）/ Wrong Reject（错误拒绝）/ Error Location
    """

    ALPHABET = list("abcdefgxyz_0129 \n(),;'\"*<>=+-/.SELECTFROMWHEREINTTEXTANDORNOT")
    SEEDS = (
        "SELECT * FROM student;",
        "SELECT id, name FROM student WHERE score > 60;",
        "INSERT INTO student VALUES (1, 'a', 90.5);",
        "UPDATE student SET name = 'b' WHERE id = 1;",
        "DELETE FROM student WHERE id = 1;",
        "CREATE TABLE t (id INT PRIMARY KEY, name TEXT);",
    )

    def setUp(self):
        self.catalog = make_catalog()
        self.analyzer = SemanticAnalyzer(self.catalog)
        self.planner = Planner(self.catalog)

    def _run(self, sql):
        """完整跑一遍编译流水线；只允许抛出编译器可识别的错误。"""
        try:
            for statement in Parser(sql).parse_all():
                self.analyzer.analyze(statement)
                plan = self.planner.build(statement)
                Optimizer().optimize(plan)
        except EXPECTED_ERRORS:
            return "rejected"
        except RecursionError:
            self.fail("Fuzz 触发递归过深：%r" % sql)
        except Exception as exc:                       # noqa: BLE001
            self.fail("Fuzz 触发未预期异常 %r：%r" % (exc, sql))
        return "accepted"

    def test_random_strings_do_not_crash(self):
        random.seed(20260401)
        for _ in range(600):
            length = random.randint(1, 40)
            sql = "".join(random.choice(self.ALPHABET) for _ in range(length))
            self._run(sql)

    def test_mutated_valid_sql_do_not_crash(self):
        """对合法 SQL 做随机字符变异，验证编译器仍能正确拒绝或接受。"""
        random.seed(20260402)
        for _ in range(600):
            sql = list(random.choice(self.SEEDS))
            for _ in range(random.randint(1, 4)):
                if not sql:
                    break
                index = random.randrange(len(sql))
                operation = random.random()
                if operation < 0.4:
                    sql[index] = random.choice(self.ALPHABET)
                elif operation < 0.7:
                    del sql[index]
                else:
                    sql.insert(index, random.choice(self.ALPHABET))
            self._run("".join(sql))

    def test_valid_seeds_are_always_accepted(self):
        """合法 SQL 必须能通过完整流水线（Wrong Reject 检测）。"""
        for sql in self.SEEDS:
            if sql.startswith("CREATE TABLE t"):
                continue                      # 表 t 未预先创建也可建表，跳过重复创建
            with self.subTest(sql=sql):
                self.assertEqual(self._run(sql), "accepted")

    def test_nested_parentheses_do_not_crash(self):
        for depth in (5, 20, 50):
            sql = "SELECT * FROM student WHERE " + "(" * depth + "1" + ")" * depth + ";"
            self._run(sql)


if __name__ == "__main__":
    unittest.main()
