# -*- coding: utf-8 -*-
"""存储引擎：负责行与页的映射、磁盘组织、记录增删查改。

对外提供表级接口，内部通过文件管理器和缓冲池访问物理页。
"""

import struct
from itertools import chain

from .catalog_manager import CatalogManager
from ..storage.buffer import BufferPool
from ..storage.file_manager import FileManager
from ..utils.constants import (
    CATALOG_TABLE,
    DEFAULT_DATA_DIR,
    DEFAULT_POOL_SIZE,
    DEFAULT_STRATEGY,
    LEN_PREFIX_SIZE,
    MAX_TEXT_LENGTH,
    PAGE_HEADER_SIZE,
    PAGE_SIZE,
    SLOT_SIZE,
    TYPE_BOOL,
    TYPE_FLOAT,
    TYPE_INT,
    TYPE_TEXT,
)
from ..utils.helpers import ensure_dir, get_logger

MAX_RECORD_SIZE = PAGE_SIZE - PAGE_HEADER_SIZE - SLOT_SIZE

# 预编译的结构体 / 列长度：解码热路径上不再重复解析格式串
_STRUCT_INT = struct.Struct("<i")
_STRUCT_FLOAT = struct.Struct("<d")
_STRUCT_BOOL = struct.Struct("<?")
_STRUCT_LEN = struct.Struct("<H")
_FIXED_SIZE = {TYPE_INT: 4, TYPE_FLOAT: 8, TYPE_BOOL: 1}
_FIXED_STRUCT = {TYPE_INT: _STRUCT_INT, TYPE_FLOAT: _STRUCT_FLOAT, TYPE_BOOL: _STRUCT_BOOL}


class StorageError(Exception):
    """存储引擎异常。"""


class DuplicateKeyError(StorageError):
    """主键冲突。"""


def _fixed_reader(unpack, size, mask, byte_index):
    """生成定长列的读取函数：f(data, offset) -> (值, 新偏移)。"""
    def read(data, offset, _unpack=unpack, _size=size, _mask=mask, _bi=byte_index):
        if data[_bi] & _mask:
            return None, offset                       # NULL 列不写入正文
        return _unpack(data, offset)[0], offset + _size
    return read


def _skip_fixed(size, mask, byte_index):
    """生成定长列的跳过函数：只推进偏移，不产出值。"""
    def skip(data, offset, _size=size, _mask=mask, _bi=byte_index):
        return None, (offset if data[_bi] & _mask else offset + _size)
    return skip


def _text_reader(mask, byte_index):
    """生成 TEXT 列的读取函数：读长度前缀后再解出 UTF-8 字符串。"""
    length_unpack = _STRUCT_LEN.unpack_from
    prefix = LEN_PREFIX_SIZE

    def read(data, offset, _len=length_unpack, _p=prefix, _mask=mask, _bi=byte_index):
        if data[_bi] & _mask:
            return None, offset
        length = _len(data, offset)[0]
        start = offset + _p
        return data[start:start + length].decode("utf-8"), start + length
    return read


def _skip_text(mask, byte_index):
    """生成 TEXT 列的跳过函数：省掉切片与 UTF-8 解码，只按长度跳过。"""
    length_unpack = _STRUCT_LEN.unpack_from
    prefix = LEN_PREFIX_SIZE

    def skip(data, offset, _len=length_unpack, _p=prefix, _mask=mask, _bi=byte_index):
        if data[_bi] & _mask:
            return None, offset
        return None, offset + _p + _len(data, offset)[0]
    return skip


def _column_step(column, index, wanted):
    """为第 index 列生成 (列名, 读取函数)；wanted=False 时列名为 None（只跳过）。"""
    column_type = column.type
    mask = 1 << (index % 8)
    byte_index = index // 8
    if column_type == TYPE_TEXT:
        if wanted:
            return column.name, _text_reader(mask, byte_index)
        return None, _skip_text(mask, byte_index)
    if column_type not in _FIXED_SIZE:
        raise StorageError("不支持的数据类型：%s" % column_type)
    if wanted:
        return column.name, _fixed_reader(_FIXED_STRUCT[column_type].unpack_from,
                                         _FIXED_SIZE[column_type], mask, byte_index)
    return None, _skip_fixed(_FIXED_SIZE[column_type], mask, byte_index)


class RecordCodec:
    """记录序列化：空值位图 + 各列编码。

    定长类型：INT 4B、FLOAT 8B、BOOL 1B；变长类型 TEXT：2B 长度前缀 + UTF-8 字节。
    """

    @staticmethod
    def encode(schema, values):
        columns = schema.columns
        bitmap = bytearray((len(columns) + 7) // 8)
        body = bytearray()

        for index, column in enumerate(columns):
            value = values.get(column.name)
            if value is None:
                bitmap[index // 8] |= (1 << (index % 8))
                continue
            if column.type == TYPE_INT:
                body += struct.pack("<i", int(value))
            elif column.type == TYPE_FLOAT:
                body += struct.pack("<d", float(value))
            elif column.type == TYPE_BOOL:
                body += struct.pack("<?", bool(value))
            elif column.type == TYPE_TEXT:
                raw = str(value).encode("utf-8")
                if len(raw) > MAX_TEXT_LENGTH:
                    raise StorageError("列 %s 的值超过 %d 字节上限" % (column.name, MAX_TEXT_LENGTH))
                body += struct.pack("<H", len(raw)) + raw
            else:
                raise StorageError("不支持的数据类型：%s" % column.type)

        return bytes(bitmap) + bytes(body)

    @staticmethod
    def decode(schema, data):
        columns = schema.columns
        bitmap_size = (len(columns) + 7) // 8
        bitmap = data[:bitmap_size]
        offset = bitmap_size
        values = {}

        for index, column in enumerate(columns):
            is_null = bool(bitmap[index // 8] & (1 << (index % 8)))
            if is_null:
                values[column.name] = None
                continue
            if column.type == TYPE_INT:
                values[column.name] = struct.unpack_from("<i", data, offset)[0]
                offset += 4
            elif column.type == TYPE_FLOAT:
                values[column.name] = struct.unpack_from("<d", data, offset)[0]
                offset += 8
            elif column.type == TYPE_BOOL:
                values[column.name] = struct.unpack_from("<?", data, offset)[0]
                offset += 1
            elif column.type == TYPE_TEXT:
                length = struct.unpack_from("<H", data, offset)[0]
                offset += LEN_PREFIX_SIZE
                values[column.name] = data[offset:offset + length].decode("utf-8")
                offset += length
            else:
                raise StorageError("不支持的数据类型：%s" % column.type)
        return values

    @staticmethod
    def plan(schema, columns=None):
        """预编译一份解码计划：每列一个读取闭包。

        plan 的元素是 (列名, 读取函数)；列名为 None 表示该列被裁剪：
        字节仍要跳过，但不产出值。type/位图掩码/长度这些信息解码前就算好，
        逐行只剩函数调用与必要的解包。
        """
        return [_column_step(column, index, columns is None or column.name in columns)
                for index, column in enumerate(schema.columns)]

    @staticmethod
    def decode_planned(plan, data, offset):
        """按计划解码全部列（无裁剪时的快速路径，没有 None 列判断）。"""
        values = {}
        for name, read in plan:
            value, offset = read(data, offset)
            values[name] = value
        return values

    @staticmethod
    def decode_selected(plan, data, offset):
        """按计划解码选定的列，其余列只跳过字节。"""
        values = {}
        for name, read in plan:
            value, offset = read(data, offset)
            if name is not None:
                values[name] = value
        return values


class StorageEngine:
    """存储引擎。"""

    def __init__(self, data_dir=DEFAULT_DATA_DIR, pool_size=DEFAULT_POOL_SIZE,
                 strategy=DEFAULT_STRATEGY, logger=None):
        self.data_dir = ensure_dir(data_dir)
        self.logger = logger or get_logger("minidb.storage")
        self.file_manager = FileManager(self.data_dir, self.logger)
        self.buffer = BufferPool(self.file_manager, pool_size, strategy, self.logger)
        self.catalog = CatalogManager(self, self.logger)
        self._insert_hint = {}          # 表名 -> 上次成功插入的页号（插入位置提示）
        self._key_index = {}            # 表名 -> {"column": 主键列名, "values": 主键值集合}
        self.catalog.bootstrap()

    # ============================ 模式访问 ============================
    def get_schema(self, table):
        name = str(table).lower()
        if name == CATALOG_TABLE:
            return CatalogManager.BOOTSTRAP_SCHEMA
        return self.catalog.get_table(name)

    def table_exists(self, table):
        return self.catalog.has_table(table)

    def list_tables(self):
        return self.catalog.list_tables()

    def create_table(self, schema):
        if schema.name == CATALOG_TABLE:
            raise StorageError("%s 是系统保留表名" % CATALOG_TABLE)
        # 孤儿文件：数据文件残留但系统目录中无登记，直接重置，避免表被永久"卡死"
        if (self.file_manager.table_exists(schema.name)
                and not self.catalog.has_table(schema.name)):
            self.buffer.discard_table(schema.name)
            self.file_manager.drop_file(schema.name)
        self.file_manager.create_file(schema.name)
        try:
            self.catalog.create_table(schema)
        except Exception:
            self.file_manager.drop_file(schema.name)
            raise
        self._insert_hint.pop(schema.name, None)
        self._key_index.pop(schema.name, None)
        return schema

    def drop_table(self, table):
        name = str(table).lower()
        if name == CATALOG_TABLE:
            raise StorageError("系统表 %s 不允许删除" % CATALOG_TABLE)
        if not self.catalog.has_table(name):
            raise StorageError("表 %s 不存在" % name)
        # 顺序：先移除目录登记（持久化），再删数据文件。
        # 中途崩溃只会留下孤儿文件，而孤儿文件能被 create_table 自动清理。
        self.buffer.discard_table(name)
        self.catalog.drop_table(name)
        self.file_manager.drop_file(name)
        self._insert_hint.pop(name, None)
        self._key_index.pop(name, None)
        return name

    # ============================ 文件（供系统目录引导使用）============================
    def ensure_table_file(self, schema):
        if not self.file_manager.table_exists(schema.name):
            self.file_manager.create_file(schema.name)

    def rewrite_catalog(self, schema, rows):
        """整体重写系统表。"""
        name = schema.name
        self.buffer.discard_table(name)          # 丢弃旧页，避免读到被删除文件的缓存
        if self.file_manager.table_exists(name):
            self.file_manager.drop_file(name)
        self.file_manager.create_file(name)
        self._insert_hint.pop(name, None)
        self._key_index.pop(name, None)
        for row in rows:
            self.insert_row(name, row)

    # ============================ 记录的增删改查 ============================
    def insert_row(self, table, values):
        """插入一行，返回行地址 rid = (page_id, slot_id)。"""
        name = str(table).lower()
        schema = self.get_schema(name)
        if schema is None:
            raise StorageError("表 %s 不存在" % name)
        self.ensure_table_file(schema)

        row_values = {str(k).lower(): v for k, v in values.items()}
        self._fill_defaults(schema, row_values)

        primary_key = schema.primary_key
        if primary_key is not None:
            key_value = row_values.get(primary_key.name)
            if key_value is None:
                raise StorageError("主键列 %s 不允许为 NULL" % primary_key.name)
            key_values = self._key_values(name, primary_key.name)
            if key_value in key_values:
                raise DuplicateKeyError("主键冲突：%s = %r 已存在" % (primary_key.name, key_value))

        data = RecordCodec.encode(schema, row_values)
        if len(data) > MAX_RECORD_SIZE:
            raise StorageError("记录过大（%d 字节），超过单页可容纳上限 %d 字节"
                               % (len(data), MAX_RECORD_SIZE))

        page_id, slot_id = self._write_record(name, data)
        if primary_key is not None:
            key_values.add(key_value)
        return (page_id, slot_id)

    @staticmethod
    def _fill_defaults(schema, row_values):
        for column in schema.columns:
            row_values.setdefault(column.name, None)

    def _write_record(self, table, data):
        """寻找有空闲空间的页写入记录，必要时分配新页。"""
        page_count = self.file_manager.num_pages(table)
        hint = self._insert_hint.get(table, 1)

        # 惰性拼接候选页：大表上不必每次插入都物化一张页码列表
        candidates = chain(range(hint, page_count), range(1, min(hint, page_count)))
        for page_id in candidates:
            page = self.buffer.fetch_page(table, page_id)
            slot_id = page.insert_record(data)
            if slot_id is not None:
                self.buffer.unpin_page(table, page_id, dirty=True)
                self._insert_hint[table] = page_id
                return page_id, slot_id
            self.buffer.unpin_page(table, page_id, dirty=False)

        new_page = self.file_manager.allocate_page(table)
        page_id = new_page.page_id
        page = self.buffer.fetch_page(table, page_id)
        slot_id = page.insert_record(data)
        if slot_id is None:
            self.buffer.unpin_page(table, page_id, dirty=False)
            raise StorageError("新分配的页仍无法容纳该记录")
        self.buffer.unpin_page(table, page_id, dirty=True)
        self._insert_hint[table] = page_id
        return page_id, slot_id

    # ============================ 主键索引 ============================
    def _key_values(self, name, key_column):
        """取表的主键值集合；不存在则扫一遍主键列惰性构建。

        主键唯一性检查原先每插入一行都要全表扫描一遍，批量插入因此退化成
        O(n^2)。这里用内存哈希集合把查找降到 O(1)，插入/更新/删除时增量维护；
        索引只是缓存，任何时候丢掉它都只会退回全表扫描。
        """
        entry = self._key_index.get(name)
        if entry is not None and entry["column"] == key_column:
            return entry["values"]
        values = set()
        if self.file_manager.table_exists(name):
            for _rid, row in self.scan_table(name, [key_column]):
                value = row.get(key_column)
                if value is not None:
                    values.add(value)
        self._key_index[name] = {"column": key_column, "values": values}
        return values

    def _key_index_entry(self, name, key_column):
        entry = self._key_index.get(name)
        if entry is not None and entry["column"] == key_column:
            return entry
        return None

    def _primary_key_exists(self, schema, key_name, key_value):
        return key_value in self._key_values(schema.name, key_name)

    def scan_table(self, table, columns=None):
        """全表扫描，逐行产出 (rid, values)。

        columns 非 None 时只解码这些列（执行期的投影下推）：其余列仍然要跳过
        字节，但不再做 struct 解包与 UTF-8 解码，也不再进入结果字典。
        """
        name = str(table).lower()
        schema = self.get_schema(name)
        if schema is None:
            raise StorageError("表 %s 不存在" % name)
        if not self.file_manager.table_exists(name):
            return

        wanted = None
        if columns is not None:
            wanted = set(str(column).lower() for column in columns)
            missing = sorted(column for column in wanted
                             if schema.get_column(column) is None)
            if missing:
                raise StorageError("表 %s 不存在列：%s" % (name, "、".join(missing)))
        # 解码计划一次算好，逐行只跑闭包
        plan = RecordCodec.plan(schema, wanted)
        bitmap_size = (len(schema.columns) + 7) // 8
        decode = (RecordCodec.decode_selected if wanted is not None
                  else RecordCodec.decode_planned)

        for page_id in self.file_manager.data_page_ids(name):
            page = self.buffer.fetch_page(name, page_id)
            try:
                for slot_id, data in list(page.records()):
                    yield (page_id, slot_id), decode(plan, data, bitmap_size)
            finally:
                self.buffer.unpin_page(name, page_id)

    def update_row(self, table, rid, new_values):
        """更新一行；空间不足时迁移到新位置并返回新的 rid。"""
        name = str(table).lower()
        schema = self.get_schema(name)
        if schema is None:
            raise StorageError("表 %s 不存在" % name)

        page_id, slot_id = rid
        page = self.buffer.fetch_page(name, page_id)
        old_data = page.get_record(slot_id)
        if old_data is None:
            self.buffer.unpin_page(name, page_id)
            raise StorageError("记录不存在：%s 页=%d 槽=%d" % (name, page_id, slot_id))

        old_row = RecordCodec.decode(schema, old_data)
        primary_key = schema.primary_key
        entry = self._key_index_entry(name, primary_key.name) if primary_key else None
        old_key = old_row.get(primary_key.name) if entry is not None else None

        merged = old_row
        merged.update({str(k).lower(): v for k, v in new_values.items()})
        new_key = merged.get(primary_key.name) if entry is not None else None
        data = RecordCodec.encode(schema, merged)

        if entry is not None:
            # 旧主键值先摘掉：写回成功再补新值（迁移分支由 insert_row 负责补）
            entry["values"].discard(old_key)

        if page.update_record(slot_id, data):
            self.buffer.unpin_page(name, page_id, dirty=True)
            if entry is not None:
                entry["values"].add(new_key)
            return (page_id, slot_id)

        page.delete_record(slot_id)
        self.buffer.unpin_page(name, page_id, dirty=True)
        return self.insert_row(name, merged)

    def delete_row(self, table, rid):
        """删除一行。"""
        name = str(table).lower()
        page_id, slot_id = rid
        page = self.buffer.fetch_page(name, page_id)
        entry = self._key_index.get(name)
        old_key = None
        if entry is not None:
            # 删除前先取出主键值，行失效后同步从索引摘掉，避免残留造成假冲突
            old_data = page.get_record(slot_id)
            if old_data is not None:
                schema = self.get_schema(name)
                if schema is not None:
                    old_key = RecordCodec.decode(schema, old_data).get(entry["column"])
        removed = page.delete_record(slot_id)
        self.buffer.unpin_page(name, page_id, dirty=removed)
        if removed and entry is not None:
            entry["values"].discard(old_key)
        return bool(removed)

    def row_count(self, table):
        """统计表的有效行数（只数槽，不解码记录）。"""
        name = str(table).lower()
        if self.get_schema(name) is None:
            raise StorageError("表 %s 不存在" % name)
        if not self.file_manager.table_exists(name):
            return 0
        total = 0
        for page_id in self.file_manager.data_page_ids(name):
            page = self.buffer.fetch_page(name, page_id)
            total += page.num_records
            self.buffer.unpin_page(name, page_id)
        return total

    def orphan_files(self):
        """存在数据文件但系统目录中未登记的表名（状态不一致的产物）。"""
        files = set(self.file_manager.list_files())
        files.discard(CATALOG_TABLE)
        return sorted(files - set(self.catalog.list_tables()))

    # ============================ 运行时 ============================
    def flush(self):
        """把所有脏页刷回磁盘。"""
        return self.buffer.flush_all()

    def stats(self):
        return self.buffer.stats()

    def log_stats(self):
        return self.buffer.log_stats()

    def close(self):
        self.flush()

    def __repr__(self):
        return "StorageEngine(dir=%s, tables=%d)" % (self.data_dir, len(self.catalog))
