# -*- coding: utf-8 -*-
"""文件管理：把一个表映射为一个物理数据文件。

文件布局：
    第 0 页为文件头页，保存魔数、总页数、空闲页链表头、最后一个数据页页号；
    第 1 页及之后为数据页，数据页之间通过 prev / next 组成双向链表。
"""

import os
import struct

from ..utils.constants import (
    FILE_FREE_LIST_OFFSET,
    FILE_LAST_PAGE_OFFSET,
    FILE_MAGIC,
    FILE_MAGIC_OFFSET,
    FILE_PAGE_COUNT_OFFSET,
    PAGE_SIZE,
)
from ..utils.helpers import ensure_dir
from .page import Page

HEADER_PAGE_ID = 0
FIRST_DATA_PAGE_ID = 1


class FileManager:
    """负责 .dat 文件的创建、删除、页读写与页分配。"""

    def __init__(self, data_dir, logger=None):
        self.data_dir = ensure_dir(data_dir)
        self.logger = logger

    # ---------------- 基础路径 ----------------
    def _path(self, table):
        return os.path.join(self.data_dir, "%s.dat" % table)

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
        return path

    def drop_file(self, table):
        path = self._path(table)
        if os.path.exists(path):
            os.remove(path)
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
    def _read_header(self, table):
        with open(self._path(table), "rb") as fp:
            fp.seek(HEADER_PAGE_ID * PAGE_SIZE)
            data = fp.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE or data[:4] != FILE_MAGIC:
            raise IOError("文件头损坏：%s" % table)
        return bytearray(data)

    def _write_header(self, table, header):
        with open(self._path(table), "r+b") as fp:
            fp.seek(HEADER_PAGE_ID * PAGE_SIZE)
            fp.write(header)

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
        """从磁盘读取一页。"""
        with open(self._path(table), "rb") as fp:
            fp.seek(page_id * PAGE_SIZE)
            data = fp.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise IOError("读取页失败：表=%s 页号=%d" % (table, page_id))
        return Page(page_id, data)

    def write_page(self, table, page):
        """把一页写回磁盘。"""
        with open(self._path(table), "r+b") as fp:
            fp.seek(page.page_id * PAGE_SIZE)
            fp.write(page.to_bytes())

    # ---------------- 页分配 / 回收 ----------------
    def allocate_page(self, table):
        """分配一个可用的数据页：优先复用空闲链表，否则追加新页。"""
        header = self._read_header(table)
        free_head = struct.unpack_from("<i", header, FILE_FREE_LIST_OFFSET)[0]

        if free_head != -1:
            page = self.read_page(table, free_head)
            next_free = page.next_page_id               # 空闲页复用 next 字段作为链表指针
            page.next_page_id = -1
            page.prev_page_id = -1
            struct.pack_into("<i", header, FILE_FREE_LIST_OFFSET, next_free)
            self._write_header(table, header)
            self.write_page(table, page)
            return page

        page_count = struct.unpack_from("<i", header, FILE_PAGE_COUNT_OFFSET)[0]
        last_page = struct.unpack_from("<i", header, FILE_LAST_PAGE_OFFSET)[0]
        page_id = page_count
        page = Page(page_id)
        page.prev_page_id = last_page

        if last_page != -1:
            prev = self.read_page(table, last_page)
            prev.next_page_id = page_id
            self.write_page(table, prev)

        struct.pack_into("<i", header, FILE_PAGE_COUNT_OFFSET, page_count + 1)
        struct.pack_into("<i", header, FILE_LAST_PAGE_OFFSET, page_id)
        self._write_header(table, header)
        self.write_page(table, page)
        return page

    def free_page(self, table, page_id):
        """回收数据页，加入空闲链表。"""
        header = self._read_header(table)
        free_head = struct.unpack_from("<i", header, FILE_FREE_LIST_OFFSET)[0]
        page = Page(page_id)                    # 清空后的空页
        page.next_page_id = free_head
        self.write_page(table, page)
        struct.pack_into("<i", header, FILE_FREE_LIST_OFFSET, page_id)
        self._write_header(table, header)
