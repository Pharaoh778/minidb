# -*- coding: utf-8 -*-
"""缓存管理：缓冲区（Buffer Pool），支持 LRU / FIFO 淘汰策略。

提供命中统计与运行日志，用于观察缓存效率。
"""

import itertools

from ..utils.constants import DEFAULT_POOL_SIZE, DEFAULT_STRATEGY


class BufferPoolError(Exception):
    """缓冲池异常。"""


class _Frame:
    """缓冲帧。"""

    __slots__ = ("key", "page", "pin_count", "dirty", "load_order", "last_access")

    def __init__(self, key, page, load_order):
        self.key = key                    # (table, page_id)
        self.page = page
        self.pin_count = 0
        self.dirty = False
        self.load_order = load_order      # FIFO 依据：装入顺序
        self.last_access = 0              # LRU 依据：最近访问序号

    def __repr__(self):
        return "Frame(%s, pin=%d, dirty=%s)" % (self.page, self.pin_count, self.dirty)


class BufferPool:
    """页级缓冲池。"""

    def __init__(self, file_manager, pool_size=DEFAULT_POOL_SIZE,
                 strategy=DEFAULT_STRATEGY, logger=None):
        if pool_size < 1:
            raise ValueError("缓冲池容量至少为 1")
        strategy = str(strategy).upper()
        if strategy not in ("LRU", "FIFO"):
            raise ValueError("不支持的淘汰策略：%s（可选 LRU / FIFO）" % strategy)

        self.file_manager = file_manager
        self.pool_size = pool_size
        self.strategy = strategy
        self.logger = logger
        self._frames = {}                 # (table, page_id) -> _Frame
        self._clock = itertools.count()
        self._order = itertools.count()
        self._reset_stats()

    # ---------------- 统计 ----------------
    def _reset_stats(self):
        self._hits = 0
        self._misses = 0
        self._disk_reads = 0
        self._disk_writes = 0
        self._evictions = 0

    def stats(self):
        total = self._hits + self._misses
        return {
            "strategy": self.strategy,
            "pool_size": self.pool_size,
            "cached": len(self._frames),
            "hits": self._hits,
            "misses": self._misses,
            "disk_reads": self._disk_reads,
            "disk_writes": self._disk_writes,
            "evictions": self._evictions,
            "hit_rate": (self._hits / total) if total else 0.0,
        }

    def reset_stats(self):
        self._reset_stats()

    def log_stats(self):
        """把统计信息写入日志。"""
        s = self.stats()
        if self.logger:
            self.logger.info(
                "缓冲池[%s] 命中=%d 未命中=%d 命中率=%.1f%% 磁盘读=%d 磁盘写=%d 淘汰=%d",
                s["strategy"], s["hits"], s["misses"], s["hit_rate"] * 100,
                s["disk_reads"], s["disk_writes"], s["evictions"],
            )
        return s

    # ---------------- 核心接口 ----------------
    def _key(self, table, page_id):
        return (table, page_id)

    def fetch_page(self, table, page_id):
        """取一页到缓冲池并 pin 住；不存在则从磁盘加载。"""
        key = self._key(table, page_id)
        frame = self._frames.get(key)
        if frame is not None:
            self._hits += 1
            frame.last_access = next(self._clock)
            frame.pin_count += 1
            return frame.page

        self._misses += 1
        if len(self._frames) >= self.pool_size:
            self._evict()
        page = self.file_manager.read_page(table, page_id)
        self._disk_reads += 1
        frame = _Frame(key, page, next(self._order))
        frame.last_access = next(self._clock)
        frame.pin_count = 1
        self._frames[key] = frame
        if self.logger:
            self.logger.debug("加载页到缓冲池：%s 页号=%d", table, page_id)
        return frame.page

    def pin_page(self, table, page_id):
        key = self._key(table, page_id)
        frame = self._frames.get(key)
        if frame is None:
            return False
        frame.pin_count += 1
        frame.last_access = next(self._clock)
        return True

    def unpin_page(self, table, page_id, dirty=False):
        """解除 pin；dirty=True 表示页被修改过（需要刷盘）。"""
        key = self._key(table, page_id)
        frame = self._frames.get(key)
        if frame is None:
            return False
        frame.pin_count = max(0, frame.pin_count - 1)
        frame.dirty = frame.dirty or dirty
        return True

    def mark_dirty(self, table, page_id):
        key = self._key(table, page_id)
        frame = self._frames.get(key)
        if frame is None:
            return False
        frame.dirty = True
        return True

    def flush_page(self, table, page_id):
        key = self._key(table, page_id)
        frame = self._frames.get(key)
        if frame is None or not frame.dirty:
            return False
        self.file_manager.write_page(table, frame.page)
        self._disk_writes += 1
        frame.dirty = False
        return True

    def flush_all(self):
        """把所有脏页刷回磁盘。"""
        count = 0
        for (table, page_id), frame in list(self._frames.items()):
            if frame.dirty:
                self.file_manager.write_page(table, frame.page)
                self._disk_writes += 1
                frame.dirty = False
                count += 1
        if count and self.logger:
            self.logger.debug("刷回脏页：%d 页", count)
        return count

    def discard_table(self, table):
        """丢弃某张表在缓冲池中的全部页（用于 DROP TABLE）。"""
        keys = [key for key in self._frames if key[0] == table]
        for key in keys:
            del self._frames[key]
        return len(keys)

    def clear(self):
        """清空缓冲池（丢弃未刷盘数据前请先 flush_all）。"""
        self.flush_all()
        self._frames.clear()

    # ---------------- 淘汰 ----------------
    def _select_victim(self):
        candidates = [f for f in self._frames.values() if f.pin_count == 0]
        if not candidates:
            raise BufferPoolError("缓冲池已满且所有页都被 pin 住，无法淘汰")
        if self.strategy == "LRU":
            return min(candidates, key=lambda f: f.last_access)
        return min(candidates, key=lambda f: f.load_order)

    def _evict(self):
        victim = self._select_victim()
        table, page_id = victim.key
        if victim.dirty:
            self.file_manager.write_page(table, victim.page)
            self._disk_writes += 1
        del self._frames[victim.key]
        self._evictions += 1
        if self.logger:
            self.logger.debug("淘汰页：%s 页号=%d（策略=%s）", table, page_id, self.strategy)

    def __len__(self):
        return len(self._frames)
