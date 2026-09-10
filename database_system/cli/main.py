# -*- coding: utf-8 -*-
"""主程序入口：MiniDB 命令行客户端。"""

import argparse
import os
import sys

# 支持两种启动方式：
#   python -m database_system.cli.main          （推荐，模块方式）
#   python database_system/cli/main.py          （直接以脚本方式运行）
# 以脚本方式运行时本文件不属于任何包，需要手动把项目根目录加入搜索路径，
# 因此下面的业务导入统一使用绝对导入。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.engine.executor import Executor, ExecutionError, Result  # noqa: E402
from database_system.engine.storage_engine import StorageEngine, StorageError  # noqa: E402
from database_system.sql_compiler.ast import format_ast  # noqa: E402
from database_system.sql_compiler.lexer import (  # noqa: E402
    LexError, format_tokens, tokenize,
)
from database_system.sql_compiler.optimizer import Optimizer  # noqa: E402
from database_system.sql_compiler.parser import ParseError, Parser  # noqa: E402
from database_system.sql_compiler.planner import Planner  # noqa: E402
from database_system.sql_compiler.semantic import (  # noqa: E402
    SemanticAnalyzer, SemanticError,
)
from database_system.utils.constants import (  # noqa: E402
    DEFAULT_DATA_DIR, DEFAULT_POOL_SIZE, DEFAULT_STRATEGY,
)
from database_system.utils.helpers import format_table, get_logger  # noqa: E402

BANNER = r"""
 __  __ _       _ ____  ____
|  \/  (_)_ __ (_)  _ \| __ )
| |\/| | | '_ \| | | | |  _ \
| |  | | | | | | | |_| | |_) |
|_|  |_|_|_| |_|_|____/|____/
         MiniDB —— 简化版数据库系统
"""

HELP_TEXT = """支持的操作：
  CREATE TABLE t (c1 INT PRIMARY KEY, c2 TEXT, c3 FLOAT);
  DROP TABLE [IF EXISTS] t;
  SHOW TABLES;
  DESC t;
  INSERT INTO t [(c1, c2)] VALUES (1, 'a'), (2, 'b');
  SELECT c1, c2 FROM t WHERE c1 > 1 [ORDER BY c1 DESC] [LIMIT 10];
  UPDATE t SET c2 = 'x' WHERE c1 = 1;
  DELETE FROM t WHERE c1 = 1;
  EXPLAIN SELECT * FROM t;

内置命令：
  .help            显示帮助
  .tables          列出所有表
  .stats           显示缓冲池统计
  .flush           把脏页刷回磁盘
  .tokens <SQL>    打印词法分析的 Token 四元式 [种别码, 词素值, 行号, 列号]
  .ast <SQL>       打印语法分析得到的抽象语法树 AST
  .explain <SQL>   对比展示优化前 / 优化后的执行计划
  .exit            退出（quit / exit 亦可）

说明：一条语句以分号结尾；未输入分号时会进入续行模式。
"""

BUILTIN_EXIT = {".exit", ".quit", "exit", "quit", "\\q"}

# 可预期、直接提示即可的错误
KNOWN_ERRORS = (LexError, ParseError, SemanticError, ExecutionError, StorageError, OSError)


def report_error(exc, verbose=False):
    """统一错误输出：预期错误给出提示，未预期错误提示用 -v 看堆栈。"""
    if isinstance(exc, KNOWN_ERRORS):
        print("Error: %s" % exc)
        return
    print("Error: 发生未预期的错误：%r" % (exc,))
    if not verbose:
        print("提示：加 -v 参数可查看完整堆栈。")


class MiniDB:
    """把编译器与引擎串起来的数据库实例。"""

    def __init__(self, data_dir=DEFAULT_DATA_DIR, pool_size=DEFAULT_POOL_SIZE,
                 strategy=DEFAULT_STRATEGY, logger=None, verbose=False, optimize=True):
        self.engine = StorageEngine(data_dir, pool_size=pool_size,
                                    strategy=strategy, logger=logger)
        self.logger = self.engine.logger
        self._verbose = verbose
        self._optimize = optimize
        self.analyzer = SemanticAnalyzer(self.engine.catalog)
        self.planner = Planner(self.engine.catalog)
        self.optimizer = Optimizer()
        self.executor = Executor(self.engine, self.logger)

    # ---------------- SQL 执行 ----------------
    def execute(self, sql_text):
        """执行一段（可含多条）SQL，返回结果列表。"""
        statements = Parser(sql_text).parse_all()
        return [self.execute_statement(stmt) for stmt in statements]

    def execute_statement(self, statement):
        self.analyzer.analyze(statement)
        if getattr(statement, "skipped", False):
            return Result(message="语句已跳过（对象已存在或不存在）")
        plan = self.planner.build(statement)
        if self._optimize:
            plan = self.optimizer.optimize(plan)
        return self.executor.execute(plan)

    def run_script(self, text):
        """执行脚本（多条 SQL），返回 (成功数, 失败数)。"""
        ok, fail = 0, 0
        for statement in Parser(text).parse_all():
            try:
                result = self.execute_statement(statement)
                self._print_result(result)
                ok += 1
            except KNOWN_ERRORS as exc:
                report_error(exc)
                fail += 1
            except Exception as exc:                 # 兜底：脚本不因单条语句中断
                report_error(exc, verbose=self._verbose)
                fail += 1
        return ok, fail

    # ---------------- 展示 ----------------
    @staticmethod
    def _print_result(result):
        if result is None:
            return
        if result.is_query:
            print(result.to_table())
            print("(%d 行)" % result.rowcount)
        elif result.message:
            print(result.message)

    def _show_tables(self):
        tables = self.engine.list_tables()
        if not tables:
            print("(空)")
            return
        print(format_table(["tables"], [[name] for name in tables]))

    def _show_stats(self):
        stats = self.engine.stats()
        rows = [[key, stats[key]] for key in
                ("strategy", "pool_size", "cached", "hits", "misses",
                 "disk_reads", "disk_writes", "evictions")]
        rows.append(["hit_rate", "%.1f%%" % (stats["hit_rate"] * 100)])
        print(format_table(["item", "value"], rows))

    # ---------------- 交互式 REPL ----------------
    def repl(self):
        print(BANNER)
        print("数据目录：%s" % os.path.abspath(self.engine.data_dir))
        print("缓冲池：%d 页 / 策略 %s" % (self.engine.buffer.pool_size, self.engine.buffer.strategy))
        print("输入 .help 查看帮助，.exit 退出。")

        buffer_sql = ""
        while True:
            try:
                prompt = "MiniDB > " if not buffer_sql else "     ... "
                line = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not buffer_sql and line.lower() in BUILTIN_EXIT:
                break

            if not buffer_sql and line.startswith("."):
                self._handle_builtin(line)
                continue

            if not line:
                continue

            buffer_sql = (buffer_sql + " " + line).strip() if buffer_sql else line
            if not buffer_sql.endswith(";") and not _is_complete(buffer_sql):
                continue

            try:
                for result in self.execute(buffer_sql):
                    self._print_result(result)
            except KNOWN_ERRORS as exc:
                report_error(exc)
            except Exception as exc:                # 兜底：REPL 不因单个语句崩溃
                report_error(exc, verbose=self._verbose)
            finally:
                buffer_sql = ""

        self.engine.close()
        print("已保存并退出，Bye!")

    def _handle_builtin(self, line):
        parts = line.split(None, 1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command in (".help", ".h", "?"):
            print(HELP_TEXT)
        elif command == ".tables":
            self._show_tables()
        elif command in (".stats", ".buffer"):
            self._show_stats()
        elif command == ".flush":
            count = self.engine.flush()
            print("已刷回 %d 个脏页" % count)
        elif command == ".tokens":
            self._show_tokens(argument)
        elif command == ".ast":
            self._show_ast(argument)
        elif command in (".explain", ".plan"):
            self._show_plan(argument)
        else:
            print("未知命令：%s（输入 .help 查看帮助）" % line)

    # ---------------- 编译过程可视化 ----------------
    @staticmethod
    def _require_sql(sql, command):
        if not sql:
            print("用法：%s <SQL 语句>" % command)
            return False
        return True

    @staticmethod
    def _show_tokens(sql):
        """展示词法分析结果：[种别码, 词素值, 行号, 列号]。"""
        if not MiniDB._require_sql(sql, ".tokens"):
            return
        try:
            print(format_tokens(tokenize(sql)))
        except LexError as exc:
            report_error(exc)

    @staticmethod
    def _show_ast(sql):
        """展示语法分析结果：抽象语法树。"""
        if not MiniDB._require_sql(sql, ".ast"):
            return
        try:
            for statement in Parser(sql).parse_all():
                print(format_ast(statement))
        except (LexError, ParseError) as exc:
            report_error(exc)

    def _show_plan(self, sql):
        """对比展示优化前后的逻辑执行计划。"""
        if not self._require_sql(sql, ".explain"):
            return
        try:
            for statement in Parser(sql).parse_all():
                self.analyzer.analyze(statement)
                before = self.planner.build(statement)
                optimizer = Optimizer()
                after = optimizer.optimize(self.planner.build(statement))
                print("优化前：")
                print(before.describe())
                print("优化后：")
                print(after.describe())
                print("生效规则：%s" % optimizer.report())
        except KNOWN_ERRORS as exc:
            report_error(exc)


def _is_complete(sql):
    """粗略判断语句是否完整（以分号结尾）。"""
    return sql.rstrip().endswith(";")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="minidb",
        description="MiniDB —— 简化版数据库管理系统（课程实习项目）",
    )
    parser.add_argument("-d", "--data-dir", default=DEFAULT_DATA_DIR,
                        help="数据目录（默认 %s）" % DEFAULT_DATA_DIR)
    parser.add_argument("-p", "--pool-size", type=int, default=DEFAULT_POOL_SIZE,
                        help="缓冲池页数（默认 %d）" % DEFAULT_POOL_SIZE)
    parser.add_argument("-s", "--strategy", default=DEFAULT_STRATEGY,
                        choices=["LRU", "FIFO"], help="缓存淘汰策略（默认 LRU）")
    parser.add_argument("-f", "--file", help="执行 SQL 脚本文件后退出")
    parser.add_argument("-e", "--execute", help="执行一条 SQL 语句后退出")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--no-optimize", action="store_true",
                        help="关闭规则式查询优化（默认开启）")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    import logging
    logger = get_logger("minidb", logging.DEBUG if args.verbose else logging.INFO)

    db = MiniDB(data_dir=args.data_dir, pool_size=args.pool_size,
                strategy=args.strategy, logger=logger, verbose=args.verbose,
                optimize=not args.no_optimize)

    orphans = db.engine.orphan_files()
    if orphans:
        print("警告：以下数据文件在系统目录中无对应表定义，已自动忽略：%s"
              % "、".join(orphans))

    if args.file:
        with open(args.file, "r", encoding="utf-8") as fp:
            ok, fail = db.run_script(fp.read())
        db.engine.close()
        print("脚本执行完成：成功 %d 条，失败 %d 条" % (ok, fail))
        return 0 if fail == 0 else 1

    if args.execute:
        if args.execute.lstrip().startswith("."):
            # 内置调试命令（.tokens / .ast / .explain）也支持 -e 方式调用，便于截图
            db._handle_builtin(args.execute.strip())
            db.engine.close()
            return 0
        try:
            for result in db.execute(args.execute):
                MiniDB._print_result(result)
        except KNOWN_ERRORS as exc:
            report_error(exc)
            db.engine.close()
            return 1
        except Exception as exc:
            report_error(exc, verbose=args.verbose)
            db.engine.close()
            return 1
        db.engine.close()
        return 0

    db.repl()
    return 0


if __name__ == "__main__":
    # 唯一入口：python -m database_system.cli.main [参数]
    sys.exit(main())
