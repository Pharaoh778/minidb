#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键执行 SQL 脚本的启动器（默认执行 test_data.sql）。

用法：
    python run_test_data.py                          # 执行 test_data.sql，数据写入 data/
    python run_test_data.py test_crud.sql -d demo_data  # 增删改查全流程演示
    python run_test_data.py other.sql -d demo_data   # 执行任意脚本，写入指定目录
    python run_test_data.py --clean                  # 先清空数据目录，保证完全干净
    python run_test_data.py -s FIFO -p 8             # 指定缓冲池策略与页数

Windows 双击运行：run_test_data.bat（已处理 UTF-8 代码页与编码）。

退出码：0 表示全部语句成功且行数校验通过；非 0 表示存在失败或数据不符。
"""

import argparse
import logging
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Windows 控制台默认 GBK，中文与表格边框会乱码：强制按 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from database_system.cli.main import MiniDB            # noqa: E402
from database_system.utils.constants import (           # noqa: E402
    DEFAULT_DATA_DIR, DEFAULT_POOL_SIZE, DEFAULT_STRATEGY,
)
from database_system.utils.helpers import format_table, get_logger  # noqa: E402

DEFAULT_SCRIPT = os.path.join(HERE, "test_data.sql")

# 按脚本文件名登记的期望行数；未登记的脚本自动跳过校验
EXPECTED_ROWS = {
    "test_data.sql": [
        ("student", 12),
        ("course", 6),
        ("employee", 10),
        ("crud_lab", 8),
        ("type_probe", 4),
        ("seq_num", 30),
    ],
    # CRUD 演示跑完后，8 行经 4 次 UPDATE、3 次 DELETE 应剩 2 行（id=3、6）
    "test_crud.sql": [
        ("crud_demo", 2),
    ],
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="执行 SQL 脚本并校验导入结果（默认 test_data.sql）")
    parser.add_argument("script", nargs="?", default=DEFAULT_SCRIPT,
                        help="SQL 脚本路径（默认 test_data.sql）")
    parser.add_argument("-d", "--data-dir", default=DEFAULT_DATA_DIR,
                        help="数据目录（默认 %s；相对路径锚定项目根目录，"
                             "绝对路径按原样使用）" % DEFAULT_DATA_DIR)
    parser.add_argument("-p", "--pool-size", type=int, default=DEFAULT_POOL_SIZE,
                        help="缓冲池页数（默认 %d）" % DEFAULT_POOL_SIZE)
    parser.add_argument("-s", "--strategy", default=DEFAULT_STRATEGY,
                        choices=["LRU", "FIFO"], help="缓存淘汰策略（默认 LRU）")
    parser.add_argument("--clean", action="store_true",
                        help="执行前清空数据目录（彻底重来，会删除已有数据）")
    parser.add_argument("--no-check", action="store_true",
                        help="跳过导入后的行数校验")
    parser.add_argument("--no-optimize", action="store_true",
                        help="关闭规则式查询优化（默认开启）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出调试日志")
    return parser.parse_args(argv)


def check_rows(db, expected):
    """逐表对比实际行数与期望行数，返回不一致的表的个数。"""
    rows = []
    mismatched = 0
    for table, want in expected:
        got = db.engine.row_count(table)
        matched = (got == want)
        if not matched:
            mismatched += 1
        rows.append([table, str(want), str(got), "一致" if matched else "不一致"])
    print(format_table(["表名", "期望行数", "实际行数", "结果"], rows))
    return mismatched


def main(argv=None):
    args = parse_args(argv)

    script_path = os.path.abspath(args.script)
    data_dir = os.path.abspath(args.data_dir)

    if not os.path.isfile(script_path):
        print("错误：找不到 SQL 脚本：%s" % script_path)
        return 2

    if args.clean and os.path.isdir(data_dir):
        shutil.rmtree(data_dir)
        print("已清空数据目录：%s" % data_dir)

    print("=" * 60)
    print("MiniDB 脚本执行器")
    print("  脚本文件：%s" % script_path)
    print("  数据目录：%s" % data_dir)
    print("  缓冲池  ：%d 页 / %s" % (args.pool_size, args.strategy))
    print("=" * 60)

    with open(script_path, "r", encoding="utf-8") as fp:
        text = fp.read()

    logger = get_logger("minidb", logging.DEBUG if args.verbose else logging.INFO)
    db = MiniDB(data_dir=data_dir, pool_size=args.pool_size,
                strategy=args.strategy, logger=logger, verbose=args.verbose,
                optimize=not args.no_optimize)

    orphans = db.engine.orphan_files()
    if orphans:
        print("警告：以下数据文件在系统目录中无对应表定义，已自动忽略：%s"
              % "、".join(orphans))

    ok, fail = db.run_script(text)

    expected = EXPECTED_ROWS.get(os.path.basename(script_path))
    should_check = (not args.no_check and expected is not None)
    if should_check:
        print("-" * 60)
        mismatched = check_rows(db, expected)

    stats = db.engine.stats()
    print("-" * 60)
    print("缓冲池：命中 %d / 未命中 %d，命中率 %.1f%%（磁盘读 %d，磁盘写 %d）"
          % (stats["hits"], stats["misses"], stats["hit_rate"] * 100,
             stats["disk_reads"], stats["disk_writes"]))

    db.engine.close()

    print("=" * 60)
    print("脚本执行完成：成功 %d 条，失败 %d 条" % (ok, fail))
    if should_check and mismatched == 0:
        print("行数校验：全部一致")
    print("=" * 60)

    if fail or mismatched:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
