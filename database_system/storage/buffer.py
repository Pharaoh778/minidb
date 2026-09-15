# -*- coding: utf-8 -*-
"""线程安全的页级缓冲池。

支持 LRU/FIFO、顺序预读、可选后台刷盘、按表软保留/硬上限分区，以及
FileManager 直接修改文件拓扑时的脏页同步与缓存失效。
"""

import itertools
import threading

from ..utils.constants import (
    DEFAULT_BACKGROUND_FLUSH_INTERVAL,
    DEFAULT_POOL_SIZE,
    DEFAULT_PREFETCH_PAGES,
    DEFAULT_STRATEGY,
)


class BufferPoolError(Exception):
    """缓冲池异常。"""


class _Frame:
    """缓冲帧。"""

    __slots__ = (
        "key", "page", "pin_count", "dirty", "load_order", "last_access",
        "prefetched",
    )

    def __init__(self, key, page, load_order, prefetched=False):
        self.key = key
        self.page = page
        self.pin_count = 0
        self.dirty = False
        self.load_order = load_order
        self.last_access = 0
        self.prefetched = prefetched

    def __repr__(self):
        return "Frame(%s, pin=%d, dirty=%s, prefetched=%s)" % (
            self.page, self.pin_count, self.dirty, self.prefetched
        )


class BufferPool:
    """页级缓冲池。

    新参数均位于旧参数之后，现有调用保持兼容：

    * ``prefetch_pages``：一次顺序预读的后续页数，默认 0（关闭）。
    * ``flush_interval``：后台刷盘间隔秒数，默认 0（关闭）。
    * ``partitions``：按表分区配置，值可为最大页数、``(min, max)``，
      或 ``{"min": ..., "max": ...}``。
    """

    def __init__(self, file_manager, pool_size=DEFAULT_POOL_SIZE,
                 strategy=DEFAULT_STRATEGY, logger=None,
                 prefetch_pages=DEFAULT_PREFETCH_PAGES,
                 flush_interval=DEFAULT_BACKGROUND_FLUSH_INTERVAL,
                 partitions=None):
        if not isinstance(pool_size, int) or isinstance(pool_size, bool) or pool_size < 1:
            raise ValueError("缓冲池容量至少为 1")
        strategy = str(strategy).upper()
        if strategy not in ("LRU", "FIFO"):
            raise ValueError("不支持的淘汰策略：%s（可选 LRU / FIFO）" % strategy)
        if (not isinstance(prefetch_pages, int) or isinstance(prefetch_pages, bool)
                or prefetch_pages < 0):
            raise ValueError("预读页数必须是非负整数")

        self.file_manager = file_manager
        self.pool_size = pool_size
        self.strategy = strategy
        self.logger = logger
        self.prefetch_count = prefetch_pages
        self._frames = {}
        self._header_cache = {}
        self._partitions = {}
        self._clock = itertools.count()
        self._order = itertools.count()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._flush_thread = None
        self._flush_interval = 0.0
        self._closed = False
        self._reset_stats()

        if partitions:
            for table, config in partitions.items():
                if isinstance(config, dict):
                    self.configure_partition(
                        table, config.get("min", 0), config.get("max")
                    )
                elif isinstance(config, (tuple, list)):
                    if len(config) != 2:
                        raise ValueError("分区元组必须是 (min_pages, max_pages)")
                    self.configure_partition(table, config[0], config[1])
                else:
                    self.configure_partition(table, 0, config)

        register = getattr(file_manager, "register_buffer_pool", None)
        if register is not None:
            register(self)
        if flush_interval:
            self.start_background_flush(flush_interval)

    # ---------------- 统计 ----------------
    def _reset_stats(self):
        self._hits = 0
        self._misses = 0
        self._disk_reads = 0
        self._disk_writes = 0
        self._evictions = 0
        self._prefetch_reads = 0
        self._prefetch_hits = 0
        self._background_writes = 0
        self._background_failures = 0
        self._external_invalidations = 0
        self._last_background_error = None

    def stats(self):
        with self._lock:
            total = self._hits + self._misses
            result = {
                "strategy": self.strategy,
                "pool_size": self.pool_size,
                "cached": len(self._frames),
                "hits": self._hits,
                "misses": self._misses,
                "disk_reads": self._disk_reads,
                "disk_writes": self._disk_writes,
                "evictions": self._evictions,
                "hit_rate": (self._hits / total) if total else 0.0,
                "prefetch_reads": self._prefetch_reads,
                "prefetch_hits": self._prefetch_hits,
                "background_writes": self._background_writes,
                "background_failures": self._background_failures,
                "last_background_error": self._last_background_error,
                "external_invalidations": self._external_invalidations,
                "background_running": bool(
                    self._flush_thread and self._flush_thread.is_alive()
                ),
                "partitions": self.partition_stats(),
                "header_frames": len(self._header_cache),
            }
            header_stats = getattr(self.file_manager, "header_stats", None)
            if header_stats is not None:
                result["header_cache"] = header_stats()
            return result

    def reset_stats(self):
        with self._lock:
            self._reset_stats()

    def log_stats(self):
        stats = self.stats()
        if self.logger:
            self.logger.info(
                "缓冲池[%s] 命中=%d 未命中=%d 命中率=%.1f%% "
                "磁盘读=%d 磁盘写=%d 预读=%d/%d 淘汰=%d",
                stats["strategy"], stats["hits"], stats["misses"],
                stats["hit_rate"] * 100, stats["disk_reads"],
                stats["disk_writes"], stats["prefetch_hits"],
                stats["prefetch_reads"], stats["evictions"],
            )
        return stats

    # ---------------- 分区管理 ----------------
    def configure_partition(self, table, min_pages=0, max_pages=None):
        """设置按表缓存配额：软保留下限和硬占用上限。"""
        table = str(table).lower()
        if (not isinstance(min_pages, int) or isinstance(min_pages, bool)
                or min_pages < 0):
            raise ValueError("分区最小页数必须是非负整数")
        if max_pages is not None:
            if (not isinstance(max_pages, int) or isinstance(max_pages, bool)
                    or max_pages < 1):
                raise ValueError("分区最大页数必须是正整数或 None")
            if max_pages < min_pages:
                raise ValueError("分区最大页数不能小于最小页数")
            if max_pages > self.pool_size:
                raise ValueError("分区最大页数不能超过缓冲池容量")
        with self._lock:
            reserved = sum(
                config["min"] for name, config in self._partitions.items()
                if name != table
            ) + min_pages
            if reserved > self.pool_size:
                raise ValueError("所有分区的最小保留页数之和超过缓冲池容量")
            if min_pages == 0 and max_pages is None:
                self._partitions.pop(table, None)
            else:
                self._partitions[table] = {"min": min_pages, "max": max_pages}
        return True

    def remove_partition(self, table):
        with self._lock:
            return self._partitions.pop(str(table).lower(), None) is not None

    def _table_count(self, table):
        return sum(1 for key in self._frames if key[0] == table)

    def partition_stats(self):
        with self._lock:
            tables = set(self._partitions)
            tables.update(table for table, _page_id in self._frames)
            return {
                table: {
                    "cached": self._table_count(table),
                    "min": self._partitions.get(table, {}).get("min", 0),
                    "max": self._partitions.get(table, {}).get("max"),
                }
                for table in sorted(tables)
            }

    # ---------------- 核心接口 ----------------
    @staticmethod
    def _key(table, page_id):
        return (str(table).lower(), page_id)

    def _ensure_open(self):
        if self._closed:
            raise BufferPoolError("缓冲池已经关闭")

    def _read_disk_page(self, table, page_id):
        reader = getattr(self.file_manager, "_read_page_from_disk", None)
        if reader is None:
            reader = self.file_manager.read_page
        return reader(table, page_id)

    def _write_disk_page(self, table, page):
        writer = getattr(self.file_manager, "_write_page_from_buffer", None)
        if writer is None:
            writer = self.file_manager.write_page
        writer(table, page)

    def _ensure_capacity(self, incoming_table):
        config = self._partitions.get(incoming_table)
        if (config and config["max"] is not None
                and self._table_count(incoming_table) >= config["max"]):
            self._evict(preferred_table=incoming_table)
        if len(self._frames) >= self.pool_size:
            self._evict()

    def fetch_page(self, table, page_id):
        """取一页到缓冲池并 pin；未命中时可顺序预读后续页。"""
        table = str(table).lower()
        with self._lock:
            self._ensure_open()
            key = self._key(table, page_id)
            frame = self._frames.get(key)
            if frame is not None:
                self._hits += 1
                if frame.prefetched:
                    frame.prefetched = False
                    self._prefetch_hits += 1
                frame.last_access = next(self._clock)
                frame.pin_count += 1
                return frame.page

            self._misses += 1
            self._ensure_capacity(table)
            page = self._read_disk_page(table, page_id)
            self._disk_reads += 1
            frame = _Frame(key, page, next(self._order))
            frame.last_access = next(self._clock)
            frame.pin_count = 1
            self._frames[key] = frame
            if self.logger:
                self.logger.debug("加载页到缓冲池：%s 页号=%d", table, page_id)
            if self.prefetch_count:
                self._prefetch_pages_unlocked(
                    table, range(page_id + 1, page_id + 1 + self.prefetch_count)
                )
            return frame.page

    # Compatibility name shared with FileManager's page interface.
    def get_page(self, table, page_id):
        return self.fetch_page(table, page_id)

    def pin_page(self, table, page_id):
        with self._lock:
            frame = self._frames.get(self._key(table, page_id))
            if frame is None:
                return False
            frame.pin_count += 1
            frame.last_access = next(self._clock)
            if frame.prefetched:
                frame.prefetched = False
                self._prefetch_hits += 1
            return True

    def unpin_page(self, table, page_id, dirty=False):
        with self._lock:
            frame = self._frames.get(self._key(table, page_id))
            if frame is None:
                return False
            frame.pin_count = max(0, frame.pin_count - 1)
            frame.dirty = frame.dirty or dirty
            return True

    def mark_dirty(self, table, page_id):
        with self._lock:
            frame = self._frames.get(self._key(table, page_id))
            if frame is None:
                return False
            frame.dirty = True
            return True

    def snapshot_page(self, table, page_id):
        """返回缓存页的独立快照，使 FileManager 看见尚未刷盘的修改。"""
        with self._lock:
            frame = self._frames.get(self._key(table, page_id))
            if frame is None:
                return None
            return frame.page.__class__(page_id, frame.page.to_bytes())

    def cache_header(self, table, header):
        """缓存文件头快照；文件头写穿磁盘，但读取可命中此缓存。"""
        with self._lock:
            self._header_cache[str(table).lower()] = bytes(header)

    def snapshot_header(self, table):
        """返回文件头副本；不存在时返回 None。"""
        with self._lock:
            header = self._header_cache.get(str(table).lower())
            return bytearray(header) if header is not None else None

    def invalidate_header(self, table=None):
        with self._lock:
            if table is None:
                count = len(self._header_cache)
                self._header_cache.clear()
                return count
            return self._header_cache.pop(str(table).lower(), None) is not None

    # ---------------- 预读 ----------------
    def prefetch_pages(self, table, page_ids):
        """把指定页预读为未 pin 帧，返回实际读入页数。"""
        with self._lock:
            self._ensure_open()
            return self._prefetch_pages_unlocked(str(table).lower(), page_ids)

    def _prefetch_pages_unlocked(self, table, page_ids):
        loaded = 0
        for page_id in page_ids:
            key = self._key(table, page_id)
            if key in self._frames:
                continue
            try:
                self._ensure_capacity(table)
                page = self._read_disk_page(table, page_id)
            except (IndexError, BufferPoolError):
                break
            frame = _Frame(key, page, next(self._order), prefetched=True)
            frame.last_access = next(self._clock)
            self._frames[key] = frame
            self._disk_reads += 1
            self._prefetch_reads += 1
            loaded += 1
        return loaded

    # ---------------- 刷盘与外部一致性 ----------------
    def _flush_frame(self, frame, background=False):
        if not frame.dirty:
            return False
        table, _page_id = frame.key
        self._write_disk_page(table, frame.page)
        self._disk_writes += 1
        if background:
            self._background_writes += 1
        frame.dirty = False
        return True

    def flush_page(self, table, page_id):
        with self._lock:
            frame = self._frames.get(self._key(table, page_id))
            return bool(frame and self._flush_frame(frame))

    def flush_all(self):
        with self._lock:
            count = sum(
                1 for frame in list(self._frames.values())
                if self._flush_frame(frame)
            )
            if count and self.logger:
                self.logger.debug("刷回脏页：%d 页", count)
            return count

    def flush_unpinned(self, background=False):
        """只刷回未 pin 的脏页，适合后台线程。"""
        with self._lock:
            return sum(
                1 for frame in list(self._frames.values())
                if frame.pin_count == 0 and self._flush_frame(frame, background)
            )

    def prepare_external_write(self, table, page_ids):
        """FileManager 直接改盘前刷回并失效相关缓存页。"""
        keys = [self._key(table, page_id) for page_id in page_ids]
        with self._lock:
            frames = [self._frames[key] for key in keys if key in self._frames]
            pinned = [frame.key[1] for frame in frames if frame.pin_count]
            if pinned:
                raise BufferPoolError(
                    "直接修改磁盘前发现仍被 pin 的页：表=%s 页=%s"
                    % (table, pinned)
                )
            for frame in frames:
                self._flush_frame(frame)
            for frame in frames:
                self._frames.pop(frame.key, None)
            self._external_invalidations += len(frames)
            return len(frames)

    # ---------------- 后台刷盘 ----------------
    def start_background_flush(self, interval):
        interval = float(interval)
        if interval <= 0:
            raise ValueError("后台刷盘间隔必须大于 0")
        with self._lock:
            self._ensure_open()
            if self._flush_thread and self._flush_thread.is_alive():
                self._flush_interval = interval
                return False
            self._flush_interval = interval
            self._stop_event.clear()
            self._flush_thread = threading.Thread(
                target=self._background_worker,
                name="MiniDB-BufferFlusher",
                daemon=True,
            )
            self._flush_thread.start()
            return True

    def _background_worker(self):
        while not self._stop_event.wait(self._flush_interval):
            try:
                self.flush_unpinned(background=True)
            except Exception as error:
                with self._lock:
                    self._background_failures += 1
                    self._last_background_error = repr(error)
                if self.logger:
                    self.logger.exception("后台刷盘失败：%s", error)

    def stop_background_flush(self, flush=False):
        with self._lock:
            thread = self._flush_thread
            self._stop_event.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._lock:
            self._flush_thread = None
        if flush:
            self.flush_all()
        return thread is not None

    # ---------------- 生命周期与淘汰 ----------------
    def discard_table(self, table):
        table = str(table).lower()
        with self._lock:
            keys = [key for key in self._frames if key[0] == table]
            for key in keys:
                del self._frames[key]
            return len(keys)

    def clear(self):
        with self._lock:
            self.flush_all()
            self._frames.clear()
            self._header_cache.clear()

    def _select_victim(self, preferred_table=None):
        candidates = [frame for frame in self._frames.values() if frame.pin_count == 0]
        if preferred_table is not None:
            candidates = [frame for frame in candidates if frame.key[0] == preferred_table]
        if not candidates:
            raise BufferPoolError("缓冲池已满且可选页都被 pin，无法淘汰")

        if preferred_table is None:
            counts = {table: self._table_count(table) for table, _ in self._frames}
            unprotected = [
                frame for frame in candidates
                if counts[frame.key[0]]
                > self._partitions.get(frame.key[0], {}).get("min", 0)
            ]
            if unprotected:
                candidates = unprotected
        if self.strategy == "LRU":
            return min(candidates, key=lambda frame: frame.last_access)
        return min(candidates, key=lambda frame: frame.load_order)

    def _evict(self, preferred_table=None):
        victim = self._select_victim(preferred_table)
        table, page_id = victim.key
        self._flush_frame(victim)
        del self._frames[victim.key]
        self._evictions += 1
        if self.logger:
            self.logger.debug("淘汰页：%s 页号=%d（策略=%s）", table, page_id, self.strategy)
        return victim

    def close(self):
        if self._closed:
            return 0
        self.stop_background_flush(flush=False)
        count = self.flush_all()
        unregister = getattr(self.file_manager, "unregister_buffer_pool", None)
        if unregister is not None:
            unregister(self)
        with self._lock:
            self._closed = True
        return count

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __len__(self):
        with self._lock:
            return len(self._frames)
