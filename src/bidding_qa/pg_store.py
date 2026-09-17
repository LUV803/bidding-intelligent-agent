"""采购记录查询：把 PostgreSQL 里的 `bid_records` 平表封装成 Agent 可调用的工具后端。

**为什么是结构化过滤器而不是让模型写 SQL**：开放 SQL 要同时防注入、防慢查询、防越权写库，
三件事都做对才敢执行；而这张表的典型问题就是「带条件的筛选 + 计数求和」那么几类。
所以改成：**模型只填筛选条件，SQL 由本模块从白名单列名拼、值全走参数绑定**。
灵活度接近自由 SQL，注入面为零。

⚠ 四条硬约束，改这个文件前先读：

1. **`warmup()` 必须吞异常**。数据库是可选能力，PostgreSQL 没起不该让整个问答服务挂掉
   （CLAUDE.md 的「降级而非报错」原则，与 `reranker.warmup()` / `graph_store.warmup()` 同理）。
2. **只读**。见 `_connect()`——**必须走连接参数 `options`，不能用 `conn.read_only = True`**，
   理由写在那段注释里（实测那个属性在 autocommit 下完全不生效）。
3. **绝不把调用方给的字符串拼进 SQL 文本**。列名只能来自本模块的常量表
   （`COLS` / `GROUP_COLS` / `TEXT_COLS` / `DATE_COLS` / `SORT_COLS`），值一律 `%s`。
4. **`query()` 永不抛异常**。Agent 的工具执行漏掉 `role="tool"` 响应会让下一轮请求 400，
   所以失败一律转成带 `note` 的 `PgResult`。

数据由独立脚本 `src/database/to_postgresql.py` 灌入（8751 行 × 19 列）。表结构见那个脚本的
`COLUMN_TYPES`——**两处的列名必须一致**，改了入库脚本就要同步改这里的白名单。
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
import re
import threading
import time
from functools import lru_cache
from typing import Any

from .config import settings
from .schemas import PgResult, PgRow

logger = logging.getLogger(__name__)

TABLE = "bid_records"

# 是否启用数据库查询工具。**三处必须一致**：agent.py 挂不挂这个工具、rag.py 提示词提不提它、
# 运行时 query() 能不能查。都以这个常量为准，否则会出现「提示词让模型用数据库，
# 工具列表里却没有」这种自相矛盾的状态。
ENABLED = bool(settings.postgres_host.strip())

# ---------- 列名白名单（注入面的全部防线）----------
# 模型给的是「语义槽位」（purchaser / province …），槽位到真实列名的映射只在这里定义。
# SQL 文本里出现的列名**只能来自这几张表**，绝不拼接模型给的任何字符串。
COLS: dict[str, str] = {
    "purchaser": "采购人",
    "supplier": "中标人",
    "agency": "代理机构",
    "province": "省份",
    "city": "市区",
    "county": "县城",
    "category": "类别",
    "source": "来源",
}

C_AMOUNT = "中标金额"

# 模糊匹配的参数（contains）扫这几列。**是「或」的关系**：任一一列命中即算命中，
# 所以它比图谱那边的实体匹配宽，别拿它的命中数去跟图谱的对账（见 CLAUDE.md 的工具分工）。
TEXT_COLS: tuple[str, ...] = ("标题", "项目名称", "核心标的物", "标的物")

DATE_COLS: dict[str, str] = {"published": "发布时间", "won_at": "中标时间"}
SORT_COLS: dict[str, str] = {"amount": C_AMOUNT, "published": "发布时间"}

# 可以作为 group_by 的列。**是有意挑过的子集**，实测数据画像决定的：
#   - `province` 不列进来：安徽省占 8683/8751（99.2%），分组出来就一行，是「看起来完全合理
#     的废话」，比报错更难发现。`agency` 同理且更糟——7698/8751 为空，NULL 会自成一桶排第一。
#   - 年份也不列：发布时间只有 2023-10-09 ~ 2023-10-20 这 12 个日期，全表同一年。
# 换库/重新入库后这份画像会变，`scripts/probe_pg_store.py` 里有断言盯着。
GROUP_COLS: dict[str, str] = {
    "city": "市区",
    "county": "县城",
    "category": "类别",
    "source": "来源",
    "purchaser": "采购人",
    "supplier": "中标人",
}

# 基数低、值得在「命中 0 行」时回给模型让它换个值的列。
# 这是模型在**同一轮内**自我纠正的最便宜手段。
# ⚠ 但它只覆盖「筛选值写错」，覆盖不了「问题本身要拆成几问」——别再拿它当「轮数够用」的理由。
_HINT_COLS = frozenset({"省份", "类别", "来源"})

# 连接失败后的冷却时长（秒）。与 graph_store 同一套理由：既不能永久记住失败
# （PostgreSQL 可能比服务晚起），也不能每次都重试（每次都要耗掉一个 connect_timeout）。
_FAIL_COOLDOWN = 30.0

# 一次查询最多取多少行明细（兜底）。真正的展示上限是 settings.pg_max_rows。
_ROW_HARD_CAP = 200

# 金额大到离谱时的提醒阈值。源数据里有一笔 66 亿的异常记录（某县残疾人联合会，
# 与图谱那笔是同一份源数据的同一笔），按金额排序时会排在首位。
_ANOMALY_AMOUNT = 5e9

_lock = threading.Lock()
_conn: Any = None
_failed_at: float | None = None
_last_error: Exception | None = None


# ---------------------------------------------------------------- 筛选值解析
class _BadFilter(Exception):
    """筛选值无法解析或取值非法。**必须报错，不能忽略。**

    静默丢掉一个筛选条件 = 给出一个看起来完全合理的错误答案（用户问「金额过亿的」，
    我们却把全部 8751 条都算进去），这比直接报错危险得多。
    """


def _text(value: Any) -> str:
    """把模型给的值归一成字符串。None / 空白 → ""（调用方视同没给）。"""
    if value is None:
        return ""
    return str(value).strip()


_AMOUNT_RE = re.compile(r"^[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)\s*(亿|万|千|元)?\s*$")
_AMOUNT_MULT = {"亿": 1e8, "万": 1e4, "千": 1e3, "元": 1.0, "": 1.0}


def _amount(value: Any, field: str) -> float:
    """解析金额。接受 1000000 / "1000000" / "1,000,000" / "1亿" / "500万" / "1200元"。

    这里的宽容是**有意的**：模型和用户都会写「过亿」「500万」，硬要求整数只会逼出错误。
    但**解析不出来必须抛**，不能当成没给。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        raise _BadFilter(f"{field} 需要一个数字（元），收到的是布尔值")
    if isinstance(value, (int, float)):
        return float(value)
    raw = _text(value).replace(",", "").replace("，", "").replace(" ", "")
    if not raw:
        raise _BadFilter(f"{field} 是空的")
    match = _AMOUNT_RE.match(raw)
    if not match:
        raise _BadFilter(
            f"{field} 无法解析为金额：{value!r}。请给出以「元」为单位的数字，"
            "例如 100000000 或「1亿」。"
        )
    return float(match.group(1)) * _AMOUNT_MULT[match.group(2) or ""]


def _date(value: Any, field: str, *, is_end: bool) -> dt.date:
    """解析日期。接受 YYYY-MM-DD / YYYY/M/D / YYYY年M月D日 / YYYY-MM / YYYY年M月 / YYYY。

    只给到月或年时按区间端点补齐：`date_from="2023"` → 2023-01-01，`date_to="2023"` → 2023-12-31。
    不补的话「2023 年的项目」会漏掉 12 月 —— 又是一个静默错答案。
    """
    if isinstance(value, (dt.date, dt.datetime)):
        return value.date() if isinstance(value, dt.datetime) else value
    raw = (
        _text(value)
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("/", "-")
        .replace(".", "-")
    )
    parts = [p for p in raw.split("-") if p]
    if not parts or not parts[0].isdigit():
        raise _BadFilter(
            f"{field} 无法解析为日期：{value!r}。请用 YYYY-MM-DD，例如 2023-10-09。"
        )
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else (12 if is_end else 1)
        if not 1 <= month <= 12:
            raise ValueError(month)
        if len(parts) > 2 and parts[2].isdigit():
            day = int(parts[2])
        else:
            day = calendar.monthrange(year, month)[1] if is_end else 1
        return dt.date(year, month, day)
    except (ValueError, IndexError) as exc:
        raise _BadFilter(
            f"{field} 不是合法日期：{value!r}。请用 YYYY-MM-DD，例如 2023-10-09。"
        ) from exc


def _bool(value: Any, field: str, *, default: bool) -> bool:
    """解析布尔。**泄漏恢复路径给的是字符串 "true"**（_normalize_args 只转 integer），
    所以两种形态都要认。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    raw = _text(value).lower()
    if raw in {"true", "1", "yes", "y", "是"}:
        return True
    if raw in {"false", "0", "no", "n", "否"}:
        return False
    raise _BadFilter(f"{field} 需要 true/false，收到 {value!r}")


def _int(value: Any, field: str, *, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise _BadFilter(f"{field} 需要一个整数，收到 {value!r}") from exc
    return max(lo, min(hi, parsed))


def _like(value: str) -> str:
    """转义 ILIKE 的通配符。

    ⚠ **不转义的话 `%` 会匹配全表**：实测裸的 `'%%%'` 命中 8751 行、转义后的 `'%\\%%'`
    命中 0 行。这与图谱那边「空 entity 让 `CONTAINS ""` 静默返回交易最多的实体」是同一类
    错答案——看起来有数据、有数字，其实答的不是那个问题。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- 筛选条件
def _build_where(filters: dict[str, Any]) -> tuple[str, list[Any], dict[str, str], list[str]]:
    """把筛选条件拼成 (WHERE 子句, 参数, 回显, 被拒字段)。

    WHERE 子句**只在这里拼一次**，明细与聚合并用。聚合模式若自己重拼、漏掉筛选，
    `省份 + group_by` 会静默返回全局排行。
    """
    clauses: list[str] = []
    params: list[Any] = []
    echo: dict[str, str] = {}
    bad: list[str] = []

    def add(clause: str, value: Any, label: str, shown: str) -> None:
        clauses.append(clause)
        params.append(value)
        echo[label] = shown

    contains = _text(filters.get("contains"))
    if contains:
        escaped = f"%{_like(contains)}%"
        ors = " OR ".join(f'"{c}" ILIKE %s ESCAPE \'\\\'' for c in TEXT_COLS)
        clauses.append(f"({ors})")
        params.extend([escaped] * len(TEXT_COLS))
        echo["关键词"] = contains

    for key, column in COLS.items():
        value = _text(filters.get(key))
        if not value:
            continue
        add(
            f'"{column}" ILIKE %s ESCAPE \'\\\'',
            f"%{_like(value)}%",
            column,
            value,
        )

    for key, op, label in (
        ("amount_min", ">=", "金额下限"),
        ("amount_max", "<=", "金额上限"),
    ):
        raw = filters.get(key)
        if raw is None or _text(raw) == "":
            continue
        try:
            parsed = _amount(raw, key)
        except _BadFilter as exc:
            bad.append(str(exc))
            continue
        add(f'"{C_AMOUNT}" {op} %s', parsed, label, f"{parsed:,.0f} 元")

    date_field = _text(filters.get("date_field")) or "published"
    if date_field not in DATE_COLS:
        bad.append(
            f"date_field 只能是 {' / '.join(DATE_COLS)} 之一，收到 {date_field!r}。"
        )
        date_column = DATE_COLS["published"]
    else:
        date_column = DATE_COLS[date_field]

    for key, op, is_end, label in (
        ("date_from", ">=", False, "起始日期"),
        ("date_to", "<=", True, "截止日期"),
    ):
        raw = filters.get(key)
        if raw is None or _text(raw) == "":
            continue
        try:
            parsed = _date(raw, key, is_end=is_end)
        except _BadFilter as exc:
            bad.append(str(exc))
            continue
        add(f'"{date_column}" {op} %s', parsed, label, parsed.isoformat())

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params, echo, bad


def _pick(filters: dict[str, Any], key: str, allowed: dict[str, str], label: str) -> str:
    """枚举参数一律走 dict 查找——**绝不 f-string 拼模型给的值**。"""
    value = _text(filters.get(key))
    if not value:
        return ""
    if value not in allowed:
        raise _BadFilter(
            f"{label} 只能是 {' / '.join(allowed)} 之一，收到 {value!r}。"
        )
    return value


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
    """建一个**只读**连接。

    ⚠ **不要用 `conn.read_only = True`**。实测（psycopg 3.3.5 + autocommit）：
    那个属性读完返回 `True`，但服务端 `default_transaction_read_only` 仍是 `off`，
    `DELETE FROM bid_records WHERE false` **照样放行**——它只在 psycopg 自己显式开事务时
    才应用，而 autocommit 下它从不发 `BEGIN`。是个「读起来像防护、实际没生效」的假防线。

    两种真管用的方式（都实测过）：连上之后 `SET default_transaction_read_only = on`，
    或建连时用连接参数 `options`。这里选**后者**——建连就生效，与语句顺序、autocommit
    取值都无关，不可能被后来的改动绕过去。

    `statement_timeout` 一并在这里设上：`execute_tool` 自己没有任何超时，浏览器是唯一的
    取消手段，所以它必须建连就带上，不能指望调用方记得设。
    """
    import psycopg

    ms = max(1, int(max(1.0, settings.postgres_query_timeout) * 1000))
    conn = psycopg.connect(
        **_conninfo(),
        autocommit=True,
        options=f"-c default_transaction_read_only=on -c statement_timeout={ms}",
    )
    _assert_read_only(conn)
    return conn


def _assert_read_only(conn: Any) -> None:
    """建连后立刻验证只读确实生效。**不通过就抛** —— 宁可不提供这个工具，也不开着写权限跑。

    这一道存在的理由就是上面那个假防线：`conn.read_only` 会骗人，服务端的状态不会。
    """
    value = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
    if str(value).lower() not in {"on", "true", "1"}:
        conn.close()
        raise RuntimeError(f"PostgreSQL 连接不是只读的（default_transaction_read_only={value}）")


def _get_conn() -> Any:
    """懒连接 + 失败冷却。照 graph_store._get_driver()。"""
    global _conn, _failed_at, _last_error

    if _conn is not None and not getattr(_conn, "closed", True):
        return _conn
    if _failed_at is not None and time.monotonic() - _failed_at < _FAIL_COOLDOWN:
        raise RuntimeError(f"数据库不可用（{_FAIL_COOLDOWN:.0f} 秒后重试）: {_last_error}")

    with _lock:
        if _conn is not None and not getattr(_conn, "closed", True):
            return _conn
        try:
            _conn = _connect()
            _failed_at = None
            _last_error = None
        except Exception as exc:  # noqa: BLE001
            _failed_at = time.monotonic()
            _last_error = exc
            raise
    logger.info("PostgreSQL 已连接（%s/%s）", settings.postgres_host, settings.postgres_database)
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


# 只有「连接本身失效」才值得丢掉重连。刻意不用 psycopg.Error 基类——
# 它是所有数据库错误的父类，连语法错误都算，那会把连接白扔一遍。
_CONN_ERRORS = frozenset({"OperationalError", "InterfaceError"})


def _run(sql: str, params: tuple = ()) -> list[dict]:
    """执行一条只读查询，返回 dict 列表。异常向上抛，由 query() 兜。"""
    from psycopg.rows import dict_row

    conn = _get_conn()
    try:
        # 游标**必须现用现建**：psycopg 的 Connection 自带内部锁、可跨线程共享，
        # 但 Cursor 不是线程安全的（工具走 asyncio.to_thread，会并发）。
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in _CONN_ERRORS:
            _drop_conn(exc)
        raise


def warmup() -> None:
    """预建连接。**吞掉所有异常**。

    与 `reranker.warmup()` / `graph_store.warmup()` 同一性质：数据库是可选能力，
    连不上只记警告，不能让整个服务起不来（api.py 的 _bootstrap 里任何异常都会让服务 503）。
    """
    if not ENABLED:
        logger.info("未配置 POSTGRES_HOST，采购记录查询工具将不可用（问答链路不受影响）")
        return
    try:
        _get_conn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL 预热失败（建连），数据库工具本次不可用: %s", exc)
        return
    try:
        # 建连成功不等于表在。这里探一下，比等用户提问时才炸好——
        # 也顺手把冷连接的首次开销付掉。
        rows = _run(f"SELECT count(*) AS c FROM {TABLE}")
        logger.info("采购记录库预热完成（%s 行）", rows[0]["c"] if rows else "?")
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL 预热失败（试查 %s），数据库工具本次不可用: %s", TABLE, exc)


# ---------------------------------------------------------------- 查询
def _f(value: Any) -> float | None:
    """Decimal → float。

    ⚠ psycopg 把 `numeric` 取回来是 `Decimal`，而 **`Decimal` 不是 JSON 可序列化的** ——
    这个对象最终会被 `api.py` 的 `json.dumps` 处理，到那一步才炸 `TypeError`。
    构造结果对象时就转掉，别留到线上。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str:
    """date / datetime → ISO 字符串。同上的理由：`date` 也不是 JSON 可序列化的。"""
    if value is None:
        return ""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _detail_row(rec: dict) -> PgRow:
    region = "".join(_text(rec.get(c)) for c in ("省份", "市区", "县城"))
    return PgRow(
        title=_text(rec.get("标题")),
        project=_text(rec.get("项目名称")),
        purchaser=_text(rec.get("采购人")),
        supplier=_text(rec.get("中标人")),
        amount=_f(rec.get(C_AMOUNT)),
        published=_iso(rec.get("发布时间")),
        region=region,
        subject=_text(rec.get("标的物")),
        category=_text(rec.get("类别")),
        source=_text(rec.get("来源")),
    )


def _hints(filters: dict[str, Any]) -> str:
    """命中 0 行时，把低基数筛选列的真实取值回给模型，让它换个值重查。

    这是模型在**同一轮内**自我纠正的最便宜手段——比多给一轮 agent_max_rounds 便宜得多，
    也比让它回头顺着无关资料编强。
    """
    lines: list[str] = []
    for key, column in COLS.items():
        if column not in _HINT_COLS or not _text(filters.get(key)):
            continue
        try:
            rows = _run(
                f'SELECT DISTINCT "{column}" AS v FROM {TABLE} '
                f'WHERE "{column}" IS NOT NULL ORDER BY v LIMIT 20'
            )
        except Exception:  # noqa: BLE001 - 提示取不到就算了，不能因此让整次查询失败
            continue
        values = "、".join(_text(r["v"]) for r in rows if _text(r["v"]))
        if values:
            lines.append(f"该库「{column}」的实际取值有：{values}。请换成其中之一重查。")
    return "\n".join(lines)


def query(filters: dict[str, Any] | None = None) -> PgResult:
    """执行一次采购记录查询。**本函数不抛异常**——失败一律转成带 note 的 PgResult。

    Agent 的工具执行漏掉 role="tool" 响应会让下一轮请求 400，所以「不抛异常」是硬约定。
    """
    filters = dict(filters or {})
    started = time.perf_counter()

    def fail(note: str, *, mode: str = "detail", echo: dict[str, str] | None = None) -> PgResult:
        result = PgResult(
            query_label="采购记录查询",
            ok=False,
            note=note,
            mode=mode,
            filters=echo or {},
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return result

    try:
        group_by = _pick(filters, "group_by", GROUP_COLS, "group_by")
        # 记下 sort_by 是不是**模型明确给的**——空条件守卫要用它区分
        # 「{}（什么都没说）」和「{sort_by: amount}（明说要看金额排行）」（见下面那段注释）。
        asked_sort = _text(filters.get("sort_by"))
        sort_by = _pick(filters, "sort_by", SORT_COLS, "sort_by") or "published"
        date_field = _text(filters.get("date_field"))
        if date_field and date_field not in DATE_COLS:
            raise _BadFilter(
                f"date_field 只能是 {' / '.join(DATE_COLS)} 之一，收到 {date_field!r}。"
            )
    except _BadFilter as exc:
        return fail(str(exc))

    try:
        desc = _bool(filters.get("sort_desc"), "sort_desc", default=True)
        max_rows = _int(
            filters.get("max_rows"),
            "max_rows",
            default=settings.pg_max_rows,
            lo=1,
            hi=_ROW_HARD_CAP,
        )
    except _BadFilter as exc:
        return fail(str(exc))

    where, params, echo, bad = _build_where(filters)
    if bad:
        # 有任何一个筛选值解析不了就整条拒绝，**不执行**——见 _BadFilter 的说明。
        return fail("；".join(bad), echo=echo)

    if not where and not group_by and not asked_sort:
        # 参数**全空**才挡。这时的 8751 行是「随便给我看看」，模型会把它当成「你要的都在里面」，
        # 而用户问的显然不是这个——与图谱那边挡住空 entity 是同一个道理。
        #
        # ⚠ 但**不能把「有排序无筛选」也挡掉**：「中标金额最高的项目是哪个」天然就没有筛选条件，
        # 只有 sort_by=amount，那是用户真会问的问题（答辩现场就会问），挡掉等于这个工具白加。
        # 判据不是「有没有筛选」，而是「模型有没有说出它要什么」——排序字段就是那个「什么」。
        return fail(
            "没有给出任何筛选条件。请至少给出一个（省份/市区/县城、采购人、中标人、类别、"
            "来源、金额区间、日期区间、关键词 contains），或者用 group_by 做分组统计；"
            "若确实要「金额最高的前几个」这类排行，请给出 sort_by。"
        )

    mode = "group" if group_by else "detail"
    label = (
        f"按{GROUP_COLS[group_by]}分组统计" if group_by else "采购记录明细"
    )

    try:
        if group_by:
            column = GROUP_COLS[group_by]
            # ⚠ 分组列必须排掉 NULL：NULL 自成一桶且往往最大（代理机构 7698/8751 为空），
            # 排在第一名会让模型报出「第一名（空）7698 条」这种东西。
            # ⚠ `where` 自带前导空格、这里也必须带上——两处不一致就会拼出
            # `FROM bid_recordsWHERE …` 这种语法错误（实测踩过）。拼 SQL 字符串时
            # 空格和引号一样是语法的一部分。
            not_null = f'"{column}" IS NOT NULL'
            scoped = f"{where} AND {not_null}" if where else f" WHERE {not_null}"
            all_rows = _run(f"SELECT count(*) AS c FROM {TABLE}{where}", tuple(params))
            kept_rows = _run(f"SELECT count(*) AS c FROM {TABLE}{scoped}", tuple(params))
            total_all = int(all_rows[0]["c"] or 0)
            kept = int(kept_rows[0]["c"] or 0)
            groups = _run(
                f"SELECT count(*) AS c FROM (SELECT 1 FROM {TABLE}{scoped} GROUP BY \"{column}\") t",
                tuple(params),
            )
            total = int(groups[0]["c"] or 0)
            records = _run(
                f'SELECT "{column}" AS label, count(*) AS cnt, sum("{C_AMOUNT}") AS amt '
                f"FROM {TABLE}{scoped} "
                f'GROUP BY "{column}" ORDER BY cnt DESC, amt DESC NULLS LAST, label LIMIT %s',
                (*params, min(max_rows, _ROW_HARD_CAP)),
            )
            rows = [
                PgRow(
                    label=_text(r["label"]) or "（未填写）",
                    count=int(r["cnt"] or 0),
                    amount=_f(r["amt"]),
                )
                for r in records
            ]
            sums = _run(
                f'SELECT count(*) AS c, sum("{C_AMOUNT}") AS amt, count("{C_AMOUNT}") AS amt_n '
                f"FROM {TABLE}{scoped}",
                tuple(params),
            )
            extra: dict[str, float] = {
                "记录数": float(sums[0]["c"] or 0),
                "中标金额合计": _f(sums[0]["amt"]) or 0.0,
            }
            note = None
            if total_all > kept:
                note = (
                    f"另有 {total_all - kept} 条记录因为没有填写「{column}」而未计入分组。"
                )
        else:
            column = SORT_COLS[sort_by]
            counted = _run(f"SELECT count(*) AS c FROM {TABLE}{where}", tuple(params))
            total = int(counted[0]["c"] or 0)
            # ⚠ DESC 在 PostgreSQL 里 NULL 排最前，必须显式 NULLS LAST；
            # 末尾再挂一个稳定次序键，否则同分行的顺序会抖。
            direction = "DESC" if desc else "ASC"
            records = _run(
                f'SELECT "标题","项目名称","采购人","中标人","{C_AMOUNT}","发布时间",'
                f'"省份","市区","县城","标的物","类别","来源" '
                f"FROM {TABLE}{where} "
                f'ORDER BY "{column}" {direction} NULLS LAST, "id" LIMIT %s',
                (*params, min(max_rows, _ROW_HARD_CAP)),
            )
            rows = [_detail_row(r) for r in records]
            summed = _run(
                f'SELECT sum("{C_AMOUNT}") AS amt, count("{C_AMOUNT}") AS amt_n '
                f"FROM {TABLE}{where}",
                tuple(params),
            )
            extra = {
                "命中记录数": float(total),
                "中标金额合计": _f(summed[0]["amt"]) or 0.0,
            }
            note = None
            if total == 0:
                hint = _hints(filters)
                if hint:
                    note = hint

        result = PgResult(
            query_label=label,
            ok=True,
            mode=mode,
            filters=echo,
            total=total,
            rows=rows,
            truncated=total > len(rows),
            note=note,
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("采购记录查询失败（%r）: %s", filters, exc)
        return PgResult(
            query_label="采购记录查询",
            ok=False,
            mode=mode,
            filters=echo,
            note=(
                f"数据库查询失败（{exc}）。数据库只是增益数据源，请基于已有资料作答；"
                "若这个问题只能靠采购记录回答，请如实说明数据库暂时不可用，不要凭常识编造。"
            ),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


# ---------------------------------------------------------------- 渲染
def _money(amount: float) -> str:
    """金额统一按元显示，带千分位。"""
    return f"{amount:,.0f} 元"


# 这句话必须贴在数据旁边：模型在系统提示词里读到过一遍，但真正决定它怎么写答案的是
# 它刚拿到手的那段 tool 消息。数据库记录不是【资料】，没有 [n] 可标。
_NO_CITE = (
    "（以上是数据库里的真实采购记录：直接陈述结果即可，**不要标注 [n] 引用序号**；"
    "也不要给出这里没有列出的项目名或数字。）"
)

_ANOMALY_NOTE = (
    "注意：以上金额中存在异常巨大的数字，源数据里疑似有录入错误（实测有一笔 66 亿的记录）。"
    "向用户陈述时请说明该金额存疑，不要当成正常成交额。"
)


def render_for_model(result: PgResult) -> str:
    """把结果渲染成回传给模型的文本。

    与给前端的结构化数据**刻意分开**：前端可以多显示几行、可以画表格，模型这边的上下文
    要克制，且失败时的措辞直接决定它会不会编造（所以失败文案在第一行就说清「别编」）。
    """
    head = f"【数据库查询】{result.query_label}"
    if result.filters:
        head += "（筛选：" + "，".join(f"{k}={v}" for k, v in result.filters.items()) + "）"
    else:
        head += "（未应用任何筛选条件）"

    if not result.ok:
        return f"{head}\n{result.note}"
    if result.note and not result.rows and not result.extra:
        return f"{head}\n{result.note}"

    lines = [head]

    if result.extra:
        stats = "、".join(
            f"{k} {_money(v) if '额' in k else int(v)}" for k, v in result.extra.items()
        )
        lines.append(f"汇总：{stats}")

    if result.rows:
        if result.mode == "group":
            lines.append(f"分组结果（共 {result.total} 组，此处列出前 {len(result.rows)} 组）：")
            for i, row in enumerate(result.rows, start=1):
                piece = f"{i}. {row.label} —— {row.count} 条"
                if row.amount is not None:
                    piece += f"，{_money(row.amount)}"
                lines.append(piece)
        else:
            total_text = f"共 {result.total} 条"
            lines.append(f"明细（{total_text}，此处列出前 {len(result.rows)} 条）：")
            for i, row in enumerate(result.rows, start=1):
                bits = [b for b in (row.project, row.title) if b]
                piece = f"{i}. {' / '.join(bits) if bits else '（无标题）'}"
                if row.purchaser:
                    piece += f" | 采购人：{row.purchaser}"
                if row.supplier:
                    piece += f" | 中标人：{row.supplier}"
                if row.amount is not None:
                    piece += f" | 金额：{_money(row.amount)}"
                if row.published:
                    piece += f" | 发布时间：{row.published}"
                if row.region:
                    piece += f" | 地区：{row.region}"
                if row.subject:
                    piece += f" | 标的物：{row.subject}"
                lines.append(piece)
        if result.truncated:
            unit = "组" if result.mode == "group" else "条"
            lines.append(f"（另有 {result.total - len(result.rows)} {unit}未列出。）")
    elif not result.note:
        lines.append("（没有查到符合条件的记录。）")

    if result.note:
        lines.append(result.note)

    # 源数据那笔 66 亿：按金额排序时它必然排第一，而它看着像录入错误
    # （与图谱那笔是同一份源数据的同一条）。不拦着的话模型会把它当正常成交额报出去。
    if any(r.amount is not None and r.amount >= _ANOMALY_AMOUNT for r in result.rows):
        lines.append(_ANOMALY_NOTE)

    lines.append(_NO_CITE)
    return "\n".join(lines)


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


def pg_status() -> dict[str, Any]:
    """可用性摘要（诊断用，如 /api/health）。**不抛异常、不触发连接**。

    健康检查每 30 秒被轮询一次，不能被可选能力拖垮。
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
    # 只报库名，**绝不能把连接串或密码带出去**
    return {
        "configured": True,
        "available": True,
        "detail": f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}",
    }
