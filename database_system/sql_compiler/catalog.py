# -*- coding: utf-8 -*-
"""模式目录管理：表结构（Schema）的内存模型。"""

import json

from ..utils.constants import SUPPORTED_TYPES, TYPE_ALIASES, TYPE_TEXT


class Column:
    """列定义。"""

    def __init__(self, name, type_name, length=0, primary_key=False, not_null=False):
        self.name = str(name).lower()
        self.type = self._normalize_type(type_name)
        self.length = int(length or 0)
        self.primary_key = bool(primary_key)
        self.not_null = bool(not_null) or self.primary_key

    @staticmethod
    def _normalize_type(type_name):
        key = str(type_name).upper()
        if key not in TYPE_ALIASES:
            raise ValueError("不支持的数据类型：%s" % type_name)
        resolved = TYPE_ALIASES[key]
        if resolved not in SUPPORTED_TYPES:
            raise ValueError("不支持的数据类型：%s" % type_name)
        return resolved

    def is_numeric(self):
        return self.type in ("INT", "FLOAT")

    def to_dict(self):
        return {
            "name": self.name,
            "type": self.type,
            "length": self.length,
            "primary_key": self.primary_key,
            "not_null": self.not_null,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["name"], data["type"],
            length=data.get("length", 0),
            primary_key=data.get("primary_key", False),
            not_null=data.get("not_null", False),
        )

    def __repr__(self):
        extra = ""
        if self.type == TYPE_TEXT and self.length:
            extra = "(%d)" % self.length
        flags = []
        if self.primary_key:
            flags.append("PRIMARY KEY")
        if self.not_null and not self.primary_key:
            flags.append("NOT NULL")
        return "%s %s%s%s" % (self.name, self.type, extra, (" " + " ".join(flags)) if flags else "")


class TableSchema:
    """表模式。"""

    def __init__(self, name, columns, root_page=1):
        self.name = str(name).lower()
        self.columns = list(columns)
        self.root_page = root_page

    # ---------------- 便捷访问 ----------------
    @property
    def column_names(self):
        return [c.name for c in self.columns]

    def get_column(self, name):
        key = str(name).lower()
        for col in self.columns:
            if col.name == key:
                return col
        return None

    def has_column(self, name):
        return self.get_column(name) is not None

    @property
    def primary_key(self):
        for col in self.columns:
            if col.primary_key:
                return col
        return None

    def to_json(self):
        return json.dumps({
            "name": self.name,
            "root_page": self.root_page,
            "columns": [c.to_dict() for c in self.columns],
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        return cls(
            data["name"],
            [Column.from_dict(c) for c in data["columns"]],
            root_page=data.get("root_page", 1),
        )

    def __repr__(self):
        return "TableSchema(%s: %s)" % (self.name, ", ".join(map(repr, self.columns)))


class Catalog:
    """内存中的模式目录，供 SQL 编译器做语义分析。"""

    def __init__(self):
        self._tables = {}

    def create_table(self, schema):
        if schema.name in self._tables:
            raise ValueError("表已存在：%s" % schema.name)
        self._tables[schema.name] = schema

    def drop_table(self, name):
        return self._tables.pop(str(name).lower(), None) is not None

    def get_table(self, name):
        return self._tables.get(str(name).lower())

    def has_table(self, name):
        return str(name).lower() in self._tables

    def list_tables(self):
        return sorted(self._tables.keys())

    def clear(self):
        self._tables.clear()

    def __contains__(self, name):
        return self.has_table(name)

    def __len__(self):
        return len(self._tables)
