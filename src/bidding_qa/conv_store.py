"""对话历史持久化：把前端的对话（会话 + 消息）存进 PostgreSQL。

**与 `pg_store.py` 结构同构、语义不同**。两者共用同一个库、同一份 `.env` 凭据，
骨架（`ENABLED` 开关、懒连接 + 30 秒冷却、双检锁、游标每次新建、`warmup()` 吞异常）
是照抄的，但有三处**刻意不同**，别顺手「统一」回去：

| | `pg_store.py` | 本模块 |
| --- | --- | --- |
| 连接参数 | `options="-c default_transaction_read_only=on …"` | **没有这个 options** |
| 建连后自检 | `_assert_read_only()`，不过就抛 | **没有** |
| 权限 | 只读 —— 模型能影响 SQL 的列名与值 | **可写** —— 写入的是结构化、模型无关的数据 |
| 失败形态 | `query()` 永不抛，用 `ok=False` + `note` 回话 | **抛 `ConvStoreUnavailable`** |

最后一行是这里最容易搞错的地方。`pg_store.query()` 之所以「永不抛异常」，是**工具**契约
逼出来的（CLAUDE.md 不变量 #1：工具执行漏掉 `role="tool"` 响应会让下一轮请求 400）。
本模块**不是工具** —— 它的调用方是普通 JSON 路由函数，那里**抛异常才是对的形状**：
失败 → `ConvStoreUnavailable` → 路由 `except` 转 503。这正是 `/api/vision` 的成例
（`analyze()` 抛 `VisionNotConfiguredError` → 路由转 503），也是最贴近的先例：同样是一个
不碰 Qdrant 的独立能力被普通 JSON 路由调用。

⚠ 任何 psycopg 异常**都要先包成 `ConvStoreUnavailable`**，别把裸的 `OperationalError`
漏到路由层 —— 它的字符串里带着主机名、库名、用户名，前端会原样看到一串英文连接串。
原始异常只进服务端日志。

两条连接**各自独立**（`_run()` 只操作自己模块的 `_conn`），所以「多加了一条可写连接
会不会让采购工具变得可写」的答案是**不会** —— `scripts/probe_conv_store.py` ⑩ 专门钉这条。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from functools import lru_cache
from typing import Any

from .config import settings
from .pg_store import _like
from .schemas import (
    ConversationFull,
    ConversationHit,
    ConversationMeta,
    ConversationUpsert,
    FeedbackRow,
    FeedbackUpsert,
)

logger = logging.getLogger(__name__)

# 是否启用对话历史持久化。与 graph_store / pg_store 一样是**进程级**常量（导入期按 .env 算死）。
#
# ⚠ 它与 `pg_store.ENABLED` **读的是同一个键**（POSTGRES_HOST），也就是「采购记录库」与
#   「对话历史」共用同一个开关，置空 POSTGRES_HOST 会把两者一起关掉。这是**有意**的：
#   两者本来就用同一个库、同一份凭据，为它们各加一个开关只会多出一份「配了库但历史不存」
#   的中间态。真要让两者分离，再拆键不迟。
ENABLED = bool(settings.postgres_host.strip())

# 连接失败后的冷却时长（秒）。与 graph_store / pg_store 同一套理由：既不能永久记住失败
# （PostgreSQL 可能比服务晚起），也不能每次都重试（每次都要耗掉一个 connect_timeout）。
_FAIL_COOLDOWN = 30.0

# `list_conversations` 的默认上限与硬上限。历史列表是侧栏用的，不需要无限长；
# 硬上限挡住调用方传个 100000 进来。
_LIST_HARD_CAP = 500
# 搜索的默认条数与硬上限
_SEARCH_HARD_CAP = 100
# 反馈列表的硬上限（同 _LIST_HARD_CAP 的理由：挡住调用方传个 100000 进来）
_FEEDBACK_HARD_CAP = 500
# 片段在命中位置两侧各取多少字符
_SNIPPET_PAD = 60
# 取回多少正文来切片段。**必须有上限**：单条回答可以上万字符，20 条命中就是几百 KB
# 白跨一趟网络。4000 足够覆盖绝大多数消息（切片段只需命中位置附近那 120 个字符）。
_SNIPPET_SCAN_CHARS = 4000

_lock = threading.Lock()
# ⚠ 与 `_lock` **是两个不同的锁**，别合并：`_lock` 只保护连接与建表，`_op_lock` 串行化
#   所有数据库操作（理由见 `_guard`）。合并会把「取连接」和「跑语句」套在一起，
#   而 `_get_conn()` 内部也要拿 `_lock` —— 非重入锁直接死锁。
_op_lock = threading.Lock()
_conn: Any = None
_failed_at: float | None = None
_last_error: Exception | None = None
_schema_ready = False


class ConvStoreUnavailable(RuntimeError):
    """对话历史存储不可用（未配置 / 连不上 / 建表失败）。

    路由层捕获它转 **503**。刻意与「这条对话不存在」区分开 —— 后者是 `None` → **404**，
    两者塌成同一个状态码的话，用户就分不出「删过了」和「数据库挂了」。
    """


# 回给前端的统一文案。**不拼接原始异常**（理由见模块 docstring：会带出主机名/用户名）。
# 真正的错误原因进日志。
_UNAVAILABLE_MSG = "对话历史数据库暂时不可用，本次对话将只保存在本机浏览器。"

# 反馈专用的不可用文案。⚠ 别图省事复用上面那句：用户点了 👎 却读到
# 「本次对话将只保存在本机浏览器」，说的是一件**没发生的事**（对话好好地存着，丢的是评价）。
_FEEDBACK_UNAVAILABLE_MSG = "反馈数据库暂时不可用，这次的评价没有保存。"

# 「清空全部」专用的不可用文案。同 `_FEEDBACK_UNAVAILABLE_MSG` 的道理，**别复用默认那句**：
# 那句说的是**保存**（「本次对话将只保存在本机浏览器」），而用户点的是**删除** ——
# 答的不是他问的问题。⚠ 侧栏会把这句话**原样显示**在确认卡片下面，更要说对。
_DELETE_ALL_UNAVAILABLE_MSG = "对话历史数据库暂时不可用，未删除任何对话。"


# ---------------------------------------------------------------- 时间戳
def _to_ts(ms: int) -> dt.datetime:
    """epoch 毫秒 → 带时区的 datetime。

    表里用 `timestamptz` 而不是 `bigint` epoch 毫秒，是为了库里可读
    （psql 里看到 `2026-09-20 10:00:00+08` 而不是 `1758336000000`）。
    ⚠ 代价是**必须在模块边界上转掉**：`datetime` 不是 JSON 可序列化的，漏了会在
    `api.py` 那句 `json.dumps` 处炸成 `TypeError`（`Decimal`/`date` 老坑，本项目第五次）。
    """
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def _to_ms(ts: dt.datetime) -> int:
    """带时区的 datetime → epoch 毫秒。**用 round，不用 int()** —— 截断会让
    「写进去再读出来」差 1 毫秒，往返断言就永远过不了（差多少取决于小数部分）。"""
    if ts.tzinfo is None:  # 理论上不会发生（列是 timestamptz），保险起见按 UTC 解释
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return round(ts.timestamp() * 1000)


# ---------------------------------------------------------------- 表结构
# `payload jsonb` + 两个生成列：整个前端 `Message` 对象原样存进 payload，所以前端 Message
# 以后加字段（像上次加 image 那样）**不用改表**；而 role / content 是靠 `GENERATED ALWAYS AS`
# 算出来的**真列**，可直接 ILIKE，且与 payload **不可能不一致**（列与 JSON 各存一份才会漂移）。
#
# ⚠ 改这里的任何一条（尤其是列定义）都要**手工 DROP + CREATE**：`CREATE TABLE IF NOT EXISTS`
#   对已存在的表是空操作，既不补列也不改列 —— 与 `src/database/to_postgresql.py` 同一条坑。
_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id         text PRIMARY KEY,
        title      text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        conversation_id text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        id              text NOT NULL,
        seq             integer NOT NULL,
        payload         jsonb NOT NULL,
        role            text GENERATED ALWAYS AS (payload->>'role')    STORED,
        content         text GENERATED ALWAYS AS (payload->>'content') STORED,
        PRIMARY KEY (conversation_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        conversation_id text        NOT NULL,
        message_id      text        NOT NULL,
        rating          smallint    NOT NULL,
        question        text        NOT NULL DEFAULT '',
        answer          text        NOT NULL DEFAULT '',
        doc_ids         jsonb       NOT NULL DEFAULT '[]'::jsonb,
        created_at      timestamptz NOT NULL,
        updated_at      timestamptz NOT NULL,
        PRIMARY KEY (conversation_id, message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS messages_conv_seq_idx ON messages (conversation_id, seq)",
    "CREATE INDEX IF NOT EXISTS conversations_updated_idx ON conversations (updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS feedback_created_idx ON feedback (created_at DESC)",
)

# ⚠⚠ `feedback` 上**一处 `REFERENCES` 都没有**，这是刻意的，改之前先读完这段。
#
# 直觉写法是 `message_id text REFERENCES messages(id) ON DELETE CASCADE` ——那是**错的**，
# 而且错得完全看不见：`save_conversation()` 每次保存都
# `DELETE FROM messages WHERE conversation_id = %s` 再整批重插（见该函数），
# 而前端**一轮回答就 PUT 一次**。于是外键的级联会在下次自动保存时
# **把这条对话上所有的 👍/👎 静默删光** —— 不报错、界面上也看不出来，
# 只有几周后翻数据库才会发现反馈少了大半。
#
# 不加外键还顺带对了第二件事：**对话被删掉后反馈仍然留着**。而「攒下来用于优化检索」
# 要的正是这个 —— `delete_conversation()` 靠级联删消息，反馈不该跟着走。
#
# ⚠ 这条不能只靠注释守：`scripts/probe_feedback.py` ⑦⑧ 两条**行为断言**钉着它
#   （重新保存一次对话后反馈行还在 / 删掉对话后反馈行还在）。日后谁「顺手把外键补上」，
#   那两条会当场变红。


def ensure_schema(conn: Any) -> None:
    """建表。**懒确保** —— 由 `_get_conn()` 在建连成功后调用，进程内只真正跑一次。

    为什么放在连接层而不是写入路径：`/api/conversations` 完全可能在任何写入之前被访问
    （用户打开页面、还没提问）。若只在**写入时**建表，第一次读就是 `UndefinedTable`
    → 503 → 前端切进降级模式，**而且会一直降级到有人手动跑脚本为止**（表永远建不出来）。
    读路径必须也能建。

    DDL 全是 `IF NOT EXISTS`，多跑无害，但**不依赖它** —— 标志位是主，DDL 是保险。
    """
    global _schema_ready
    if _schema_ready:
        return
    with _lock:
        if _schema_ready:
            return
        with conn.cursor() as cur:
            for statement in _DDL:
                cur.execute(statement)
        _schema_ready = True
        logger.info("对话历史表已就绪（conversations / messages / feedback）")


# ---------------------------------------------------------------- 连接
def _conninfo() -> dict[str, Any]:
    return {
        "host": settings.postgres_host.strip(),
        "port": settings.postgres_port,
        "user": settings.postgres_user,
        "password": settings.postgres_password,
        "dbname": settings.postgres_database.strip(),
        "connect_timeout": settings.postgres_connect_timeout,
    }


def _connect() -> Any:
    """建一个**可写**连接。

    ⚠ **刻意不加 `options="-c default_transaction_read_only=on"`** —— 那是 `pg_store`
    的只读防线，这里要写。两条连接各自独立，所以这不影响采购工具的只读保护
    （`probe_conv_store.py` ⑩ 验的就是这件事）。

    `autocommit=True` 与 `pg_store` 一致：一条失败的语句不会污染共享连接上的长事务。
    需要原子性的那三句（见 `save_conversation`）用 `with conn.transaction():` 自己框起来 ——
    **这在 autocommit 下是真的会发 BEGIN/COMMIT 的**（已核实 psycopg 源码：
    `Transaction._push_savepoint` 里 `_outer_transaction = (pgconn.transaction_status == IDLE)`，
    连接空闲时据此在进入时发 BEGIN、退出时发 COMMIT）。这与 CLAUDE.md 里「`conn.read_only`
    在 autocommit 下是假防线」不矛盾：那条说的是 `read_only` 依赖**隐式**事务、而 autocommit
    下从不隐式开事务；`transaction()` 是**显式**开的。

    `statement_timeout` 一并设上：`execute_tool` 与路由函数都没有查询级超时，必须建连就带。
    这里给的是 `postgres_query_timeout` 而不是更长的值——历史读写都是毫秒级的小事务。
    """
    import psycopg

    ms = max(1, int(max(1.0, settings.postgres_query_timeout) * 1000))
    return psycopg.connect(
        **_conninfo(),
        autocommit=True,
        options=f"-c statement_timeout={ms}",
    )


def _get_conn() -> Any:
    """懒连接 + 失败冷却。照 `pg_store._get_conn()`，多一步建表。"""
    global _conn, _failed_at, _last_error

    if _conn is not None and not getattr(_conn, "closed", True):
        # 快路径也要过一遍建表（标志位内是纯内存判断，开销可忽略）。
        # ⚠ 不能只在建连那条路上调：头一次建表若失败（权限/库名不对），
        #   `_schema_ready` 保持 False，而连接此后一直走快路径 —— 表就再也建不出来了。
        ensure_schema(_conn)
        return _conn
    if _failed_at is not None and time.monotonic() - _failed_at < _FAIL_COOLDOWN:
        raise ConvStoreUnavailable(_UNAVAILABLE_MSG)

    with _lock:
        if _conn is not None and not getattr(_conn, "closed", True):
            ensure_schema(_conn)
            return _conn
        try:
            _conn = _connect()
            _failed_at = None
            _last_error = None
        except Exception as exc:  # noqa: BLE001
            _failed_at = time.monotonic()
            _last_error = exc
            raise

    logger.info("对话历史库已连接（%s/%s）", settings.postgres_host, settings.postgres_database)
    ensure_schema(_conn)
    return _conn


def _drop_conn(exc: Exception) -> None:
    """连接本身失效时丢掉它，下次重连。"""
    global _conn, _failed_at, _last_error
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn = None
        _failed_at = time.monotonic()
        _last_error = exc


# 只有「连接本身失效」才值得丢掉重连。刻意不用 psycopg.Error 基类 ——
# 它是所有数据库错误的父类，连语法错误、主键冲突都算，那会把连接白扔一遍。
_CONN_ERRORS = frozenset({"OperationalError", "InterfaceError"})


def _guard(action: str, fn: Any, msg: str = _UNAVAILABLE_MSG) -> Any:
    """统一的错误出口：psycopg 异常 → `ConvStoreUnavailable`（原始异常只进日志）。

    `msg` 是回给用户的文案，**默认值即对话历史那句** —— 现有调用点一个字不用改。
    反馈相关的调用传 `_FEEDBACK_UNAVAILABLE_MSG`：那边失败的是评价、不是对话，
    沿用默认值会让用户读到一件**没发生的事**。

    ⚠⚠ **所有数据库操作在这里串行化**（`_op_lock`）——这是本模块与 `pg_store` 的又一处
    **刻意不同**，改之前先读完：

    `pg_store` 不需要锁，因为它的每次调用都是**单条语句**（本身原子），游标又是现用现建。
    本模块的写入走 `with conn.transaction():` —— upsert + DELETE + INSERT 三句**必须原子**，
    否则崩在 DELETE 与 INSERT 之间会留下一条**一条消息都没有的对话**（用户看到历史对话
    点开是空的）。而连接是**共享**的：一旦一个线程在同一会话上开了显式事务，
    另一个线程的读语句会**加入那个事务**，从而看见「消息已删、还没重新插入」的中间状态。
    单用户 + 毫秒级查询，串行化的代价是零；`statement_timeout` 兜住了病态情况。
    """
    if not ENABLED:
        raise ConvStoreUnavailable(
            "未配置 POSTGRES_HOST，对话历史不会保存到数据库。"
            if msg is _UNAVAILABLE_MSG
            else msg
        )
    with _op_lock:
        try:
            return fn(_get_conn())
        except ConvStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ in _CONN_ERRORS:
                _drop_conn(exc)
            logger.warning("对话历史操作失败（%s）: %s", action, exc)
            raise ConvStoreUnavailable(msg) from exc


def _run(sql: str, params: tuple = (), msg: str = _UNAVAILABLE_MSG) -> list[dict]:
    """执行一条查询，返回 dict 列表。走 `_guard`，失败抛 `ConvStoreUnavailable`。

    `msg` 同 `_guard`：**默认值即对话历史那句**，现有调用点一个字不用改；
    反馈相关的查询（`list_feedback`）传 `_FEEDBACK_UNAVAILABLE_MSG`。

    ⚠ 这个形参不是洁癖：`list_feedback` 一开始漏传，`_guard` 就用默认值抛了
    「未配置 POSTGRES_HOST，对话历史不会保存到数据库」—— 用户点 👍 却读到一句关于
    **对话**的话。（`scripts/probe_feedback.py` ③ 断言「文案必须是反馈那句」时当场逮住。）
    """
    from psycopg.rows import dict_row

    def go(conn: Any) -> list[dict]:
        # 游标**必须现用现建**：psycopg 的 Connection 自带内部锁、可跨线程共享，
        # 但 Cursor 不是线程安全的（路由走 asyncio.to_thread，会并发）。
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    return _guard("查询", go, msg)


def warmup() -> None:
    """预建连接 + 建表。**吞掉所有异常**。

    与 `reranker.warmup()` / `graph_store.warmup()` / `pg_store.warmup()` 同一性质：
    数据库是可选能力，连不上只记警告，不能让整个服务起不来
    （`api.py` 的 `_bootstrap` 里任何异常都会让服务 503）。
    """
    if not ENABLED:
        logger.info("未配置 POSTGRES_HOST，对话历史不会持久化（问答链路不受影响）")
        return
    try:
        _get_conn()  # 成功即意味着建表也跑过了（见 _get_conn）
        logger.info("对话历史库预热完成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("对话历史库预热失败（本次不持久化，问答链路不受影响）: %s", exc)


# ---------------------------------------------------------------- 读
def _meta(rec: dict) -> ConversationMeta:
    return ConversationMeta(
        id=str(rec["id"]),
        title=str(rec["title"] or ""),
        created_at=_to_ms(rec["created_at"]),
        updated_at=_to_ms(rec["updated_at"]),
        message_count=int(rec.get("message_count") or 0),
    )


_COUNT_SUBQUERY = "SELECT count(*) FROM messages m WHERE m.conversation_id = c.id"


def list_conversations(limit: int = 100) -> list[ConversationMeta]:
    """列出对话**元数据**（不含正文），按最近更新倒序。

    ⚠ 只回元数据是**刻意**的：侧栏只需要标题和时间，而单条对话的正文可达几百 KB
    （12 条 sources、`steps[].pg` 整行原文、`reasoning` 全文）。首屏把正文全拉下来
    会让「历史秒出」这个卖点消失。正文由 `get_conversation()` 按需取。
    """
    limit = max(1, min(_LIST_HARD_CAP, int(limit)))
    rows = _run(
        f"SELECT c.id, c.title, c.created_at, c.updated_at, "
        f"({_COUNT_SUBQUERY}) AS message_count "
        f"FROM conversations c ORDER BY c.updated_at DESC, c.id LIMIT %s",
        (limit,),
    )
    return [_meta(r) for r in rows]


def get_conversation(conv_id: str) -> ConversationFull | None:
    """取一条对话的完整正文。**不存在时返回 `None`**（路由转 404）。

    ⚠ `None` 与「连不上」是**两回事**：前者 404、后者 503。都塌成 503 的话用户分不出
    「这条在别处被删了」和「数据库挂了」，前端也会因为一个 404 切进整体降级模式。
    """
    head = _run(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE id = %s",
        (conv_id,),
    )
    if not head:
        return None
    rec = head[0]
    # jsonb 列读回来**自动是 dict**（psycopg 的 JsonbLoader），不用手动 json.loads。
    # 写进去那边则必须包 Jsonb(...) —— 见 save_conversation。
    msgs = _run(
        "SELECT payload FROM messages WHERE conversation_id = %s ORDER BY seq, id",
        (conv_id,),
    )
    return ConversationFull(
        **_meta({**rec, "message_count": len(msgs)}).model_dump(),
        messages=[m["payload"] for m in msgs],
    )


def search_conversations(query: str, limit: int = 20) -> list[ConversationHit]:
    """在**标题与消息正文**里做大小写不敏感的模糊搜索，按最近更新倒序。

    命中位置附近的片段在 Python 侧切（SQL 里切要处理多字节与大小写，得不偿失）——
    所以下面那句 LATERAL 只负责**取回第一条命中的消息正文**，切片交给 `_snippet()`。

    ⚠ 通配符必须转义（复用 `pg_store._like`，**不抄第二份**）：不转义的话用户搜一个 `%`
    就是全表命中 —— 与图谱那边「空 entity 让 `CONTAINS ""` 静默返回交易最多的实体」、
    以及 pg_store 自己那条是同一类**静默错答案**：有结果、看起来合理、但答的不是那个问题。
    """
    limit = max(1, min(_SEARCH_HARD_CAP, int(limit)))
    pattern = f"%{_like(query)}%"
    rows = _run(
        # ⚠ `ESCAPE '\'` 必须与 `_like()` 的转义字符一致（那边转义 `\` `%` `_`）。
        #   少写这个子句，转义过的反斜杠会**再被当成转义符解释一次**，行为随值而变。
        f"SELECT c.id, c.title, c.created_at, c.updated_at, "
        f"({_COUNT_SUBQUERY}) AS message_count, "
        # ⚠ `h.message_id` 是给前端「跳到命中的那一条」用的（侧栏搜索 → 主区滚动定位）。
        #   没有它，点一条搜索结果只知道「这条对话里有」，不知道**是哪一条消息** ——
        #   长对话里就是「搜到了但找不到」。标题命中时整个 LATERAL 是 NULL ⇒ 空串。
        f"h.role AS hit_role, h.content AS hit_content, h.message_id AS hit_message_id "
        f"FROM conversations c "
        f"LEFT JOIN LATERAL ("
        # ⚠ WHERE 用的是**完整** `m.content`（判定要准），SELECT 出来的才是截断值：
        #   命中若落在截断点之后，`_snippet()` 找不到词、退化成开头一段 —— 这是**有意的**，
        #   宁可给一段不含关键词的预览，也不要为了切片段把整条回答拖过网络。
        #   ⚠ `m.id` **不能**跟着一起截断 —— 它是拿去 DOM 里找锚点的，截了就对不上。
        f"  SELECT m.role, m.id AS message_id, left(m.content, {_SNIPPET_SCAN_CHARS}) AS content FROM messages m "
        f"  WHERE m.conversation_id = c.id AND m.content ILIKE %s ESCAPE '\\' "
        f"  ORDER BY m.seq LIMIT 1"
        f") h ON true "
        f"WHERE c.title ILIKE %s ESCAPE '\\' OR h.content IS NOT NULL "
        f"ORDER BY c.updated_at DESC, c.id LIMIT %s",
        (pattern, pattern, limit),
    )
    hits: list[ConversationHit] = []
    for r in rows:
        meta = _meta(r)
        hit_content = r.get("hit_content") or ""
        hits.append(
            ConversationHit(
                **meta.model_dump(),
                snippet=_snippet(hit_content, query) if hit_content else "",
                snippet_role=str(r.get("hit_role") or ""),
                # 标题命中（`h` 整行为 NULL）→ 空串，前端据此**不做跳转**。
                # ⚠ 别退化成「取第一条消息」：那是猜，会把用户送到一个与关键词无关的位置。
                hit_message_id=str(r.get("hit_message_id") or ""),
            )
        )
    return hits


def _snippet(text: str, needle: str) -> str:
    """在 `text` 里找 `needle`（忽略大小写）并取命中位置两侧的窗口，两侧加省略号。"""
    flat = " ".join(text.split())  # 折叠换行/连续空白，否则片段里全是空行
    if not needle:
        return flat[:_SNIPPET_PAD * 2]
    at = flat.lower().find(needle.lower())
    if at < 0:
        # 两种可能：① 命中的是标题、正文没这个词（调用方不会在这条路上调进来）；
        # ② 命中位置落在 `_SNIPPET_SCAN_CHARS` 之后被截掉了。都退回开头一段。
        # 带上尾部省略号——不然它看起来就像个「不含关键词的片段」，用户会以为搜错了。
        return f"{flat[: _SNIPPET_PAD * 2]}{'…' if len(flat) > _SNIPPET_PAD * 2 else ''}"
    start = max(0, at - _SNIPPET_PAD)
    end = min(len(flat), at + len(needle) + _SNIPPET_PAD)
    piece = flat[start:end]
    return f"{'…' if start else ''}{piece}{'…' if end < len(flat) else ''}"


# ---------------------------------------------------------------- 写
def _message_id(payload: dict, index: int, seen: set[str]) -> str:
    """给一条消息定一个**在本对话内唯一**的 id。

    ⚠ 这个兜底不是装饰：`messages` 的主键是 `(conversation_id, id)`，前端若给出重复的 id
    （客户端 bug、或两条消息都缺 id），`executemany` 会撞主键冲突 → **整条对话从此存不进去**，
    而报错在服务端日志里、界面上只是「历史不保存」。宁可换一个自造的 id 也要写成功。
    """
    raw = payload.get("id")
    mid = str(raw).strip() if raw is not None else ""
    if not mid or mid in seen:
        mid = f"seq-{index}"
    seen.add(mid)
    return mid


def save_conversation(conv_id: str, data: ConversationUpsert) -> None:
    """整体 upsert 一条对话。**单事务**，三句话要么全成要么全不成。

    **为什么是整体替换而不是增删改单条**：前端 `regenerate` 会把消息数组**截断**
    （`useChat.ts` 的 `messages.slice(0, cutIndex + 1)`），增量 API 就得让前端告诉服务端
    「删了哪几条」；整体替换天然幂等、**重试安全**，前端也不需要任何 diff 逻辑。

    ⚠ 写 jsonb **必须包 `Jsonb(...)`**：裸的 `dict` 会在运行时抛
    `ProgrammingError: cannot adapt type 'dict'` —— 这**不是类型检查能发现的**，
    只有真跑一次才会暴露。
    """
    from psycopg.types.json import Jsonb

    seen: set[str] = set()
    payloads = [
        (conv_id, _message_id(m, i, seen), i, Jsonb(m)) for i, m in enumerate(data.messages)
    ]

    def go(conn: Any) -> None:
        with conn.transaction():
            with conn.cursor() as cur:
                # title/updated_at 覆盖，**created_at 保持首次那一个**（改标题不该改创建时间）
                cur.execute(
                    "INSERT INTO conversations (id, title, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, "
                    "updated_at = EXCLUDED.updated_at",
                    (conv_id, data.title, _to_ts(data.created_at), _to_ts(data.updated_at)),
                )
                cur.execute("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
                if payloads:
                    cur.executemany(
                        "INSERT INTO messages (conversation_id, id, seq, payload) "
                        "VALUES (%s, %s, %s, %s)",
                        payloads,
                    )

    _guard("保存对话", go)


def delete_conversation(conv_id: str) -> None:
    """删除一条对话。**幂等** —— 删一个不存在的 id 是空操作，不报错。

    消息行靠外键的 `ON DELETE CASCADE` 一起走，不用手动删。
    """

    def go(conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE id = %s", (conv_id,))

    _guard("删除对话", go)


def delete_all_conversations() -> int:
    """清空**所有**对话，返回删掉的条数。消息随外键级联。**幂等** —— 空库上返回 0。

    ⚠⚠ **加 `WHERE` 之前先读这里**：本函数**故意没有任何过滤条件**。日后若引入多用户，
    「删谁的」必须在本函数、`list_conversations`、`_DDL` 三处**同时**改 ——
    `scripts/probe_conv_store.py` ⑬ 有一条「返回值 == 删除前整表 `count(*)`」会当场变红。

    ⚠ `feedback` 表**一个字都不动**（与单条删除一致）：它没有外键，删对话带不走它，
    而「攒下来做分析」的数据本就不该跟着会话走。⑬ 里有一条专门钉这个。

    ⚠ 报错文案用 `_DELETE_ALL_UNAVAILABLE_MSG`，**不是**默认那句（见该常量的注释）。

    ⚠ **不加显式事务**：单条语句在 `autocommit=True` 下自身原子，与 `delete_conversation`
    同构（`save_conversation` 才需要 `with conn.transaction()`，它是三句）。
    """

    def go(conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversations")
            return int(cur.rowcount)

    return int(_guard("清空对话", go, _DELETE_ALL_UNAVAILABLE_MSG) or 0)


# ---------------------------------------------------------------- 反馈
# 表结构见上面 `_DDL` 后那段注释（**为什么没有外键**）。这里只补三件事：
#
# 1. **快照（question / answer / doc_ids）由前端给**，后端不去 `payload` 里翻。
#    `schemas.py` 明写「后端**不解释**它内部的结构，原样进 payload」—— 从 jsonb 里
#    解析出「上一条 user 消息是什么」「sources 里有哪些 doc_id」正是那句话禁止的事。
# 2. **`rating` 只有 1 / -1**，没有第三种「取消」值：取消 = 删掉这一行。
# 3. 反馈与对话历史**共用同一条连接、同一个 `POSTGRES_HOST` 开关**，所以
#    `warmup()` / `ensure_schema()` / `_op_lock` 全部照旧生效，不需要新的初始化路径。


def _fb(rec: dict) -> FeedbackRow:
    """库里的 dict 行 → `FeedbackRow`。照 `_meta()`。

    ⚠ `created_at` / `updated_at` 在这里转成 epoch 毫秒：`timestamptz` 回来是 `datetime`，
    直接塞进响应会在 `api.py` 的 `json.dumps` 处炸成 `TypeError`（本项目第五次同类坑）。
    `doc_ids` 是 jsonb，读回来**已经是 list**（psycopg 的 JsonbLoader），不用再解析。
    """
    return FeedbackRow(
        conversation_id=str(rec["conversation_id"]),
        message_id=str(rec["message_id"]),
        rating=int(rec["rating"]),
        question=str(rec["question"] or ""),
        answer=str(rec["answer"] or ""),
        doc_ids=[str(d) for d in (rec.get("doc_ids") or [])],
        created_at=_to_ms(rec["created_at"]),
        updated_at=_to_ms(rec["updated_at"]),
    )


def save_feedback(conv_id: str, message_id: str, data: FeedbackUpsert) -> None:
    """记一条评价（同一条消息再评一次 = 覆盖）。**幂等**，重试安全。

    `created_at` 保持首次那一个、`updated_at` 覆盖 —— 与 `save_conversation` 里
    `conversations` 的 upsert 约定一致（「第一次评」与「最后一次改」是两个不同的事实）。

    ⚠ `doc_ids` 那列是 jsonb，**必须包 `Jsonb(...)`**：裸的 `list` 会在运行时抛
    `ProgrammingError: cannot adapt type 'list'` —— 这**不是类型检查能发现的**，
    只有真跑一次才会暴露（同 `save_conversation` 里那段 dict 的警告）。
    """
    from psycopg.types.json import Jsonb

    now = dt.datetime.now(tz=dt.timezone.utc)

    def go(conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback (conversation_id, message_id, rating, question, "
                "answer, doc_ids, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (conversation_id, message_id) DO UPDATE SET "
                "rating = EXCLUDED.rating, question = EXCLUDED.question, "
                "answer = EXCLUDED.answer, doc_ids = EXCLUDED.doc_ids, "
                "updated_at = EXCLUDED.updated_at",
                (
                    conv_id,
                    message_id,
                    int(data.rating),
                    data.question,
                    data.answer,
                    Jsonb(list(data.doc_ids)),
                    now,
                    now,
                ),
            )

    _guard("保存反馈", go, _FEEDBACK_UNAVAILABLE_MSG)


def delete_feedback(conv_id: str, message_id: str) -> None:
    """取消一条评价。**幂等** —— 删一个不存在的 key 是空操作，不报错。

    前端「再点一次同一个拇指 = 取消」走的就是这条。幂等是必需的：点击与重试都可能
    重复发，而删两次和删一次的结果必须一样。
    """

    def go(conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM feedback WHERE conversation_id = %s AND message_id = %s",
                (conv_id, message_id),
            )

    _guard("删除反馈", go, _FEEDBACK_UNAVAILABLE_MSG)


def list_feedback(
    rating: int = 0, conversation_id: str = "", limit: int = 100
) -> list[FeedbackRow]:
    """列出反馈，供分析取数。`rating=0` = 不按评价过滤。

    ⚠ WHERE 是**按有没有条件在代码里拼**的，但拼进去的只有 `"rating = %s"` 这种
    **字面量片段**，值一律走 `%s`。这与「绝不把调用方给的字符串拼进 SQL 文本」
    （`pg_store` 的铁律）不冲突 —— 同它用白名单列名拼 SQL 是同一个性质。
    """
    where: list[str] = []
    params: list[Any] = []
    if rating:
        where.append("rating = %s")
        params.append(int(rating))
    if conversation_id:
        where.append("conversation_id = %s")
        params.append(conversation_id)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    cap = max(1, min(_FEEDBACK_HARD_CAP, int(limit)))
    params.append(cap)

    rows = _run(
        "SELECT conversation_id, message_id, rating, question, answer, doc_ids, "
        "created_at, updated_at FROM feedback"
        f"{clause} ORDER BY created_at DESC, message_id LIMIT %s",
        tuple(params),
        _FEEDBACK_UNAVAILABLE_MSG,
    )
    return [_fb(r) for r in rows]


# ---------------------------------------------------------------- 汇总
_STAT_KEYS = ("conversations", "messages", "feedback", "up", "down")


def usage_stats(msg: str = _UNAVAILABLE_MSG) -> dict[str, int]:
    """看板用的聚合计数：五张**精确**的 `count(*)`，一次往返。

    ⚠⚠ **不要用 `len(list_conversations())` / `len(list_feedback())` 代替** ——
    那两个都封顶（`_LIST_HARD_CAP` / `_FEEDBACK_HARD_CAP`，都是 500），
    数据长起来之后会给出一个**看起来完全合理的错误数字**：界面显示「总对话数 500」，
    而实际有 1200 条，不报错、不截断提示。本仓库最警惕的就是这一类。（`probe_conv_store.py` ⑫
    拿它与独立查询交叉对账。）

    `msg` 同 `_run` / `_guard`：**带默认值**，看板要自己的文案就自己传。
    """
    rows = _run(
        "SELECT (SELECT count(*) FROM conversations)              AS conversations,"
        "       (SELECT count(*) FROM messages)                   AS messages,"
        "       (SELECT count(*) FROM feedback)                   AS feedback,"
        "       (SELECT count(*) FROM feedback WHERE rating =  1) AS up,"
        "       (SELECT count(*) FROM feedback WHERE rating = -1) AS down",
        (),
        msg,
    )
    rec = rows[0] if rows else {}
    # ⚠ 逐键 int() + `or 0` 兜底：`count(*)` 回来是 int，但万一被包成 Decimal
    #   或 None（空表 + 某个子查询出岔子），直接塞进报表会在格式化处炸。
    return {k: int(rec.get(k) or 0) for k in _STAT_KEYS}


# ---------------------------------------------------------------- 状态
@lru_cache(maxsize=None)
def _has_module(name: str) -> bool:
    """这个进程里装没装某个包。**结果缓存**，不每次现查。

    ⚠ `importlib.util.find_spec` 每次都实打实走一遍 `sys.path` + `os.stat`，实测约
    **430µs** —— 四个 status 函数各调一次，那就是 `/api/health` 的全部开销（~1.8ms）。
    健康检查每 30 秒被轮询一次，而**装包这件事在一个进程的生命周期里不会变**，
    所以缓存它没有代价。`probe_*` 全离线段不依赖它的实时性（没有任何探针打桩 find_spec）。

    ⚠ 与 `llm_factory.available_providers()` 读活 `settings` **不冲突**：那边缓存不得，
    是因为 settings 会在运行期被改（探针就靠改它验行为）；这边没有对应的场景。
    """
    import importlib.util

    return importlib.util.find_spec(name) is not None


def conv_status() -> dict[str, Any]:
    """可用性摘要（诊断用，如 /api/health）。**不抛异常、不触发连接**。

    照 `pg_status()` 的形状 —— 健康检查每 30 秒被轮询一次，不能被可选能力拖垮。
    """
    if not _has_module("psycopg"):
        return {"configured": False, "available": False, "detail": "未安装 psycopg 驱动"}
    if not ENABLED:
        return {"configured": False, "available": False, "detail": "未配置 POSTGRES_HOST"}
    if _failed_at is not None:
        cooling = _FAIL_COOLDOWN - (time.monotonic() - _failed_at)
        suffix = f"，{cooling:.0f}s 后重试" if cooling > 0 else ""
        return {"configured": True, "available": False, "detail": f"{_last_error}{suffix}"}
    if _conn is None:
        return {"configured": True, "available": False, "detail": "尚未建立连接"}
    # 只报库名，**绝不能把连接串或密码带出去**（同 pg_status）
    return {
        "configured": True,
        "available": True,
        "detail": f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}",
    }
