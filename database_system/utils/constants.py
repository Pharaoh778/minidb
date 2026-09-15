# -*- coding: utf-8 -*-
"""全局常量定义。"""

# ============================ 存储相关 ============================
PAGE_SIZE = 4096          # 页大小（磁盘 I/O 的最小单位）
PAGE_HEADER_SIZE = 20     # 页头大小
SLOT_SIZE = 4             # 槽大小：记录偏移(2B) + 记录长度(2B)
FILE_MAGIC = b"MDBF"      # 数据文件魔数
OVERFLOW_MAGIC = b"MDBO"  # 溢出页文件魔数
OVERFLOW_FILE_SUFFIX = ".overflow"
STORAGE_LOG_FILENAME = "storage.log"

# —— 页头字段偏移 ——
PAGE_ID_OFFSET = 0        # int32  页号
NUM_SLOTS_OFFSET = 4      # uint16 槽数量
FREE_END_OFFSET = 6       # uint16 空闲区起始位置（记录从页尾向前生长）
NEXT_PAGE_OFFSET = 8      # int32  下一页（数据页双向链表 / 空闲页链表指针）
PREV_PAGE_OFFSET = 12     # int32  上一页
RESERVED_OFFSET = 16      # int32  保留字段

# —— 文件头（第 0 页）字段偏移 ——
FILE_MAGIC_OFFSET = 0       # 4B 魔数
FILE_PAGE_COUNT_OFFSET = 4  # int32 文件总页数（含头页）
FILE_FREE_LIST_OFFSET = 8   # int32 空闲页链表头
FILE_LAST_PAGE_OFFSET = 12  # int32 最后一个数据页

# —— 溢出页文件头/页布局 ——
OVERFLOW_PAGE_COUNT_OFFSET = 4
OVERFLOW_FREE_LIST_OFFSET = 8
OVERFLOW_NEXT_RECORD_OFFSET = 12
OVERFLOW_PAGE_ID_OFFSET = 0
OVERFLOW_NEXT_PAGE_OFFSET = 4
OVERFLOW_CHUNK_LENGTH_OFFSET = 8
OVERFLOW_FLAGS_OFFSET = 10
OVERFLOW_RECORD_ID_OFFSET = 12
OVERFLOW_PAGE_HEADER_SIZE = 20
OVERFLOW_RECORD_FLAG = 1

# ============================ 数据类型 ============================
TYPE_INT = "INT"
TYPE_FLOAT = "FLOAT"
TYPE_TEXT = "TEXT"
TYPE_BOOL = "BOOL"
SUPPORTED_TYPES = (TYPE_INT, TYPE_FLOAT, TYPE_TEXT, TYPE_BOOL)

# SQL 类型别名 -> 内部类型
TYPE_ALIASES = {
    "INTEGER": TYPE_INT,
    "INT": TYPE_INT,
    "DOUBLE": TYPE_FLOAT,
    "REAL": TYPE_FLOAT,
    "FLOAT": TYPE_FLOAT,
    "VARCHAR": TYPE_TEXT,
    "CHAR": TYPE_TEXT,
    "STRING": TYPE_TEXT,
    "TEXT": TYPE_TEXT,
    "BOOL": TYPE_BOOL,
    "BOOLEAN": TYPE_BOOL,
}

# 定长类型的字节数
INT_SIZE = 4
FLOAT_SIZE = 8
BOOL_SIZE = 1
LEN_PREFIX_SIZE = 2      # 变长字段（TEXT）的长度前缀
MAX_TEXT_LENGTH = 4000   # 单个 TEXT 字段最大字节数（受单页容量约束）

# ============================ 系统目录 ============================
CATALOG_TABLE = "__catalog"          # 元数据存储用的特殊系统表
CATALOG_COL_TABLE = "table_name"     # 系统表列：表名
CATALOG_COL_DEF = "definition"       # 系统表列：表定义（JSON）

# ============================ 默认配置 ============================
DEFAULT_DATA_DIR = "data"
DEFAULT_POOL_SIZE = 64
DEFAULT_STRATEGY = "LRU"   # LRU / FIFO
SUPPORTED_STRATEGIES = ("LRU", "FIFO")
# 预读和后台线程默认关闭，以保持小型/测试工作负载的确定性；可在 BufferPool
# 构造时开启，也可在运行期调用 prefetch_pages/start_background_flush。
DEFAULT_PREFETCH_PAGES = 0
DEFAULT_BACKGROUND_FLUSH_INTERVAL = 0.0

# ============================ B+ 树索引 ============================
# order 表示内部节点最多拥有的子节点数；32 阶适合内存索引并减少树高。
MIN_BPLUS_TREE_ORDER = 3
DEFAULT_BPLUS_TREE_ORDER = 32

# ============================ 关键字 ============================
KEYWORDS = {
    "CREATE", "TABLE", "DROP", "INSERT", "INTO", "VALUES", "SELECT", "FROM",
    "WHERE", "UPDATE", "SET", "DELETE", "SHOW", "TABLES", "DESC", "DESCRIBE",
    "EXPLAIN", "PRIMARY", "KEY", "NOT", "NULL", "AND", "OR", "IS", "IN",
    "LIKE", "ORDER", "BY", "ASC", "LIMIT", "IF", "EXISTS", "TRUE", "FALSE",
    "AS",
    "INT", "INTEGER", "FLOAT", "DOUBLE", "REAL", "TEXT", "VARCHAR", "CHAR",
    "BOOL", "BOOLEAN",
}

# ============================ 词法记号类型 ============================
TT_KEYWORD = "KEYWORD"
TT_IDENTIFIER = "IDENTIFIER"
TT_INT = "INT_LITERAL"
TT_FLOAT = "FLOAT_LITERAL"
TT_STRING = "STRING_LITERAL"
TT_OPERATOR = "OPERATOR"
TT_PUNCT = "PUNCTUATION"
TT_EOF = "EOF"
