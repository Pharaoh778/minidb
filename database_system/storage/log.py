# -*- coding: utf-8 -*-
"""Persistent storage operation log.

The log is append-only JSONL so it remains inspectable with ordinary tools while
retaining enough metadata to locate a bad operation by LSN/table/page/slot.
It is deliberately storage-layer agnostic: callers decide whether a record is
replayable, and ``replay`` simply delivers validated entries in LSN order.
"""

import json
import os
import threading
import time
from collections import namedtuple


LogEntry = namedtuple(
    "LogEntry", "lsn timestamp operation table page_id slot_id details"
)


class StorageLogError(IOError):
    """Base class for persistent storage-log errors."""


class StorageLogCorruptionError(StorageLogError):
    """A malformed log line with its file and line/LSN location."""

    def __init__(self, path, line_number, message, lsn=None):
        self.path = path
        self.line_number = line_number
        self.lsn = lsn
        location = "%s 行=%d" % (path, line_number)
        if lsn is not None:
            location += " LSN=%d" % lsn
        super().__init__("存储日志损坏（%s）：%s" % (location, message))


class StorageLog:
    """Thread-safe append-only operation log.

    ``durable=True`` calls ``flush`` and ``os.fsync`` for every append.  The
    default is buffered append plus explicit ``flush``/``close`` for better
    throughput; callers can still request durability per append.
    """

    def __init__(self, path, durable=False, auto_flush=True, create=True):
        self.path = os.path.abspath(os.fspath(path))
        self.durable = bool(durable)
        self.auto_flush = bool(auto_flush)
        self._lock = threading.RLock()
        self._closed = False
        parent = os.path.dirname(self.path)
        if create and parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(self.path):
            if not create:
                raise FileNotFoundError(self.path)
            with open(self.path, "ab"):
                pass
        self._next_lsn = self._scan_last_lsn() + 1
        self._fp = open(self.path, "a", encoding="utf-8", newline="\n")

    def _scan_last_lsn(self):
        last_lsn = 0
        with open(self.path, "r", encoding="utf-8") as fp:
            for line_number, line in enumerate(fp, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError) as error:
                    raise StorageLogCorruptionError(
                        self.path, line_number, "不是有效 JSON：%s" % error
                    ) from error
                lsn = payload.get("lsn")
                if not isinstance(lsn, int) or lsn <= last_lsn:
                    raise StorageLogCorruptionError(
                        self.path, line_number, "LSN 不递增或类型错误", lsn
                    )
                last_lsn = lsn
        return last_lsn

    @property
    def next_lsn(self):
        with self._lock:
            return self._next_lsn

    def _ensure_open(self):
        if self._closed:
            raise StorageLogError("存储日志已经关闭：%s" % self.path)

    def append(self, operation, table=None, page_id=None, slot_id=None,
               durable=None, timestamp=None, **details):
        """Append one operation and return its monotonically increasing LSN."""
        operation = str(operation).upper()
        if not operation or any(char.isspace() for char in operation):
            raise ValueError("日志操作名不能为空且不能包含空白")
        with self._lock:
            self._ensure_open()
            lsn = self._next_lsn
            payload = {
                "lsn": lsn,
                "timestamp": time.time_ns() if timestamp is None else timestamp,
                "operation": operation,
                "table": None if table is None else str(table).lower(),
                "page_id": page_id,
                "slot_id": slot_id,
                "details": details,
            }
            self._fp.write(json.dumps(payload, ensure_ascii=False,
                                       separators=(",", ":")) + "\n")
            self._next_lsn += 1
            should_sync = self.durable if durable is None else bool(durable)
            if self.auto_flush or should_sync:
                self._fp.flush()
            if should_sync:
                os.fsync(self._fp.fileno())
            return lsn

    def flush(self, durable=False):
        with self._lock:
            self._ensure_open()
            self._fp.flush()
            if durable:
                os.fsync(self._fp.fileno())

    def entries(self, start_lsn=1, end_lsn=None):
        """Yield validated entries in LSN order without mutating the log."""
        if start_lsn < 1:
            raise ValueError("start_lsn 必须大于等于 1")
        with self._lock:
            self._ensure_open()
            self._fp.flush()
            entries = []
            with open(self.path, "r", encoding="utf-8") as fp:
                previous = 0
                for line_number, line in enumerate(fp, 1):
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError) as error:
                        raise StorageLogCorruptionError(
                            self.path, line_number, "不是有效 JSON：%s" % error
                        ) from error
                    lsn = payload.get("lsn")
                    if not isinstance(lsn, int) or lsn <= previous:
                        raise StorageLogCorruptionError(
                            self.path, line_number, "LSN 不递增或类型错误", lsn
                        )
                    previous = lsn
                    if lsn < start_lsn:
                        continue
                    if end_lsn is not None and lsn > end_lsn:
                        break
                    entries.append(LogEntry(
                        lsn,
                        payload.get("timestamp"),
                        payload.get("operation"),
                        payload.get("table"),
                        payload.get("page_id"),
                        payload.get("slot_id"),
                        payload.get("details") or {},
                    ))
        yield from entries

    def replay(self, handler, start_lsn=1, end_lsn=None):
        """Invoke ``handler(entry)`` in durable log order; return count."""
        count = 0
        for entry in self.entries(start_lsn, end_lsn):
            handler(entry)
            count += 1
        return count

    def checkpoint(self, label=None, durable=None, **details):
        if label is not None:
            details["label"] = str(label)
        return self.append("CHECKPOINT", durable=durable, **details)

    def close(self, durable=False):
        with self._lock:
            if self._closed:
                return
            self._fp.flush()
            if durable:
                os.fsync(self._fp.fileno())
            self._fp.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close(durable=self.durable)

    def __repr__(self):
        return "StorageLog(path=%r, next_lsn=%d, durable=%s)" % (
            self.path, self.next_lsn, self.durable
        )
