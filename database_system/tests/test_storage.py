# -*- coding: utf-8 -*-
"""存储系统测试：页式存储、文件管理、缓存管理。"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.storage.buffer import BufferPool  # noqa: E402
from database_system.storage.file_manager import FileManager  # noqa: E402
from database_system.storage.page import Page  # noqa: E402
from database_system.utils.constants import PAGE_SIZE  # noqa: E402


class TestPage(unittest.TestCase):
    def test_insert_and_read(self):
        page = Page(1)
        slot_a = page.insert_record(b"hello")
        slot_b = page.insert_record(b"world")
        self.assertEqual(slot_a, 0)
        self.assertEqual(slot_b, 1)
        self.assertEqual(page.get_record(slot_a), b"hello")
        self.assertEqual(page.num_records, 2)

    def test_delete(self):
        page = Page(1)
        slot = page.insert_record(b"data")
        self.assertTrue(page.delete_record(slot))
        self.assertIsNone(page.get_record(slot))
        self.assertEqual(page.num_records, 0)

    def test_update_in_place(self):
        page = Page(1)
        slot = page.insert_record(b"aaaa")
        self.assertTrue(page.update_record(slot, b"bb"))
        self.assertEqual(page.get_record(slot), b"bb")

    def test_update_larger_fails(self):
        page = Page(1)
        slot = page.insert_record(b"aa")
        self.assertFalse(page.update_record(slot, b"aaaaaa"))

    def test_free_space_decreases(self):
        page = Page(1)
        before = page.free_space
        page.insert_record(b"x" * 100)
        self.assertLess(page.free_space, before)

    def test_full_page_returns_none(self):
        page = Page(1)
        payload = b"y" * 200
        count = 0
        while page.insert_record(payload) is not None:
            count += 1
            if count > PAGE_SIZE:
                break
        self.assertGreater(count, 10)
        self.assertIsNone(page.insert_record(payload))

    def test_serialization_round_trip(self):
        page = Page(7)
        page.insert_record(b"abc")
        page.next_page_id = 9
        restored = Page(7, page.to_bytes())
        self.assertEqual(restored.page_id, 7)
        self.assertEqual(restored.get_record(0), b"abc")
        self.assertEqual(restored.next_page_id, 9)


class TestFileManager(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fm = FileManager(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_create_and_drop(self):
        self.fm.create_file("t")
        self.assertTrue(self.fm.table_exists("t"))
        self.assertEqual(self.fm.num_pages("t"), 1)      # 仅头页
        self.fm.drop_file("t")
        self.assertFalse(self.fm.table_exists("t"))

    def test_allocate_and_link_pages(self):
        self.fm.create_file("t")
        p1 = self.fm.allocate_page("t")
        p2 = self.fm.allocate_page("t")
        self.assertEqual(p1.page_id, 1)
        self.assertEqual(p2.page_id, 2)
        # 链表关系写入了磁盘，需重新读页校验
        self.assertEqual(self.fm.read_page("t", 1).next_page_id, 2)
        self.assertEqual(self.fm.read_page("t", 2).prev_page_id, 1)
        self.assertEqual(self.fm.last_page_id("t"), 2)
        self.assertEqual(list(self.fm.data_page_ids("t")), [1, 2])

    def test_free_page_reused(self):
        self.fm.create_file("t")
        self.fm.allocate_page("t")
        second = self.fm.allocate_page("t")
        self.fm.free_page("t", second.page_id)
        reused = self.fm.allocate_page("t")
        self.assertEqual(reused.page_id, second.page_id)

    def test_write_read_page(self):
        self.fm.create_file("t")
        page = self.fm.allocate_page("t")
        page.insert_record(b"persisted")
        self.fm.write_page("t", page)
        reread = self.fm.read_page("t", page.page_id)
        self.assertEqual(reread.get_record(0), b"persisted")


class TestBufferPool(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fm = FileManager(self.dir)
        self.fm.create_file("t")
        for _ in range(6):
            self.fm.allocate_page("t")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_hit_and_miss(self):
        pool = BufferPool(self.fm, pool_size=4)
        pool.fetch_page("t", 1)
        pool.unpin_page("t", 1)
        pool.fetch_page("t", 1)
        pool.unpin_page("t", 1)
        stats = pool.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["disk_reads"], 1)

    def test_lru_eviction(self):
        pool = BufferPool(self.fm, pool_size=2, strategy="LRU")
        pool.fetch_page("t", 1)
        pool.unpin_page("t", 1)
        pool.fetch_page("t", 2)
        pool.unpin_page("t", 2)
        pool.fetch_page("t", 1)          # 页 1 变为最近使用
        pool.unpin_page("t", 1)
        pool.fetch_page("t", 3)          # 应淘汰页 2
        pool.unpin_page("t", 3)
        self.assertFalse(("t", 2) in pool._frames)
        self.assertTrue(("t", 1) in pool._frames)
        self.assertEqual(pool.stats()["evictions"], 1)

    def test_fifo_eviction(self):
        pool = BufferPool(self.fm, pool_size=2, strategy="FIFO")
        pool.fetch_page("t", 1)
        pool.unpin_page("t", 1)
        pool.fetch_page("t", 2)
        pool.unpin_page("t", 2)
        pool.fetch_page("t", 1)          # 再次访问不改变 FIFO 顺序
        pool.unpin_page("t", 1)
        pool.fetch_page("t", 3)          # FIFO 淘汰最先装入的页 1
        pool.unpin_page("t", 3)
        self.assertFalse(("t", 1) in pool._frames)
        self.assertTrue(("t", 2) in pool._frames)

    def test_dirty_page_flushed(self):
        pool = BufferPool(self.fm, pool_size=1)
        page = pool.fetch_page("t", 1)
        page.insert_record(b"dirty")
        pool.unpin_page("t", 1, dirty=True)
        pool.fetch_page("t", 2)          # 触发淘汰，脏页需写回
        pool.unpin_page("t", 2)
        self.assertEqual(pool.stats()["disk_writes"], 1)
        self.assertEqual(self.fm.read_page("t", 1).get_record(0), b"dirty")

    def test_full_pool_error(self):
        pool = BufferPool(self.fm, pool_size=1)
        pool.fetch_page("t", 1)          # 保持 pin 状态
        with self.assertRaises(Exception):
            pool.fetch_page("t", 2)

    def test_discard_table(self):
        pool = BufferPool(self.fm, pool_size=4)
        pool.fetch_page("t", 1)
        pool.unpin_page("t", 1)
        self.assertEqual(pool.discard_table("t"), 1)
        self.assertEqual(len(pool), 0)


if __name__ == "__main__":
    unittest.main()
