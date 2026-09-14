# -*- coding: utf-8 -*-
"""B+ tree index used by the storage layer.

The tree is a multimap: one key can point at more than one record location.  All
payloads live in leaves, internal nodes only contain separator keys, and leaves
are linked in both directions so range scans do not have to revisit the tree.
"""

from bisect import bisect_left, bisect_right
from collections import namedtuple

from ..utils.constants import DEFAULT_BPLUS_TREE_ORDER, MIN_BPLUS_TREE_ORDER


RecordLocation = namedtuple("RecordLocation", "table page_id slot_id")
_MISSING = object()


class IndexErrorBase(Exception):
    """Base class for storage-index errors."""


class DuplicateIndexKeyError(IndexErrorBase):
    """Raised when a unique index receives a duplicate key."""


class IndexCorruptionError(IndexErrorBase):
    """An invariant violation with a node path suitable for diagnostics."""

    def __init__(self, message, path="root"):
        self.path = path
        super().__init__("索引结构损坏（%s）：%s" % (path, message))


class StaleIndexEntryError(IndexErrorBase):
    """The index points to a record that no longer exists."""

    def __init__(self, location, message):
        self.location = location
        super().__init__(
            "索引记录失效（表=%s 页=%d 槽=%d）：%s"
            % (location.table, location.page_id, location.slot_id, message)
        )


class _Node:
    def __init__(self):
        self.keys = []
        self.parent = None

    @property
    def is_leaf(self):
        return False


class _Leaf(_Node):
    def __init__(self):
        super().__init__()
        self.values = []
        self.next = None
        self.prev = None

    @property
    def is_leaf(self):
        return True


class _Internal(_Node):
    def __init__(self):
        super().__init__()
        self.children = []


class BPlusTree:
    """Ordered B+ tree multimap.

    ``order`` is the maximum number of children in an internal node.  Exact
    lookup and insertion are O(log n); ordered/range scans are O(log n + k).
    ``search`` always returns a new list because duplicate keys are supported.
    Set ``unique=True`` to reject a second value for the same key.
    """

    def __init__(self, order=DEFAULT_BPLUS_TREE_ORDER, unique=False):
        if not isinstance(order, int) or isinstance(order, bool):
            raise TypeError("B+ 树阶数必须是整数")
        if order < MIN_BPLUS_TREE_ORDER:
            raise ValueError("B+ 树阶数不能小于 %d" % MIN_BPLUS_TREE_ORDER)
        self.order = order
        self.unique = bool(unique)
        self.root = _Leaf()
        self._key_count = 0
        self._value_count = 0

    @property
    def key_count(self):
        return self._key_count

    @property
    def height(self):
        height = 1
        node = self.root
        while not node.is_leaf:
            height += 1
            node = node.children[0]
        return height

    def __len__(self):
        """Return the number of indexed values (duplicates included)."""
        return self._value_count

    def __bool__(self):
        return self._value_count > 0

    @property
    def _max_keys(self):
        return self.order - 1

    @property
    def _min_leaf_keys(self):
        return self.order // 2

    @property
    def _min_internal_children(self):
        return (self.order + 1) // 2

    def _find_leaf(self, key):
        node = self.root
        while not node.is_leaf:
            node = node.children[bisect_right(node.keys, key)]
        return node

    @staticmethod
    def _first_key(node):
        while not node.is_leaf:
            node = node.children[0]
        if not node.keys:
            raise IndexCorruptionError("非根子树没有首键")
        return node.keys[0]

    def _rebuild_keys(self, node):
        if not node.is_leaf:
            node.keys = [self._first_key(child) for child in node.children[1:]]

    def _refresh_upward(self, node):
        parent = node.parent
        while parent is not None:
            self._rebuild_keys(parent)
            parent = parent.parent

    def insert(self, key, value):
        """Add ``key -> value`` and return the value."""
        leaf = self._find_leaf(key)
        index = bisect_left(leaf.keys, key)
        if index < len(leaf.keys) and leaf.keys[index] == key:
            if self.unique:
                raise DuplicateIndexKeyError("索引键已存在：%r" % (key,))
            leaf.values[index].append(value)
            self._value_count += 1
            return value

        leaf.keys.insert(index, key)
        leaf.values.insert(index, [value])
        self._key_count += 1
        self._value_count += 1
        if len(leaf.keys) > self._max_keys:
            self._split_leaf(leaf)
        else:
            self._refresh_upward(leaf)
        return value

    def _split_leaf(self, leaf):
        split_at = (len(leaf.keys) + 1) // 2
        right = _Leaf()
        right.keys = leaf.keys[split_at:]
        right.values = leaf.values[split_at:]
        leaf.keys = leaf.keys[:split_at]
        leaf.values = leaf.values[:split_at]

        right.next = leaf.next
        if right.next is not None:
            right.next.prev = right
        leaf.next = right
        right.prev = leaf
        self._insert_sibling(leaf, right)

    def _insert_sibling(self, left, right):
        parent = left.parent
        if parent is None:
            parent = _Internal()
            parent.children = [left, right]
            left.parent = parent
            right.parent = parent
            self._rebuild_keys(parent)
            self.root = parent
            return

        position = parent.children.index(left) + 1
        parent.children.insert(position, right)
        right.parent = parent
        self._rebuild_keys(parent)
        if len(parent.children) > self.order:
            self._split_internal(parent)
        else:
            self._refresh_upward(parent)

    def _split_internal(self, node):
        split_at = (len(node.children) + 1) // 2
        right = _Internal()
        right.children = node.children[split_at:]
        node.children = node.children[:split_at]
        for child in right.children:
            child.parent = right
        self._rebuild_keys(node)
        self._rebuild_keys(right)
        self._insert_sibling(node, right)

    def search(self, key):
        """Return all values for an exact key; return ``[]`` when absent."""
        leaf = self._find_leaf(key)
        index = bisect_left(leaf.keys, key)
        if index < len(leaf.keys) and leaf.keys[index] == key:
            return list(leaf.values[index])
        return []

    def get(self, key, default=None):
        """Return the first exact-match value, useful for unique indexes."""
        values = self.search(key)
        return values[0] if values else default

    def contains(self, key):
        return bool(self.search(key))

    def range_search(self, start=None, end=None, include_start=True,
                     include_end=True):
        """Return sorted ``(key, value)`` pairs in the requested interval."""
        if start is not None and end is not None and start > end:
            return []
        leaf = self._leftmost_leaf() if start is None else self._find_leaf(start)
        result = []
        while leaf is not None:
            for key, values in zip(leaf.keys, leaf.values):
                if start is not None and (key < start or (key == start and not include_start)):
                    continue
                if end is not None and (key > end or (key == end and not include_end)):
                    return result
                result.extend((key, value) for value in values)
            leaf = leaf.next
        return result

    def items(self):
        """Iterate over all ``(key, value)`` pairs in key order."""
        leaf = self._leftmost_leaf()
        while leaf is not None:
            for key, values in zip(leaf.keys, leaf.values):
                for value in values:
                    yield key, value
            leaf = leaf.next

    def keys(self):
        leaf = self._leftmost_leaf()
        while leaf is not None:
            yield from leaf.keys
            leaf = leaf.next

    def _leftmost_leaf(self):
        node = self.root
        while not node.is_leaf:
            node = node.children[0]
        return node

    def delete(self, key, value=_MISSING):
        """Delete a key or one of its values; return whether anything changed."""
        leaf = self._find_leaf(key)
        index = bisect_left(leaf.keys, key)
        if index >= len(leaf.keys) or leaf.keys[index] != key:
            return False

        if value is not _MISSING:
            try:
                leaf.values[index].remove(value)
            except ValueError:
                return False
            self._value_count -= 1
            if leaf.values[index]:
                return True
        else:
            self._value_count -= len(leaf.values[index])

        del leaf.keys[index]
        del leaf.values[index]
        self._key_count -= 1
        if leaf is self.root:
            return True
        if len(leaf.keys) < self._min_leaf_keys:
            self._rebalance_leaf(leaf)
        else:
            self._refresh_upward(leaf)
        return True

    def _rebalance_leaf(self, leaf):
        parent = leaf.parent
        index = parent.children.index(leaf)
        left = parent.children[index - 1] if index else None
        right = parent.children[index + 1] if index + 1 < len(parent.children) else None

        if left is not None and len(left.keys) > self._min_leaf_keys:
            leaf.keys.insert(0, left.keys.pop())
            leaf.values.insert(0, left.values.pop())
            self._refresh_upward(leaf)
            return
        if right is not None and len(right.keys) > self._min_leaf_keys:
            leaf.keys.append(right.keys.pop(0))
            leaf.values.append(right.values.pop(0))
            self._refresh_upward(right)
            return

        if left is not None:
            left.keys.extend(leaf.keys)
            left.values.extend(leaf.values)
            left.next = leaf.next
            if leaf.next is not None:
                leaf.next.prev = left
            self._remove_child(parent, index)
        else:
            leaf.keys.extend(right.keys)
            leaf.values.extend(right.values)
            leaf.next = right.next
            if right.next is not None:
                right.next.prev = leaf
            self._remove_child(parent, index + 1)

    def _remove_child(self, parent, index):
        child = parent.children.pop(index)
        child.parent = None
        self._rebuild_keys(parent)
        if parent is self.root:
            if len(parent.children) == 1:
                self.root = parent.children[0]
                self.root.parent = None
            return
        if len(parent.children) < self._min_internal_children:
            self._rebalance_internal(parent)
        else:
            self._refresh_upward(parent)

    def _rebalance_internal(self, node):
        parent = node.parent
        index = parent.children.index(node)
        left = parent.children[index - 1] if index else None
        right = parent.children[index + 1] if index + 1 < len(parent.children) else None

        if left is not None and len(left.children) > self._min_internal_children:
            child = left.children.pop()
            node.children.insert(0, child)
            child.parent = node
            self._rebuild_keys(left)
            self._rebuild_keys(node)
            self._refresh_upward(node)
            return
        if right is not None and len(right.children) > self._min_internal_children:
            child = right.children.pop(0)
            node.children.append(child)
            child.parent = node
            self._rebuild_keys(right)
            self._rebuild_keys(node)
            self._refresh_upward(right)
            return

        if left is not None:
            for child in node.children:
                child.parent = left
            left.children.extend(node.children)
            self._rebuild_keys(left)
            self._remove_child(parent, index)
        else:
            for child in right.children:
                child.parent = node
            node.children.extend(right.children)
            self._rebuild_keys(node)
            self._remove_child(parent, index + 1)

    def clear(self):
        self.root = _Leaf()
        self._key_count = 0
        self._value_count = 0

    def bulk_load(self, items):
        """Replace the contents with iterable ``(key, value)`` pairs."""
        self.clear()
        for key, value in sorted(items, key=lambda item: item[0]):
            self.insert(key, value)
        return self

    def validate(self):
        """Check every structural invariant and pinpoint the failing node."""
        leaves = []
        counted_keys = 0
        counted_values = 0
        leaf_depth = None

        def visit(node, path, depth):
            nonlocal counted_keys, counted_values, leaf_depth
            if any(node.keys[i] >= node.keys[i + 1] for i in range(len(node.keys) - 1)):
                raise IndexCorruptionError("键没有严格递增", path)
            if node.is_leaf:
                if len(node.keys) != len(node.values):
                    raise IndexCorruptionError("键和值数组长度不一致", path)
                if node is not self.root and len(node.keys) < self._min_leaf_keys:
                    raise IndexCorruptionError("叶节点低于最小占用率", path)
                if len(node.keys) > self._max_keys:
                    raise IndexCorruptionError("叶节点溢出", path)
                if any(not values for values in node.values):
                    raise IndexCorruptionError("存在没有记录位置的键", path)
                if leaf_depth is None:
                    leaf_depth = depth
                elif leaf_depth != depth:
                    raise IndexCorruptionError("叶节点不在同一层", path)
                leaves.append(node)
                counted_keys += len(node.keys)
                counted_values += sum(len(values) for values in node.values)
                return

            if len(node.children) != len(node.keys) + 1:
                raise IndexCorruptionError("子节点数不等于键数加一", path)
            if node is not self.root and len(node.children) < self._min_internal_children:
                raise IndexCorruptionError("内部节点低于最小占用率", path)
            if len(node.children) > self.order:
                raise IndexCorruptionError("内部节点溢出", path)
            expected = [self._first_key(child) for child in node.children[1:]]
            if node.keys != expected:
                raise IndexCorruptionError("分隔键与右子树首键不一致", path)
            for child_index, child in enumerate(node.children):
                if child.parent is not node:
                    raise IndexCorruptionError("子节点 parent 指针错误", "%s/%d" % (path, child_index))
                visit(child, "%s/%d" % (path, child_index), depth + 1)

        visit(self.root, "root", 0)
        for index, leaf in enumerate(leaves):
            expected_prev = leaves[index - 1] if index else None
            expected_next = leaves[index + 1] if index + 1 < len(leaves) else None
            if leaf.prev is not expected_prev or leaf.next is not expected_next:
                raise IndexCorruptionError("叶节点链表指针错误", "leaf/%d" % index)
            if expected_prev and expected_prev.keys[-1] >= leaf.keys[0]:
                raise IndexCorruptionError("相邻叶节点键区间重叠", "leaf/%d" % index)
        if counted_keys != self._key_count or counted_values != self._value_count:
            raise IndexCorruptionError(
                "计数不一致，期望 keys=%d/values=%d，实际 keys=%d/values=%d"
                % (self._key_count, self._value_count, counted_keys, counted_values)
            )
        return True


class RecordIndex(BPlusTree):
    """B+ tree specialized for globally locating table records by a key."""

    def add(self, key, table, page_id, slot_id):
        location = RecordLocation(str(table).lower(), int(page_id), int(slot_id))
        return self.insert(key, location)

    def locate(self, key):
        return self.search(key)

    def remove(self, key, table=None, page_id=None, slot_id=None):
        if table is None:
            return self.delete(key)
        location = RecordLocation(str(table).lower(), int(page_id), int(slot_id))
        return self.delete(key, location)

    def rebuild(self, file_manager, key_func, tables=None):
        """Rebuild a global index directly from storage pages.

        ``key_func`` receives ``(table, page_id, slot_id, raw_record)``.  It may
        decode a primary key in the engine layer or simply index the raw bytes.
        Passing ``None`` as its result skips a record.  The operation builds a
        temporary tree first, so a decoding failure leaves this index intact.
        """
        table_names = file_manager.list_files() if tables is None else tables
        replacement = RecordIndex(order=self.order, unique=self.unique)
        for table in table_names:
            table = str(table).lower()
            if not file_manager.table_exists(table):
                continue
            for page_id in file_manager.data_page_ids(table):
                page = file_manager.read_page(table, page_id)
                for slot_id, data in page.records():
                    try:
                        key = key_func(table, page_id, slot_id, data)
                    except Exception as error:
                        location = RecordLocation(table, page_id, slot_id)
                        raise StaleIndexEntryError(
                            location, "提取索引键失败：%s" % error
                        ) from error
                    if key is not None:
                        replacement.insert(
                            key, RecordLocation(table, page_id, slot_id)
                        )
        self.root = replacement.root
        self._key_count = replacement._key_count
        self._value_count = replacement._value_count
        return self._value_count

    def search_records(self, file_manager, key):
        """Resolve a key to ``(RecordLocation, raw_record)`` pairs.

        A stale entry is reported with its exact table/page/slot address, which
        makes index/data inconsistencies straightforward to diagnose.
        """
        records = []
        for location in self.locate(key):
            if not file_manager.table_exists(location.table):
                raise StaleIndexEntryError(location, "表文件不存在")
            try:
                page = file_manager.read_page(location.table, location.page_id)
            except Exception as error:
                raise StaleIndexEntryError(location, str(error)) from error
            data = page.get_record(location.slot_id)
            if data is None:
                raise StaleIndexEntryError(location, "记录不存在或已删除")
            records.append((location, data))
        return records
