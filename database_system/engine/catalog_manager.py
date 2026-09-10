# -*- coding: utf-8 -*-
"""系统目录管理：元数据以“特殊系统表” __catalog 的形式持久化存储。

系统表 __catalog 的表结构在代码中引导（bootstrap）定义，
其余用户表的结构则以记录的形式存放在这张特殊表中。
"""

from ..sql_compiler.catalog import Catalog, Column, TableSchema
from ..utils.constants import CATALOG_COL_DEF, CATALOG_COL_TABLE, CATALOG_TABLE, TYPE_TEXT


class CatalogManager(Catalog):
    """在 Catalog（内存模式目录）基础上增加持久化能力。"""

    BOOTSTRAP_SCHEMA = TableSchema(
        CATALOG_TABLE,
        [Column(CATALOG_COL_TABLE, TYPE_TEXT, primary_key=True),
         Column(CATALOG_COL_DEF, TYPE_TEXT)],
    )

    def __init__(self, storage_engine, logger=None):
        super().__init__()
        self.engine = storage_engine
        self.logger = logger

    # ---------------- 引导与加载 ----------------
    def bootstrap(self):
        """确保系统表存在并加载元数据。"""
        self.engine.ensure_table_file(self.BOOTSTRAP_SCHEMA)
        self.load()

    def load(self):
        """从系统表中读入所有表定义。"""
        self.clear()
        for row in self.engine.scan_table(CATALOG_TABLE):
            _rid, values = row
            schema = TableSchema.from_json(values[CATALOG_COL_DEF])
            self._tables[schema.name] = schema
        if self.logger:
            self.logger.debug("系统目录加载完成，共 %d 张表", len(self._tables))
        return self

    def save(self):
        """整体重写系统表（DDL 频率低，直接重写更简单可靠）。"""
        rows = [{CATALOG_COL_TABLE: name, CATALOG_COL_DEF: schema.to_json()}
                for name, schema in self._tables.items()]
        self.engine.rewrite_catalog(self.BOOTSTRAP_SCHEMA, rows)

    # ---------------- DDL ----------------
    def create_table(self, schema):
        if schema.name == CATALOG_TABLE:
            raise ValueError("%s 是系统保留表名" % CATALOG_TABLE)
        super().create_table(schema)
        self.save()
        return schema

    def drop_table(self, name):
        removed = super().drop_table(name)
        if removed:
            self.save()
        return removed
