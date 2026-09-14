# -*- coding: utf-8 -*-
"""Overflow-page storage for records larger than one normal data page.

Overflow pages live in ``<table>.overflow`` sidecar files.  Their local page
numbers and free list are independent from ``.dat`` so old scans over normal
data pages remain unchanged.  A returned :class:`OverflowRef` identifies the
head page and exact logical length of the chained value.
"""

import os
import struct
import threading
from collections import namedtuple

from ..utils.constants import (
    FILE_MAGIC_OFFSET,
    OVERFLOW_CHUNK_LENGTH_OFFSET,
    OVERFLOW_FILE_SUFFIX,
    OVERFLOW_FLAGS_OFFSET,
    OVERFLOW_FREE_LIST_OFFSET,
    OVERFLOW_MAGIC,
    OVERFLOW_NEXT_PAGE_OFFSET,
    OVERFLOW_NEXT_RECORD_OFFSET,
    OVERFLOW_PAGE_COUNT_OFFSET,
    OVERFLOW_PAGE_HEADER_SIZE,
    OVERFLOW_PAGE_ID_OFFSET,
    OVERFLOW_RECORD_ID_OFFSET,
    OVERFLOW_RECORD_FLAG,
    PAGE_SIZE,
)


OverflowRef = namedtuple("OverflowRef", "page_id length record_id")
OVERFLOW_CHUNK_SIZE = PAGE_SIZE - OVERFLOW_PAGE_HEADER_SIZE


class OverflowCorruptionError(IOError):
    """An overflow chain/file error with table and local page location."""

    def __init__(self, table, message, page_id=None):
        self.table = table
        self.page_id = page_id
        location = "表=%s" % table
        if page_id is not None:
            location += " 溢出页=%d" % page_id
        super().__init__("溢出文件损坏（%s）：%s" % (location, message))


class OverflowPage:
    """One fixed-size overflow page."""

    def __init__(self, page_id, data=None):
        if data is None:
            self._data = bytearray(PAGE_SIZE)
            struct.pack_into("<i", self._data, OVERFLOW_PAGE_ID_OFFSET, page_id)
            struct.pack_into("<i", self._data, OVERFLOW_NEXT_PAGE_OFFSET, -1)
            struct.pack_into("<H", self._data, OVERFLOW_CHUNK_LENGTH_OFFSET, 0)
            struct.pack_into("<H", self._data, OVERFLOW_FLAGS_OFFSET, OVERFLOW_RECORD_FLAG)
            struct.pack_into("<i", self._data, OVERFLOW_RECORD_ID_OFFSET, 0)
        else:
            if len(data) != PAGE_SIZE:
                raise ValueError("溢出页长度必须为 %d，收到 %d" % (PAGE_SIZE, len(data)))
            self._data = bytearray(data)
            if self.page_id != page_id:
                raise OverflowCorruptionError(
                    "?", "页头页号为 %d" % self.page_id, page_id
                )
            self.validate()

    @property
    def page_id(self):
        return struct.unpack_from("<i", self._data, OVERFLOW_PAGE_ID_OFFSET)[0]

    @property
    def next_page_id(self):
        return struct.unpack_from("<i", self._data, OVERFLOW_NEXT_PAGE_OFFSET)[0]

    @next_page_id.setter
    def next_page_id(self, value):
        struct.pack_into("<i", self._data, OVERFLOW_NEXT_PAGE_OFFSET, value)

    @property
    def chunk_length(self):
        return struct.unpack_from("<H", self._data, OVERFLOW_CHUNK_LENGTH_OFFSET)[0]

    @property
    def chunk(self):
        return bytes(self._data[
            OVERFLOW_PAGE_HEADER_SIZE:
            OVERFLOW_PAGE_HEADER_SIZE + self.chunk_length
        ])

    @chunk.setter
    def chunk(self, value):
        value = bytes(value)
        if len(value) > OVERFLOW_CHUNK_SIZE:
            raise ValueError("溢出页数据不能超过 %d 字节" % OVERFLOW_CHUNK_SIZE)
        self._data[OVERFLOW_PAGE_HEADER_SIZE:PAGE_SIZE] = b"\x00" * OVERFLOW_CHUNK_SIZE
        self._data[OVERFLOW_PAGE_HEADER_SIZE:OVERFLOW_PAGE_HEADER_SIZE + len(value)] = value
        struct.pack_into("<H", self._data, OVERFLOW_CHUNK_LENGTH_OFFSET, len(value))

    @property
    def record_id(self):
        return struct.unpack_from("<i", self._data, OVERFLOW_RECORD_ID_OFFSET)[0]

    @record_id.setter
    def record_id(self, value):
        struct.pack_into("<i", self._data, OVERFLOW_RECORD_ID_OFFSET, value)

    def validate(self):
        if self.chunk_length > OVERFLOW_CHUNK_SIZE:
            raise ValueError("溢出页块长度越界：%d" % self.chunk_length)
        if self.next_page_id == self.page_id:
            raise ValueError("溢出页 next 指向自身：%d" % self.page_id)
        return True

    def to_bytes(self):
        return bytes(self._data)


class OverflowStore:
    """Sidecar-file manager used by :class:`FileManager`."""

    def __init__(self, data_dir, table, logger=None, storage_log=None):
        self.data_dir = data_dir
        self.table = str(table).lower()
        self.logger = logger
        self.storage_log = storage_log
        self.path = os.path.join(data_dir, self.table + OVERFLOW_FILE_SUFFIX)
        self._lock = threading.RLock()

    def _log(self, operation, page_id=None, **details):
        if self.storage_log is not None:
            return self.storage_log.append(
                operation, table=self.table, page_id=page_id, **details
            )
        return None

    def _create(self):
        header = bytearray(PAGE_SIZE)
        header[FILE_MAGIC_OFFSET:FILE_MAGIC_OFFSET + 4] = OVERFLOW_MAGIC
        struct.pack_into("<i", header, OVERFLOW_PAGE_COUNT_OFFSET, 1)
        struct.pack_into("<i", header, OVERFLOW_FREE_LIST_OFFSET, -1)
        struct.pack_into("<i", header, OVERFLOW_NEXT_RECORD_OFFSET, 1)
        with open(self.path, "wb") as fp:
            fp.write(header)
            fp.flush()
        return header

    def _read_header(self):
        if not os.path.exists(self.path):
            return bytearray(self._create())
        with open(self.path, "rb") as fp:
            header = fp.read(PAGE_SIZE)
        if len(header) != PAGE_SIZE or header[:4] != OVERFLOW_MAGIC:
            raise OverflowCorruptionError(self.table, "文件头长度或魔数无效", 0)
        page_count = struct.unpack_from("<i", header, OVERFLOW_PAGE_COUNT_OFFSET)[0]
        if page_count < 1 or os.path.getsize(self.path) < page_count * PAGE_SIZE:
            raise OverflowCorruptionError(self.table, "文件大小与页数不一致", 0)
        return bytearray(header)

    def _write_header(self, header):
        with open(self.path, "r+b") as fp:
            fp.seek(0)
            fp.write(header)
            fp.flush()

    def _read_page(self, page_id):
        header = self._read_header()
        count = struct.unpack_from("<i", header, OVERFLOW_PAGE_COUNT_OFFSET)[0]
        if page_id < 1 or page_id >= count:
            raise OverflowCorruptionError(self.table, "页号越界", page_id)
        with open(self.path, "rb") as fp:
            fp.seek(page_id * PAGE_SIZE)
            data = fp.read(PAGE_SIZE)
        try:
            return OverflowPage(page_id, data)
        except (ValueError, OverflowCorruptionError) as error:
            raise OverflowCorruptionError(self.table, str(error), page_id) from error

    def _write_page(self, page):
        page.validate()
        with open(self.path, "r+b") as fp:
            fp.seek(page.page_id * PAGE_SIZE)
            fp.write(page.to_bytes())

    def _allocate_page(self, header):
        free_head = struct.unpack_from("<i", header, OVERFLOW_FREE_LIST_OFFSET)[0]
        if free_head != -1:
            page = self._read_page(free_head)
            next_free = page.next_page_id
            struct.pack_into("<i", header, OVERFLOW_FREE_LIST_OFFSET, next_free)
            self._write_header(header)
            return OverflowPage(free_head)
        page_count = struct.unpack_from("<i", header, OVERFLOW_PAGE_COUNT_OFFSET)[0]
        page = OverflowPage(page_count)
        struct.pack_into("<i", header, OVERFLOW_PAGE_COUNT_OFFSET, page_count + 1)
        self._write_header(header)
        with open(self.path, "r+b") as fp:
            fp.seek(page_count * PAGE_SIZE)
            fp.write(page.to_bytes())
        return page

    def write(self, data, replace=None):
        """Write bytes as a chained overflow value and return ``OverflowRef``."""
        data = bytes(data)
        if not data:
            raise ValueError("溢出记录不能为空")
        with self._lock:
            header = self._read_header()
            record_id = struct.unpack_from("<i", header, OVERFLOW_NEXT_RECORD_OFFSET)[0]
            struct.pack_into("<i", header, OVERFLOW_NEXT_RECORD_OFFSET, record_id + 1)
            self._write_header(header)
            pages = []
            for offset in range(0, len(data), OVERFLOW_CHUNK_SIZE):
                page = self._allocate_page(header)
                page.chunk = data[offset:offset + OVERFLOW_CHUNK_SIZE]
                page.record_id = record_id
                pages.append(page)
            for current, following in zip(pages, pages[1:] + [None]):
                current.next_page_id = following.page_id if following else -1
                self._write_page(current)
            ref = OverflowRef(pages[0].page_id, len(data), record_id)
            self._log("OVERFLOW_WRITE", pages[0].page_id,
                      length=len(data), pages=len(pages), record_id=record_id)
            # 先完整写入新链再释放旧链；新写入失败时旧引用仍然有效。
            if replace is not None:
                self.free(replace)
            return ref

    def read(self, ref):
        ref = self._coerce_ref(ref)
        if ref.length < 1:
            raise ValueError("溢出记录长度必须大于 0")
        with self._lock:
            parts = []
            bytes_read = 0
            page_id = ref.page_id
            seen = set()
            while page_id != -1 and bytes_read < ref.length:
                if page_id in seen:
                    raise OverflowCorruptionError(self.table, "溢出页链存在环", page_id)
                seen.add(page_id)
                page = self._read_page(page_id)
                if page.record_id != ref.record_id:
                    raise OverflowCorruptionError(
                        self.table,
                        "记录编号为 %d，引用要求 %d"
                        % (page.record_id, ref.record_id),
                        page_id,
                    )
                parts.append(page.chunk)
                bytes_read += page.chunk_length
                page_id = page.next_page_id
            result = b"".join(parts)
            if len(result) < ref.length:
                raise OverflowCorruptionError(self.table, "溢出链长度不足", ref.page_id)
            return result[:ref.length]

    def free(self, ref):
        ref = self._coerce_ref(ref)
        with self._lock:
            header = self._read_header()
            page_id = ref.page_id
            seen = set()
            pages = []
            while page_id != -1 and page_id not in seen:
                seen.add(page_id)
                page = self._read_page(page_id)
                if page.record_id != ref.record_id:
                    raise OverflowCorruptionError(
                        self.table, "溢出引用已经失效", page_id
                    )
                pages.append(page_id)
                page_id = page.next_page_id
            if page_id in seen:
                raise OverflowCorruptionError(self.table, "溢出页链存在环", page_id)
            free_head = struct.unpack_from("<i", header, OVERFLOW_FREE_LIST_OFFSET)[0]
            for page_id in pages:
                page = OverflowPage(page_id)
                page.next_page_id = free_head
                self._write_page(page)
                free_head = page_id
            struct.pack_into("<i", header, OVERFLOW_FREE_LIST_OFFSET, free_head)
            self._write_header(header)
            self._log("OVERFLOW_FREE", ref.page_id, pages=len(pages), length=ref.length)
            return len(pages)

    def stats(self):
        with self._lock:
            if not os.path.exists(self.path):
                return {"table": self.table, "pages": 0, "free_pages": 0,
                        "used_pages": 0, "chunk_size": OVERFLOW_CHUNK_SIZE}
            header = self._read_header()
            total = struct.unpack_from("<i", header, OVERFLOW_PAGE_COUNT_OFFSET)[0] - 1
            free = 0
            page_id = struct.unpack_from("<i", header, OVERFLOW_FREE_LIST_OFFSET)[0]
            seen = set()
            while page_id != -1:
                if page_id in seen:
                    raise OverflowCorruptionError(self.table, "空闲链存在环", page_id)
                seen.add(page_id)
                free += 1
                page_id = self._read_page(page_id).next_page_id
            return {"table": self.table, "pages": total, "free_pages": free,
                    "used_pages": total - free, "chunk_size": OVERFLOW_CHUNK_SIZE}

    @staticmethod
    def _coerce_ref(ref):
        if isinstance(ref, OverflowRef):
            return ref
        if isinstance(ref, dict):
            return OverflowRef(
                int(ref["page_id"]), int(ref["length"]), int(ref["record_id"])
            )
        try:
            page_id, length, record_id = ref
            return OverflowRef(int(page_id), int(length), int(record_id))
        except (TypeError, ValueError) as error:
            raise TypeError(
                "溢出引用必须是 OverflowRef 或 (page_id, length, record_id)"
            ) from error
