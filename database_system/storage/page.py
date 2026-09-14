# -*- coding: utf-8 -*-
"""页式存储实现。

页布局（PAGE_SIZE = 4096 字节）：

    +-----------------+-------------------------------------------+-----------+
    | 页头 (20B)      | 槽数组 (从前往后生长)                      |  空闲空间 |
    +-----------------+-------------------------------------------+-----------+
                                                       记录区从页尾向前生长

槽（slot）：每项 4 字节，记录 (offset, length)；length == 0 表示该槽已删除。
删除和缩短记录产生的空洞会在空间不足时自动合并，也可显式调用 compact()。
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


class PageCorruptionError(IOError):
    """页格式损坏，并携带可直接用于定位的页号和槽号。"""

    def __init__(self, page_id, message, slot_id=None):
        self.page_id = page_id
        self.slot_id = slot_id
        location = "页=%d" % page_id
        if slot_id is not None:
            location += " 槽=%d" % slot_id
        super().__init__("页数据损坏（%s）：%s" % (location, message))


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
            self.validate(expected_page_id=page_id)

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
        """槽数组与记录区之间当前连续可用的空间（字节）。"""
        return self.free_end - (PAGE_HEADER_SIZE + self.num_slots * SLOT_SIZE)

    @property
    def fragmented_space(self):
        """删除或缩短记录后，记录区内可通过 compact() 回收的字节数。"""
        used = sum(length for _offset, length in self._slot_table() if length > 0)
        return PAGE_SIZE - self.free_end - used

    @property
    def reclaimable_space(self):
        """fragmented_space 的语义化别名。"""
        return self.fragmented_space

    @property
    def total_free_space(self):
        """整理后可用空间；不重复计算已删除槽所占的槽目录。"""
        return self.free_space + self.fragmented_space

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

    def validate(self, expected_page_id=None):
        """校验页头、槽边界和记录区，损坏时精确报告页/槽。"""
        page_id = self.page_id
        if expected_page_id is not None and page_id != expected_page_id:
            raise PageCorruptionError(
                expected_page_id,
                "页头中的页号为 %d" % page_id,
            )

        slot_end = PAGE_HEADER_SIZE + self.num_slots * SLOT_SIZE
        if slot_end > PAGE_SIZE:
            raise PageCorruptionError(page_id, "槽目录越过页边界")
        if self.free_end < slot_end or self.free_end > PAGE_SIZE:
            raise PageCorruptionError(
                page_id,
                "free_end=%d 不在合法区间 [%d, %d]"
                % (self.free_end, slot_end, PAGE_SIZE),
            )

        ranges = []
        for slot_id, (offset, length) in enumerate(self._slot_table()):
            if length == 0:
                if offset != 0:
                    raise PageCorruptionError(page_id, "删除槽偏移量不为 0", slot_id)
                continue
            if offset < self.free_end or offset + length > PAGE_SIZE:
                raise PageCorruptionError(
                    page_id,
                    "记录范围 [%d, %d) 越过记录区 [%d, %d)"
                    % (offset, offset + length, self.free_end, PAGE_SIZE),
                    slot_id,
                )
            ranges.append((offset, offset + length, slot_id))

        ranges.sort()
        for previous, current in zip(ranges, ranges[1:]):
            if previous[1] > current[0]:
                raise PageCorruptionError(
                    page_id,
                    "记录与槽 %d 的范围重叠" % previous[2],
                    current[2],
                )
        return True

    def compact(self):
        """合并页内空洞并修正全部有效槽的 offset，槽号保持不变。

        返回被回收的碎片字节数。数据先复制到临时列表，因此即使记录的
        新旧区域重叠也不会互相覆盖。
        """
        reclaimed = self.fragmented_space
        records = [
            (slot_id, self.get_record(slot_id))
            for slot_id, (_offset, length) in enumerate(self._slot_table())
            if length > 0
        ]
        directory_end = PAGE_HEADER_SIZE + self.num_slots * SLOT_SIZE
        self._data[directory_end:PAGE_SIZE] = b"\x00" * (PAGE_SIZE - directory_end)

        write_end = PAGE_SIZE
        for slot_id, data in records:
            write_end -= len(data)
            self._data[write_end:write_end + len(data)] = data
            struct.pack_into(
                "<HH", self._data, self._slot_offset(slot_id), write_end, len(data)
            )
        struct.pack_into("<H", self._data, FREE_END_OFFSET, write_end)
        return reclaimed

    def get_record(self, slot_id):
        """读取指定槽的记录字节串；槽不存在或已删除返回 None。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return None
        offset, length = struct.unpack_from("<HH", self._data, self._slot_offset(slot_id))
        if length == 0:
            return None
        return bytes(self._data[offset:offset + length])

    def insert_record(self, data):
        """插入记录；优先复用删除槽，并在需要时自动合并页内空洞。"""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("记录必须是 bytes-like 对象")
        data = bytes(data)
        size = len(data)
        if size == 0:
            raise ValueError("记录不能为空（长度 0 保留为删除槽标记）")

        reusable_slot = next(
            (slot_id for slot_id, (_offset, length) in enumerate(self._slot_table())
             if length == 0),
            None,
        )
        directory_cost = 0 if reusable_slot is not None else SLOT_SIZE
        required = size + directory_cost
        if required > self.free_space and required <= self.total_free_space:
            self.compact()
        if required > self.free_space:
            return None

        new_free_end = self.free_end - size
        self._data[new_free_end:self.free_end] = data
        struct.pack_into("<H", self._data, FREE_END_OFFSET, new_free_end)

        if reusable_slot is not None:
            slot_id = reusable_slot
        else:
            slot_id = self.num_slots
            struct.pack_into("<H", self._data, NUM_SLOTS_OFFSET, slot_id + 1)
        struct.pack_into("<HH", self._data, self._slot_offset(slot_id), new_free_end, size)
        return slot_id

    def update_record(self, slot_id, data):
        """原地更新记录。新记录更长（空间不足）时返回 False。"""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("记录必须是 bytes-like 对象")
        data = bytes(data)
        if not data:
            raise ValueError("记录不能为空（请使用 delete_record 删除记录）")
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
        return ("Page(id=%d, slots=%d, records=%d, free=%dB, fragmented=%dB)"
                % (self.page_id, self.num_slots, self.num_records,
                   self.free_space, self.fragmented_space))
