# -*- coding: utf-8 -*-
"""文件管理：把一个表映射为一个物理数据文件。

文件布局：
    第 0 页为文件头页，保存魔数、总页数、空闲页链表头、最后一个数据页页号；
    第 1 页及之后为数据页，数据页之间通过 prev / next 组成双向链表。
"""

import os
import struct
import threading
import weakref

from ..utils.constants import (
    FILE_FREE_LIST_OFFSET,
    FILE_LAST_PAGE_OFFSET,
    FILE_MAGIC,
    FILE_MAGIC_OFFSET,
    FILE_PAGE_COUNT_OFFSET,
    OVERFLOW_FILE_SUFFIX,
    PAGE_SIZE,
    STORAGE_LOG_FILENAME,
)
from ..utils.helpers import ensure_dir
from .log import StorageLog
from .overflow import OverflowRef, OverflowStore
from .page import Page, PageCorruptionError

HEADER_PAGE_ID = 0
FIRST_DATA_PAGE_ID = 1


class FileCorruptionError(IOError):
    """数据文件结构错误，消息中包含表名和具体页号。"""

    def __init__(self, table, message, page_id=None):
        self.table = table
        self.page_id = page_id
        location = "表=%s" % table
        if page_id is not None:
            location += " 页=%d" % page_id
        super().__init__("数据文件损坏（%s）：%s" % (location, message))


class FileManager:
    """负责 .dat 文件的创建、删除、页读写与页分配。"""

    def __init__(self, data_dir, logger=None, storage_log=None):
        self.data_dir = ensure_dir(data_dir)
        self.logger = logger
        if storage_log is True:
            storage_log = os.path.join(self.data_dir, STORAGE_LOG_FILENAME)
        if isinstance(storage_log, (str, bytes, os.PathLike)):
            storage_log = StorageLog(storage_log)
        self.storage_log = storage_log
        self._overflow_stores = {}
        # 文件头采用写穿缓存：频繁的 num_pages/last_page_id 不再重复读盘，
        # 元数据更新仍在方法返回前落盘，避免分配结果只存在于内存。
        self._header_cache = {}
        self._header_hits = 0
        self._header_misses = 0
        self._header_writes = 0
        self._header_lock = threading.RLock()
        # FileManager 的拓扑修改绕过普通 fetch 路径，借助观察者在修改前
        # 刷回并失效相关缓存页，防止脏数据被磁盘旧副本覆盖。
        self._buffer_pools = weakref.WeakSet()

    def _log(self, operation, table=None, page_id=None, slot_id=None, **details):
        if self.storage_log is None:
            return None
        return self.storage_log.append(
            operation, table=table, page_id=page_id, slot_id=slot_id, **details
        )

    def enable_storage_log(self, path=None, durable=False, auto_flush=True):
        """Enable persistent operation logging after construction."""
        if self.storage_log is not None:
            return False
        if path is None:
            path = os.path.join(self.data_dir, STORAGE_LOG_FILENAME)
        self.storage_log = StorageLog(path, durable=durable, auto_flush=auto_flush)
        return True

    def disable_storage_log(self, durable=False):
        if self.storage_log is None:
            return False
        self.storage_log.close(durable=durable)
        self.storage_log = None
        for store in self._overflow_stores.values():
            store.storage_log = None
        return True

    def flush_storage_log(self, durable=False):
        if self.storage_log is None:
            return False
        self.storage_log.flush(durable=durable)
        return True

    def storage_log_entries(self, start_lsn=1, end_lsn=None):
        if self.storage_log is None:
            return iter(())
        return self.storage_log.entries(start_lsn, end_lsn)

    def _overflow_store(self, table):
        name = str(table).lower()
        store = self._overflow_stores.get(name)
        if store is None:
            store = OverflowStore(
                self.data_dir, name, self.logger, self.storage_log
            )
            self._overflow_stores[name] = store
        else:
            store.storage_log = self.storage_log
        return store

    def register_buffer_pool(self, buffer_pool):
        self._buffer_pools.add(buffer_pool)
        cache_header = getattr(buffer_pool, "cache_header", None)
        if cache_header is not None:
            with self._header_lock:
                cached_headers = list(self._header_cache.items())
            for table, header in cached_headers:
                cache_header(table, header)

    def unregister_buffer_pool(self, buffer_pool):
        self._buffer_pools.discard(buffer_pool)

    def _prepare_external_write(self, table, page_ids):
        page_ids = tuple(sorted(set(page_id for page_id in page_ids if page_id != -1)))
        for buffer_pool in list(self._buffer_pools):
            buffer_pool.prepare_external_write(table, page_ids)

    def _cache_header_in_pools(self, table, header):
        for buffer_pool in list(self._buffer_pools):
            cache_header = getattr(buffer_pool, "cache_header", None)
            if cache_header is not None:
                cache_header(table, header)

    def _snapshot_header_from_pools(self, table):
        for buffer_pool in list(self._buffer_pools):
            snapshot_header = getattr(buffer_pool, "snapshot_header", None)
            if snapshot_header is not None:
                header = snapshot_header(table)
                if header is not None:
                    return header
        return None

    def _invalidate_header_in_pools(self, table=None):
        for buffer_pool in list(self._buffer_pools):
            invalidate_header = getattr(buffer_pool, "invalidate_header", None)
            if invalidate_header is not None:
                invalidate_header(table)

    # ---------------- 基础路径 ----------------
    def _path(self, table):
        return os.path.join(self.data_dir, "%s.dat" % table)

    def _overflow_path(self, table):
        return os.path.join(
            self.data_dir, "%s%s" % (str(table).lower(), OVERFLOW_FILE_SUFFIX)
        )

    def table_exists(self, table):
        return os.path.exists(self._path(table))

    # ---------------- 文件级操作 ----------------
    def create_file(self, table):
        path = self._path(table)
        if os.path.exists(path):
            raise FileExistsError("表文件已存在：%s" % table)
        header = bytearray(PAGE_SIZE)
        header[FILE_MAGIC_OFFSET:FILE_MAGIC_OFFSET + 4] = FILE_MAGIC
        struct.pack_into("<i", header, FILE_PAGE_COUNT_OFFSET, 1)   # 仅头页
        struct.pack_into("<i", header, FILE_FREE_LIST_OFFSET, -1)   # 空闲链表空
        struct.pack_into("<i", header, FILE_LAST_PAGE_OFFSET, -1)   # 无数据页
        with open(path, "wb") as fp:
            fp.write(header)
        with self._header_lock:
            self._header_cache[table] = bytearray(header)
            self._header_writes += 1
        self._cache_header_in_pools(table, header)
        self._log("CREATE_FILE", table=table)
        return path

    def drop_file(self, table):
        path = self._path(table)
        if os.path.exists(path):
            os.remove(path)
            self.invalidate_header(table)
            overflow_path = self._overflow_path(table)
            if os.path.exists(overflow_path):
                os.remove(overflow_path)
            self._overflow_stores.pop(str(table).lower(), None)
            self._log("DROP_FILE", table=table)
            return True
        return False

    def list_files(self):
        """列出数据目录下所有表名。"""
        names = []
        for name in os.listdir(self.data_dir):
            if name.endswith(".dat"):
                names.append(name[:-4])
        return sorted(names)

    # ---------------- 文件头读写 ----------------
    def _read_header(self, table, refresh=False):
        if not refresh:
            pooled_header = self._snapshot_header_from_pools(table)
            if pooled_header is not None:
                with self._header_lock:
                    self._header_hits += 1
                return pooled_header
        with self._header_lock:
            if not refresh and table in self._header_cache:
                self._header_hits += 1
                return bytearray(self._header_cache[table])

        path = self._path(table)
        with open(path, "rb") as fp:
            fp.seek(HEADER_PAGE_ID * PAGE_SIZE)
            data = fp.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE or data[:4] != FILE_MAGIC:
            raise FileCorruptionError(table, "文件头长度或魔数无效", HEADER_PAGE_ID)
        page_count = struct.unpack_from("<i", data, FILE_PAGE_COUNT_OFFSET)[0]
        free_head = struct.unpack_from("<i", data, FILE_FREE_LIST_OFFSET)[0]
        last_page = struct.unpack_from("<i", data, FILE_LAST_PAGE_OFFSET)[0]
        if page_count < 1:
            raise FileCorruptionError(table, "总页数 %d 小于 1" % page_count, HEADER_PAGE_ID)
        for field, page_id in (("空闲链表头", free_head), ("最后数据页", last_page)):
            if page_id != -1 and not FIRST_DATA_PAGE_ID <= page_id < page_count:
                raise FileCorruptionError(
                    table, "%s页号 %d 超出 [1, %d)" % (field, page_id, page_count),
                    HEADER_PAGE_ID,
                )
        try:
            actual_size = os.path.getsize(path)
        except OSError:
            actual_size = len(data)
        expected_size = page_count * PAGE_SIZE
        if actual_size < expected_size:
            raise FileCorruptionError(
                table,
                "文件仅有 %d 字节，文件头声明需要 %d 字节" % (actual_size, expected_size),
                HEADER_PAGE_ID,
            )
        with self._header_lock:
            self._header_cache[table] = bytearray(data)
            self._header_misses += 1
        self._cache_header_in_pools(table, data)
        return bytearray(data)

    def _write_header(self, table, header):
        if len(header) != PAGE_SIZE or header[:4] != FILE_MAGIC:
            raise FileCorruptionError(table, "拒绝写入无效文件头", HEADER_PAGE_ID)
        with open(self._path(table), "r+b") as fp:
            fp.seek(HEADER_PAGE_ID * PAGE_SIZE)
            fp.write(header)
            fp.flush()
        with self._header_lock:
            self._header_cache[table] = bytearray(header)
            self._header_writes += 1
        self._cache_header_in_pools(table, header)
        self._log("HEADER_WRITE", table=table)

    def invalidate_header(self, table=None):
        """使一个或全部文件头缓存失效，供外部文件维护后调用。"""
        with self._header_lock:
            if table is None:
                count = len(self._header_cache)
                self._header_cache.clear()
                target = None
                removed = count
            else:
                removed = self._header_cache.pop(table, None) is not None
                target = table
        self._invalidate_header_in_pools(target)
        return removed

    def header_stats(self):
        """返回文件头缓存命中情况。"""
        with self._header_lock:
            total = self._header_hits + self._header_misses
            return {
                "cached": len(self._header_cache),
                "hits": self._header_hits,
                "misses": self._header_misses,
                "writes": self._header_writes,
                "hit_rate": self._header_hits / total if total else 0.0,
            }

    def num_pages(self, table):
        """文件总页数（含头页）。"""
        header = self._read_header(table)
        return struct.unpack_from("<i", header, FILE_PAGE_COUNT_OFFSET)[0]

    def last_page_id(self, table):
        header = self._read_header(table)
        return struct.unpack_from("<i", header, FILE_LAST_PAGE_OFFSET)[0]

    def data_page_ids(self, table):
        """产出所有数据页页号（1 .. page_count - 1）。"""
        return range(FIRST_DATA_PAGE_ID, self.num_pages(table))

    # ---------------- 页读写 ----------------
    def read_page(self, table, page_id):
        """读取一页；若缓冲池已有更新版本，则返回其一致性快照。"""
        for buffer_pool in list(self._buffer_pools):
            snapshot = buffer_pool.snapshot_page(table, page_id)
            if snapshot is not None:
                return snapshot
        return self._read_page_from_disk(table, page_id)

    def _read_page_from_disk(self, table, page_id):
        """供缓冲池和文件拓扑操作使用的无缓存递归磁盘读取。"""
        if not isinstance(page_id, int) or isinstance(page_id, bool):
            raise TypeError("页号必须是整数")
        page_count = self.num_pages(table)
        if page_id < FIRST_DATA_PAGE_ID or page_id >= page_count:
            raise IndexError("页号越界：表=%s 页=%d，有效范围=[1, %d)"
                             % (table, page_id, page_count))
        with open(self._path(table), "rb") as fp:
            fp.seek(page_id * PAGE_SIZE)
            data = fp.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise FileCorruptionError(table, "物理页长度不是 %d 字节" % PAGE_SIZE, page_id)
        try:
            return Page(page_id, data)
        except PageCorruptionError as error:
            raise FileCorruptionError(table, str(error), page_id) from error

    def write_page(self, table, page):
        """直接写页；写入前同步并失效缓冲池中的同一页。"""
        self._prepare_external_write(table, (page.page_id,))
        self._write_page_from_buffer(table, page)

    def _write_page_from_buffer(self, table, page):
        """缓冲池刷盘专用入口，避免再次触发自身同步。"""
        path = self._path(table)
        page_count = os.path.getsize(path) // PAGE_SIZE
        if page.page_id < FIRST_DATA_PAGE_ID or page.page_id >= page_count:
            raise IndexError("页号越界：表=%s 页=%d，有效范围=[1, %d)"
                             % (table, page.page_id, page_count))
        try:
            page.validate(expected_page_id=page.page_id)
        except PageCorruptionError as error:
            raise FileCorruptionError(table, str(error), page.page_id) from error
        with open(path, "r+b") as fp:
            fp.seek(page.page_id * PAGE_SIZE)
            fp.write(page.to_bytes())
            fp.flush()
        self._log("PAGE_WRITE", table=table, page_id=page.page_id,
                  records=page.num_records, dirty=True)

    def validate_file(self, table):
        """校验整个文件并定位损坏的页链、空闲链或槽目录。

        返回有效页、空闲页和总页数统计；发现问题时抛出
        FileCorruptionError，其属性包含 table/page_id。
        """
        header = self._read_header(table, refresh=True)
        page_count = struct.unpack_from("<i", header, FILE_PAGE_COUNT_OFFSET)[0]
        free_head = struct.unpack_from("<i", header, FILE_FREE_LIST_OFFSET)[0]
        last_page = struct.unpack_from("<i", header, FILE_LAST_PAGE_OFFSET)[0]

        free_pages = set()
        current = free_head
        while current != -1:
            if current in free_pages:
                raise FileCorruptionError(table, "空闲页链表存在环", current)
            if current < FIRST_DATA_PAGE_ID or current >= page_count:
                raise FileCorruptionError(table, "空闲页链表指针越界", current)
            free_pages.add(current)
            current = self._read_page_from_disk(table, current).next_page_id

        active_pages = set()
        expected_next = -1
        current = last_page
        while current != -1:
            if current in active_pages:
                raise FileCorruptionError(table, "数据页链表存在环", current)
            if current in free_pages:
                raise FileCorruptionError(table, "同一页同时出现在数据链和空闲链", current)
            if current < FIRST_DATA_PAGE_ID or current >= page_count:
                raise FileCorruptionError(table, "数据页链表指针越界", current)
            page = self._read_page_from_disk(table, current)
            if page.next_page_id != expected_next:
                raise FileCorruptionError(
                    table,
                    "next=%d，期望为 %d" % (page.next_page_id, expected_next),
                    current,
                )
            active_pages.add(current)
            expected_next = current
            current = page.prev_page_id

        accounted = active_pages | free_pages
        all_pages = set(range(FIRST_DATA_PAGE_ID, page_count))
        missing = sorted(all_pages - accounted)
        if missing:
            raise FileCorruptionError(table, "页未挂入数据链或空闲链", missing[0])
        return {
            "table": table,
            "total_pages": page_count,
            "data_pages": len(active_pages),
            "free_pages": len(free_pages),
        }

    # ---------------- 页分配 / 回收 ----------------
    def allocate_page(self, table):
        """分配一个可用的数据页：优先复用空闲链表，否则追加新页。"""
        header = self._read_header(table)
        free_head = struct.unpack_from("<i", header, FILE_FREE_LIST_OFFSET)[0]
        last_page = struct.unpack_from("<i", header, FILE_LAST_PAGE_OFFSET)[0]
        self._prepare_external_write(table, (free_head, last_page))

        if free_head != -1:
            free_page = self._read_page_from_disk(table, free_head)
            next_free = free_page.next_page_id          # 空闲页复用 next 字段作为链表指针
            page = Page(free_head)
            page.prev_page_id = last_page
            if last_page != -1:
                previous = self._read_page_from_disk(table, last_page)
                previous.next_page_id = free_head
                self._write_page_from_buffer(table, previous)
            struct.pack_into("<i", header, FILE_FREE_LIST_OFFSET, next_free)
            struct.pack_into("<i", header, FILE_LAST_PAGE_OFFSET, free_head)
            self._write_header(table, header)
            self._write_page_from_buffer(table, page)
            self._log("ALLOCATE_PAGE", table=table, page_id=page.page_id,
                      reused=True)
            return page

        page_count = struct.unpack_from("<i", header, FILE_PAGE_COUNT_OFFSET)[0]
        page_id = page_count
        page = Page(page_id)
        page.prev_page_id = last_page

        if last_page != -1:
            prev = self._read_page_from_disk(table, last_page)
            prev.next_page_id = page_id
            self._write_page_from_buffer(table, prev)

        struct.pack_into("<i", header, FILE_PAGE_COUNT_OFFSET, page_count + 1)
        struct.pack_into("<i", header, FILE_LAST_PAGE_OFFSET, page_id)
        self._write_header(table, header)
        # 此时文件头已经声明了新页，而物理文件尚未扩展；直接追加可避免
        # write_page 的完整性检查把这个正常的分配中间态误判为截断文件。
        with open(self._path(table), "r+b") as fp:
            fp.seek(page_id * PAGE_SIZE)
            fp.write(page.to_bytes())
            fp.flush()
        self._log("ALLOCATE_PAGE", table=table, page_id=page_id, reused=False)
        return page

    def free_page(self, table, page_id):
        """从数据页链表摘除并回收一页，之后 allocate_page 会优先复用。"""
        header = self._read_header(table)
        page_count = struct.unpack_from("<i", header, FILE_PAGE_COUNT_OFFSET)[0]
        if not isinstance(page_id, int) or isinstance(page_id, bool):
            raise TypeError("页号必须是整数")
        if page_id < FIRST_DATA_PAGE_ID or page_id >= page_count:
            raise IndexError("页号越界：表=%s 页=%d，有效范围=[1, %d)"
                             % (table, page_id, page_count))

        free_head = struct.unpack_from("<i", header, FILE_FREE_LIST_OFFSET)[0]
        current = free_head
        seen = set()
        while current != -1:
            if current in seen:
                raise FileCorruptionError(table, "空闲页链表存在环", current)
            if current < FIRST_DATA_PAGE_ID or current >= page_count:
                raise FileCorruptionError(table, "空闲页链表指针越界", current)
            if current == page_id:
                raise ValueError("页已在空闲链表中：表=%s 页=%d" % (table, page_id))
            seen.add(current)
            current = self._read_page_from_disk(table, current).next_page_id

        # 目标页可能在缓冲池中仍是脏页，必须先刷回，才能从磁盘取得
        # 最新的 prev/next；相关邻页也必须在直接改盘前失效。
        self._prepare_external_write(table, (page_id,))
        old_page = self._read_page_from_disk(table, page_id)
        previous_id = old_page.prev_page_id
        next_id = old_page.next_page_id
        self._prepare_external_write(table, (previous_id, next_id))
        if previous_id != -1:
            previous = self._read_page_from_disk(table, previous_id)
            previous.next_page_id = next_id
            self._write_page_from_buffer(table, previous)
        if next_id != -1:
            following = self._read_page_from_disk(table, next_id)
            following.prev_page_id = previous_id
            self._write_page_from_buffer(table, following)

        last_page = struct.unpack_from("<i", header, FILE_LAST_PAGE_OFFSET)[0]
        if last_page == page_id:
            struct.pack_into("<i", header, FILE_LAST_PAGE_OFFSET, previous_id)

        page = Page(page_id)
        page.next_page_id = free_head
        self._write_page_from_buffer(table, page)
        struct.pack_into("<i", header, FILE_FREE_LIST_OFFSET, page_id)
        self._write_header(table, header)
        self._log("FREE_PAGE", table=table, page_id=page_id)
        return True

    # ---------------- 溢出页 ----------------
    def write_overflow(self, table, data, replace=None):
        """将超出普通页容量的字节写入溢出页链，返回 ``OverflowRef``。"""
        if not self.table_exists(table):
            raise FileNotFoundError("表文件不存在：%s" % table)
        return self._overflow_store(table).write(data, replace=replace)

    def read_overflow(self, table, ref):
        """按溢出引用读取完整字节串。"""
        return self._overflow_store(table).read(ref)

    def free_overflow(self, table, ref):
        """释放一条溢出页链并回收到溢出文件空闲链。"""
        return self._overflow_store(table).free(ref)

    # 语义更明确的别名，便于上层把它当作超大记录存储使用。
    write_overflow_record = write_overflow
    read_overflow_record = read_overflow
    free_overflow_record = free_overflow

    def overflow_stats(self, table):
        return self._overflow_store(table).stats()

    def close(self, durable_log=False):
        """关闭存储日志；普通页由 BufferPool 自己负责 flush。"""
        if self.storage_log is not None:
            self.storage_log.close(durable=durable_log)
            self.storage_log = None
