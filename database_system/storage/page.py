# -*- coding: utf-8 -*-
"""页式存储实现。

页布局（PAGE_SIZE = 4096 字节）：

    +-----------------+-------------------------------------------+-----------+
    | 页头 (20B)      | 槽数组 (从前往后生长)                      |  空闲空间 |
    +-----------------+-------------------------------------------+-----------+
                                                       记录区从页尾向前生长

槽（slot）：每项 4 字节，记录 (offset, length)；length == 0 表示该槽已删除。
"""

import struct

from ..utils.constants import (
    FREE_END_OFFSET,
    NEXT_PAGE_OFFSET,
    NUM_SLOTS_OFFSET,
    PAGE_HEADER_SIZE,
    PAGE_ID_OFFSET,
    PAGE_SIZE,
    PREV_PAGE_OFFSET,
    RESERVED_OFFSET,
    SLOT_SIZE,
)


class Page:
    """一个固定大小的物理页。"""

    def __init__(self, page_id, data=None):
        if data is None:
            buf = bytearray(PAGE_SIZE)
            struct.pack_into("<i", buf, PAGE_ID_OFFSET, page_id)
            struct.pack_into("<H", buf, NUM_SLOTS_OFFSET, 0)
            struct.pack_into("<H", buf, FREE_END_OFFSET, PAGE_SIZE)
            struct.pack_into("<i", buf, NEXT_PAGE_OFFSET, -1)
            struct.pack_into("<i", buf, PREV_PAGE_OFFSET, -1)
            struct.pack_into("<i", buf, RESERVED_OFFSET, 0)
            self._data = buf
        else:
            if len(data) != PAGE_SIZE:
                raise ValueError("页数据长度必须为 %d，收到 %d" % (PAGE_SIZE, len(data)))
            self._data = bytearray(data)

    # ---------------- 页头属性 ----------------
    @property
    def page_id(self):
        return struct.unpack_from("<i", self._data, PAGE_ID_OFFSET)[0]

    @page_id.setter
    def page_id(self, value):
        struct.pack_into("<i", self._data, PAGE_ID_OFFSET, value)

    @property
    def num_slots(self):
        return struct.unpack_from("<H", self._data, NUM_SLOTS_OFFSET)[0]

    @property
    def free_end(self):
        return struct.unpack_from("<H", self._data, FREE_END_OFFSET)[0]

    @property
    def next_page_id(self):
        return struct.unpack_from("<i", self._data, NEXT_PAGE_OFFSET)[0]

    @next_page_id.setter
    def next_page_id(self, value):
        struct.pack_into("<i", self._data, NEXT_PAGE_OFFSET, value)

    @property
    def prev_page_id(self):
        return struct.unpack_from("<i", self._data, PREV_PAGE_OFFSET)[0]

    @prev_page_id.setter
    def prev_page_id(self, value):
        struct.pack_into("<i", self._data, PREV_PAGE_OFFSET, value)

    @property
    def free_space(self):
        """当前可用空间（字节）。"""
        return self.free_end - (PAGE_HEADER_SIZE + self.num_slots * SLOT_SIZE)

    @property
    def num_records(self):
        """有效记录数（不含已删除槽）。"""
        return sum(1 for _, length in self._slot_table() if length > 0)

    def to_bytes(self):
        return bytes(self._data)

    # ---------------- 槽操作 ----------------
    def _slot_offset(self, slot_id):
        return PAGE_HEADER_SIZE + slot_id * SLOT_SIZE

    def _slot_table(self):
        table = []
        for i in range(self.num_slots):
            off, length = struct.unpack_from("<HH", self._data, self._slot_offset(i))
            table.append((off, length))
        return table

    def get_record(self, slot_id):
        """读取指定槽的记录字节串；槽不存在或已删除返回 None。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return None
        offset, length = struct.unpack_from("<HH", self._data, self._slot_offset(slot_id))
        if length == 0:
            return None
        return bytes(self._data[offset:offset + length])

    def insert_record(self, data):
        """插入一条记录，成功返回 slot_id，空间不足返回 None。"""
        size = len(data)
        if size + SLOT_SIZE > self.free_space:
            return None
        new_free_end = self.free_end - size
        self._data[new_free_end:self.free_end] = data
        struct.pack_into("<H", self._data, FREE_END_OFFSET, new_free_end)

        slot_id = self.num_slots
        struct.pack_into("<HH", self._data, self._slot_offset(slot_id), new_free_end, size)
        struct.pack_into("<H", self._data, NUM_SLOTS_OFFSET, slot_id + 1)
        return slot_id

    def update_record(self, slot_id, data):
        """原地更新记录。新记录更长（空间不足）时返回 False。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return False
        offset, length = struct.unpack_from("<HH", self._data, self._slot_offset(slot_id))
        if length == 0:
            return False
        if len(data) > length:
            return False
        self._data[offset:offset + len(data)] = data
        struct.pack_into("<HH", self._data, self._slot_offset(slot_id), offset, len(data))
        return True

    def delete_record(self, slot_id):
        """删除（标记）指定槽的记录。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return False
        offset, length = struct.unpack_from("<HH", self._data, self._slot_offset(slot_id))
        if length == 0:
            return False
        struct.pack_into("<HH", self._data, self._slot_offset(slot_id), 0, 0)
        return True

    def records(self):
        """遍历所有有效记录，产出 (slot_id, bytes)。"""
        for slot_id, (_offset, length) in enumerate(self._slot_table()):
            if length > 0:
                yield slot_id, self.get_record(slot_id)

    def __repr__(self):
        return ("Page(id=%d, slots=%d, records=%d, free=%dB)"
                % (self.page_id, self.num_slots, self.num_records, self.free_space))
