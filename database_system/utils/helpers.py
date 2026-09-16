# -*- coding: utf-8 -*-
"""通用辅助函数。"""

import logging
import os
import re

from .constants import (
    MAX_TEXT_LENGTH,
    PROJECT_ROOT,
    TYPE_BOOL,
    TYPE_FLOAT,
    TYPE_INT,
    TYPE_TEXT,
)

_INT_RE = re.compile(r"[+-]?\d+")


def ensure_dir(path):
    """确保目录存在。"""
    os.makedirs(path, exist_ok=True)
    return path


def resolve_data_dir(data_dir):
    """把数据目录解析为绝对路径。

    相对路径锚定到项目根目录而非当前工作目录：否则在 database_system/cli
    这类子目录下启动时会静默使用一个同名但完全不同的库。绝对路径（尤其是
    测试使用的临时目录）原样返回。
    """
    path = os.fspath(data_dir)
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


def get_logger(name="minidb", level=logging.INFO):
    """获取一个全局唯一的 logger（含命中统计等运行日志输出）。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s | %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def coerce_value(type_name, value, column_name="?"):
    """把字面量转换为列类型对应的 Python 值，失败抛出 ValueError。"""
    if value is None:
        return None

    t = str(type_name).upper()
    if t == TYPE_INT:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            if not _INT_RE.fullmatch(value.strip()):
                raise ValueError("列 %s 需要 INT 类型，收到 %r" % (column_name, value))
            value = int(value)
        elif isinstance(value, float):
            if not float(value).is_integer():
                raise ValueError("列 %s 需要 INT 类型，收到 %r" % (column_name, value))
            value = int(value)
        if not (-2 ** 31 <= value < 2 ** 31):
            raise ValueError("列 %s 的 INT 值超出范围：%s" % (column_name, value))
        return value

    if t == TYPE_FLOAT:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("列 %s 需要 FLOAT 类型，收到 %r" % (column_name, value))

    if t == TYPE_BOOL:
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "y", "t"):
                return True
            if low in ("false", "0", "no", "n", "f"):
                return False
            raise ValueError("列 %s 需要 BOOL 类型，收到 %r" % (column_name, value))
        return bool(value)

    if t == TYPE_TEXT:
        text = value if isinstance(value, str) else str(value)
        if len(text.encode("utf-8")) > MAX_TEXT_LENGTH:
            raise ValueError("列 %s 的 TEXT 值过长（上限 %d 字节）" % (column_name, MAX_TEXT_LENGTH))
        return text

    raise ValueError("不支持的数据类型：%s" % type_name)


def format_value(value):
    """结果集中单个值的显示形式。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return ("%g" % value)
    return str(value)


def format_table(columns, rows, max_width=40):
    """把查询结果格式化为 ASCII 表格。"""
    columns = list(columns)
    body = [[format_value(v) for v in row] for row in rows]

    def display_width(text):
        # 中文等宽字符按 2 列计算
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    widths = []
    for i, col in enumerate(columns):
        w = display_width(str(col))
        for row in body:
            w = max(w, display_width(row[i]))
        widths.append(min(w, max_width))

    def fit(text, width):
        text = text.replace("\n", " ")
        if display_width(text) > width:
            cut, total = [], 0
            for ch in text:
                total += display_width(ch)
                if total > width - 1:
                    break
                cut.append(ch)
            return "".join(cut) + "…"
        return text + " " * (width - display_width(text))

    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(fit(str(c), widths[i]) for i, c in enumerate(columns)) + " |", line]
    for row in body:
        out.append("| " + " | ".join(fit(row[i], widths[i]) for i in range(len(columns))) + " |")
    out.append(line)
    return "\n".join(out)
