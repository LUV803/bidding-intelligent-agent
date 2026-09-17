"""知识图谱查询：把 Neo4j 里的固定查询模板封装成 Agent 可调用的工具后端。

**为什么是固定模板而不是让模型自己写 Cypher**：开放 Cypher 要同时防注入、防笛卡尔积、
防慢查询，三件事都做对才敢执行；而本图谱的典型问题就那么几类（某标的物的供应商、某单位
采购过什么、金额排行），模板覆盖得住。模型只填「问哪一类」+「问哪个实体」。

⚠ 三条硬约束，改这个文件前先读：

1. **`warmup()` 必须吞异常**。图谱是可选能力，Neo4j 没起不该让整个问答服务挂掉
   （CLAUDE.md 的「降级而非报错」原则，与 `reranker.warmup()` 同理）。
2. **只读**。会话走 `neo4j.READ_ACCESS`，模板也全是 `MATCH`——两道防线都留着。
3. **绝不执行调用方传来的 Cypher**。`query_type` 走 `_EDGE_QUERIES` / `_HANDLERS` 白名单分派。

对应的图模型是 `batch/知识图谱_导入Neo4j.py` 导的那套（中文标签、**以交易为中心**）：

    (:采购商)-[:采购]->(:交易 {行号, 交易额})
    (:供应商)-[:中标]->(:交易)
    (:代理机构)-[:代理]->(:交易)
    (:交易)-[:标的]->(:标的物 {名称, 交易频次})
    (:交易)-[:发生地]->(:所在地)

用这套而不用 `batch/to_neo4j.py` 那套英文标签的，原因是英文那套把当事方挂在标的物上，
按供应商聚合金额会笛卡尔积（实测单个供应商虚高 24 倍，总量却只错 1.6 倍所以极难察觉）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from .config import settings
from .schemas import GraphResult, GraphRow

logger = logging.getLogger(__name__)

# ---------- 图模型的词汇表 ----------
# 集中放这里：标签名散落在各条 Cypher 里的话，改一处漏一处不会有任何报错
L_ITEM = "标的物"
L_TRADE = "交易"
L_PURCHASER = "采购商"
L_SUPPLIER = "供应商"
L_PLACE = "所在地"

R_ITEM = "标的"
R_WIN = "中标"
R_BUY = "采购"
R_LOC = "发生地"

P_NAME = "名称"
P_AMOUNT = "交易额"
P_FREQ = "交易频次"

# 是否启用图谱工具。**三处必须一致**：agent.py 挂不挂这个工具、rag.py 提示词提不提它、
# 运行时 query() 能不能查。都以这个常量为准，否则会出现「提示词让模型用图谱，
# 工具列表里却没有」这种自相矛盾的状态。
ENABLED = bool(settings.neo4j_uri.strip())

# 定位实体时最多返回多少个候选
LOCATE_CAP = 10

# 明细查询的硬上限（Cypher 里的 LIMIT）。
# 比展示上限大得多：要拿到**确切的命中总数**才能告诉模型「共 98 家，此处列出前 20」。
# 图谱扇出实测最大 216（打印/复印纸的采购商），500 足够兜住且开销可忽略。
HARD_CAP = 500

# ⚠ 绝不要在这些查询里写 sum(DISTINCT t.交易额)。
#   两笔金额相同时 DISTINCT 会吃掉一笔：实测激光打印机 107 笔里只有 88 个不同金额，
#   sum(DISTINCT) 得 921785，正确的 sum() 得 973172 —— 静默少算 5.3%，且不报任何错。
#   （这些查询无 fan-out 顾虑：每笔交易只有 1 个供应商/采购商/所在地，是 MERGE 建边保证的）


# ---------- 实体定位 ----------
# 用户（和模型）不会说「打印/复印纸」这种规范名，一律模糊匹配。
#
# **精确名必须排第一**：搜「打印机」时图谱里真有一个叫「打印机」的节点，如果只按
# 交易频次排，会静默匹配到「激光打印机」（107 笔 > 19 笔）——用户问 A 却拿到 B 的数据，
# 而且看不出哪里不对。精确命中优先，其余候选照频次排、通过 candidates 回给模型，
# 模型想换哪个都有依据。实测 CASE ... ORDER BY 在当前 Cypher 上正常。
#
# 标的物有现成的 交易频次 属性可直接排序；采购商/供应商没有，只能数一下交易数
# （实测 ≈280ms，而按名称长度排序反而更慢，且 size(字符串) 在当前 Cypher 上会报
#  TypeError: expected a map but was String）。
_EXACT_FIRST = "CASE WHEN name = $entity THEN 1 ELSE 0 END DESC"

_QUERY_LOCATE: dict[str, str] = {
    L_ITEM: f"""
        MATCH (x:{L_ITEM}) WHERE x.{P_NAME} CONTAINS $entity
        RETURN x.{P_NAME} AS name, coalesce(x.{P_FREQ}, 0) AS freq
        ORDER BY {_EXACT_FIRST}, freq DESC LIMIT $cap
    """,
    L_PURCHASER: f"""
        MATCH (x:{L_PURCHASER}) WHERE x.{P_NAME} CONTAINS $entity
        OPTIONAL MATCH (x)-[:{R_BUY}]->(t:{L_TRADE})
        WITH x.{P_NAME} AS name, count(t) AS freq
        RETURN name, freq
        ORDER BY {_EXACT_FIRST}, freq DESC LIMIT $cap
    """,
    L_SUPPLIER: f"""
        MATCH (x:{L_SUPPLIER}) WHERE x.{P_NAME} CONTAINS $entity
        OPTIONAL MATCH (x)-[:{R_WIN}]->(t:{L_TRADE})
        WITH x.{P_NAME} AS name, count(t) AS freq
        RETURN name, freq
        ORDER BY {_EXACT_FIRST}, freq DESC LIMIT $cap
    """,
}

# ---------- 四类「某实体 → 关联实体列表」查询 ----------
# 结构完全一样，只是换标签和关系方向，用一张表驱动。
# 每条都把金额一起查出来：金额往往才是用户真正关心的排序依据。
_Q_SUBJECT_SUPPLIERS = f"""
    MATCH (i:{L_ITEM} {{{P_NAME}: $name}})
    MATCH (t:{L_TRADE})-[:{R_ITEM}]->(i)
    MATCH (s:{L_SUPPLIER})-[:{R_WIN}]->(t)
    WITH s.{P_NAME} AS label, count(t) AS cnt, sum(t.{P_AMOUNT}) AS amt
    RETURN label, cnt, amt ORDER BY amt DESC LIMIT $cap
"""

_Q_SUBJECT_PURCHASERS = f"""
    MATCH (i:{L_ITEM} {{{P_NAME}: $name}})
    MATCH (t:{L_TRADE})-[:{R_ITEM}]->(i)
    MATCH (p:{L_PURCHASER})-[:{R_BUY}]->(t)
    WITH p.{P_NAME} AS label, count(t) AS cnt, sum(t.{P_AMOUNT}) AS amt
    RETURN label, cnt, amt ORDER BY amt DESC LIMIT $cap
"""

_Q_PURCHASER_ITEMS = f"""
    MATCH (p:{L_PURCHASER} {{{P_NAME}: $name}})
    MATCH (p)-[:{R_BUY}]->(t:{L_TRADE})-[:{R_ITEM}]->(i:{L_ITEM})
    WITH i.{P_NAME} AS label, count(t) AS cnt, sum(t.{P_AMOUNT}) AS amt
    RETURN label, cnt, amt ORDER BY amt DESC LIMIT $cap
"""

_Q_SUPPLIER_ITEMS = f"""
    MATCH (s:{L_SUPPLIER} {{{P_NAME}: $name}})
    MATCH (s)-[:{R_WIN}]->(t:{L_TRADE})-[:{R_ITEM}]->(i:{L_ITEM})
    WITH i.{P_NAME} AS label, count(t) AS cnt, sum(t.{P_AMOUNT}) AS amt
    RETURN label, cnt, amt ORDER BY amt DESC LIMIT $cap
"""

# query_type -> (中文语义, 在哪个标签里定位实体, Cypher)
_EDGE_QUERIES: dict[str, tuple[str, str, str]] = {
    "subject_suppliers": ("标的物的供应商", L_ITEM, _Q_SUBJECT_SUPPLIERS),
    "subject_purchasers": ("标的物的采购商", L_ITEM, _Q_SUBJECT_PURCHASERS),
    "purchaser_items": ("采购商采购过的标的物", L_PURCHASER, _Q_PURCHASER_ITEMS),
    "supplier_items": ("供应商供过的标的物", L_SUPPLIER, _Q_SUPPLIER_ITEMS),
}

# ---------- 标的物全景：刻意拆成 3 条独立小查询 ----------
# 一条大查询要同时算金额和各方计数，就得把 OPTIONAL MATCH 层层叠起来，那样 sum() 会
# 依赖「每笔交易只挂 1 个供应商」这个假设——今天成立，数据一变就静默虚高。
# 拆开之后，金额那条永远不 fan-out，计数各数各的，互不干扰。
_Q_PROFILE_BASE = f"""
    MATCH (i:{L_ITEM} {{{P_NAME}: $name}})
    MATCH (t:{L_TRADE})-[:{R_ITEM}]->(i)
    RETURN count(t) AS tx, sum(t.{P_AMOUNT}) AS amt
"""

_Q_PROFILE_PARTIES = f"""
    MATCH (i:{L_ITEM} {{{P_NAME}: $name}})
    MATCH (t:{L_TRADE})-[:{R_ITEM}]->(i)
    OPTIONAL MATCH (s:{L_SUPPLIER})-[:{R_WIN}]->(t)
    OPTIONAL MATCH (p:{L_PURCHASER})-[:{R_BUY}]->(t)
    RETURN count(DISTINCT s.{P_NAME}) AS sup, count(DISTINCT p.{P_NAME}) AS pur
"""

_Q_PROFILE_PLACES = f"""
    MATCH (i:{L_ITEM} {{{P_NAME}: $name}})
    MATCH (t:{L_TRADE})-[:{R_ITEM}]->(i)
    MATCH (t)-[:{R_LOC}]->(l:{L_PLACE})
    RETURN l.{P_NAME} AS label, count(t) AS cnt
    ORDER BY cnt DESC LIMIT $cap
"""

_Q_TOP_SUBJECTS = f"""
    MATCH (i:{L_ITEM}) WHERE i.{P_FREQ} IS NOT NULL
    RETURN i.{P_NAME} AS label, i.{P_FREQ} AS cnt
    ORDER BY cnt DESC LIMIT $cap
"""


# ---------- 「列举某一类实体」查询 ----------
# 问「采购企业有哪些」时问题里**没有具体名称、只有类别词**，于是所有实体定位型查询
# （_QUERY_LOCATE / _Q_ENTITY_SEARCH，都是 `名称 CONTAINS $entity`）都无从下手：
# 拿「采购人」去模糊匹配具体单位名**必然 0 命中**。这类问题要的是**全量排行**，
# 所以单开一族模板——与 _Q_SUBJECT_SUPPLIERS 同构，只是不锚定任何实体。
#
# 采购商/供应商都没有现成的频次属性，count(t) 现数。
#
# **按交易笔数排序，不按金额**（与 _Q_SUBJECT_SUPPLIERS 那些按金额排的相反，那是有意的）：
# ① 「有哪些」问的是「谁在采购/供货」，活跃度比金额更贴题，也和 _QUERY_LOCATE 里
#   采购商/供应商候选一律 `freq DESC` 的既有约定一致（标的物才按频次/金额排）；
# ② 单行金额异常不会绑架整张榜——实测行号 3121 那笔「某县残疾人联合会 / 智力精神和
#   重度残肢体疾人托养服务」记了 6,600,000,000（66 亿，是次高那笔的 2.8 倍，疑似源 CSV
#   的录入错误）。按金额排它俩就占住榜首，按笔数排则沉到下面，金额仍在行里照常显示。
def _top_entity_query(kind: str, rel: str, *, filtered: bool) -> str:
    """filtered=True 时按名称过滤（模型顺手把关键词也填上了），否则全量排行。"""
    where = f"WHERE x.{P_NAME} CONTAINS $entity" if filtered else ""
    return f"""
        MATCH (x:{kind}) {where}
        MATCH (x)-[:{rel}]->(t:{L_TRADE})
        WITH x.{P_NAME} AS label, count(t) AS cnt, sum(t.{P_AMOUNT}) AS amt
        RETURN label, cnt, amt ORDER BY cnt DESC, amt DESC LIMIT $cap
    """


# query_type -> (中文语义, 标签, 关系)。语义要写清「什么问句用哪个」：模型很容易把
# 「有哪些采购人」误判成实体查找，然后拿类别词去 entity_search 空手而归。
_TOP_ENTITIES: dict[str, tuple[str, str, str]] = {
    "top_purchasers": ("采购商排行（有哪些采购单位，无需 entity）", L_PURCHASER, R_BUY),
    "top_suppliers": ("供应商排行（有哪些供应商，无需 entity）", L_SUPPLIER, R_WIN),
}

# 三个标签一起搜。`--` 无向匹配对三种关系都成立（前两者出边、标的物是入边）
_Q_ENTITY_SEARCH = f"""
    MATCH (x) WHERE (x:{L_ITEM} OR x:{L_PURCHASER} OR x:{L_SUPPLIER})
      AND x.{P_NAME} CONTAINS $entity
    OPTIONAL MATCH (x)--(t:{L_TRADE})
    RETURN labels(x)[0] AS kind, x.{P_NAME} AS label, count(DISTINCT t) AS cnt
    ORDER BY cnt DESC LIMIT $cap
"""


# ---------- 驱动 ----------
# 连接失败后进入冷却：这段时间内直接判定不可用，不再尝试连接。
#
# 为什么不是「失败一次就永久记住」（reranker 是那么做的）：模型加载失败确实是永久的
# （文件不会自己出现），但 Neo4j 可能比这个服务晚起——永久记住就得重启服务。
# 为什么也不是「每次调用都重试」：URI 指向不可达主机时，每次重试都要耗掉一个
# connection_timeout，模型连调两次工具就是用户干等两分钟。
# 30 秒是这两者之间的折中：不会白等，也会自己恢复。
_FAIL_COOLDOWN = 30.0
_driver: Any = None
_driver_lock = threading.Lock()
_failed_at: float | None = None
_last_error: Exception | None = None


def _get_driver():
    """懒建 driver，失败抛异常（由 query() 兜成 note）。"""
    global _driver, _failed_at, _last_error

    uri = settings.neo4j_uri.strip()
    if not uri:
        raise RuntimeError("未配置 NEO4J_URI")
    if _driver is not None:
        return _driver
    if _failed_at is not None and time.monotonic() - _failed_at < _FAIL_COOLDOWN:
        raise RuntimeError(f"图谱不可用（{_FAIL_COOLDOWN:.0f} 秒后重试）: {_last_error}")

    with _driver_lock:
        if _driver is None:
            try:
                from neo4j import GraphDatabase

                # ⚠ default_access_mode 是 **session** 级配置，传进 driver() 会被拒
                # （实测 6.3.1 报 "Unexpected config keys: default_access_mode"）。
                # 只读防线在 _run 的 session() 上，driver 这里不要传。
                candidate = GraphDatabase.driver(
                    uri,
                    auth=(settings.neo4j_username, settings.neo4j_password),
                    connection_timeout=settings.neo4j_connection_timeout,
                    max_connection_lifetime=settings.neo4j_max_connection_lifetime,
                    max_connection_pool_size=settings.neo4j_max_connection_pool_size,
                )
                # 立刻握手：新 driver 的**首个**查询要付约 2 秒的连接建立开销
                # （实测 2047ms，之后同一条 15ms）。在这里付掉，别让用户等。
                candidate.verify_connectivity()
            except Exception as exc:  # noqa: BLE001
                _failed_at = time.monotonic()
                _last_error = exc
                logger.warning("Neo4j 连接失败，图谱工具将降级 %ss: %s", _FAIL_COOLDOWN, exc)
                raise
            _driver = candidate
            _failed_at = None
            _last_error = None
            logger.info("已连接知识图谱 %s (库=%s)", uri, settings.neo4j_database)
    return _driver


def _drop_driver(exc: Exception) -> None:
    """连接类故障：关掉旧 driver 并进冷却，下次调用重新建连。"""
    global _driver, _failed_at, _last_error
    with _driver_lock:
        driver, _driver = _driver, None
        _failed_at = time.monotonic()
        _last_error = exc
    if driver is not None:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass


def warmup() -> None:
    """预建连接。**吞掉所有异常**。

    与 `reranker.warmup()` 同一性质：图谱是可选能力，连不上只记警告，
    不能让整个服务起不来（api.py 的 _bootstrap 里任何异常都会让服务 503）。
    """
    if not settings.neo4j_uri.strip():
        logger.info("未配置 NEO4J_URI，知识图谱工具将不可用（问答链路不受影响）")
        return
    for step, fn in (("建连", lambda: _get_driver()), ("试查", _warmup_probe)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("知识图谱预热失败（%s），图谱工具本次不可用: %s", step, exc)
            return
    logger.info("知识图谱预热完成")


def _warmup_probe() -> None:
    """建连成功不等于查询路径通（库名、权限、schema 都可能有问题）。
    现在暴露出来，比等用户提问时才炸好。"""
    _run(f"MATCH (n:{L_ITEM}) RETURN count(n) AS c")
    _run(f"MATCH (n:{L_SUPPLIER}) RETURN count(n) AS c")


# 只有「driver 本身失效」才值得丢掉重连。刻意不把 Neo4jError 基类放进来——
# 它是所有服务端错误的父类，连 CypherSyntaxError 都算，那会把 driver 白扔一遍。
_CONN_ERRORS = frozenset({"ServiceUnavailable", "SessionExpired", "AuthError", "ConfigurationError"})


def _run(cypher: str, **params) -> list[dict]:
    """执行一条只读查询，返回 dict 列表。异常向上抛，由 query() 兜。"""
    import neo4j  # 函数级 import：保持本模块在没有 neo4j 时也能被导入

    driver = _get_driver()
    try:
        with driver.session(
            database=settings.neo4j_database,
            default_access_mode=neo4j.READ_ACCESS,
        ) as session:
            return session.execute_read(
                lambda tx: [
                    record.data()
                    for record in tx.run(cypher, timeout=settings.neo4j_query_timeout, **params)
                ]
            )
    except Exception as exc:  # noqa: BLE001
        # AuthError 也会被 driver 抛成 Neo4jError 的子类；这里按类名判，避免 import 期依赖
        if type(exc).__name__ in _CONN_ERRORS:
            _drop_driver(exc)
        raise


def _locate(kind: str, entity: str, cap: int = LOCATE_CAP) -> list[dict]:
    """在某个标签里模糊定位实体，返回按交易数降序的候选。"""
    return _run(_QUERY_LOCATE[kind], entity=entity, cap=cap)


def _rows_from(
    records: list[dict], *, with_amount: bool = True
) -> tuple[list[GraphRow], int, bool]:
    """把查询结果转成 GraphRow 并按 graph_max_rows 截断。

    返回 (截断后的行, 总数, 总数是否只是下限)。明细查询一律用 HARD_CAP 取数，
    所以「取满 HARD_CAP」就意味着真实总数更多（`top_subjects` 实测就撞上了：
    标的物总数远超 500）。这时必须把 total 标成下限，否则模型会照着说
    「图谱里一共 500 种标的物」——一个我们并不知道的数字。
    """
    total = len(records)
    rows = [
        GraphRow(
            label=str(r.get("label") or ""),
            count=int(r.get("cnt") or 0),
            amount=(float(r["amt"]) if with_amount and r.get("amt") is not None else None),
        )
        for r in records[: settings.graph_max_rows]
    ]
    return rows, total, total >= HARD_CAP


# ---------- 各查询类型的处理器 ----------
# 每个返回 GraphResult。**都不抛异常**：失败一律转成带 note 的结果，
# 这样 execute_tool 的「永不抛异常」约定在本层就已经满足。


def _edge_query(query_type: str, label: str, kind: str, cypher: str, entity: str) -> GraphResult:
    """四类「某实体 → 关联实体列表」共用。"""
    found = _locate(kind, entity)
    if not found:
        return GraphResult(
            query_type=query_type,
            query_label=label,
            entity=entity,
            note=f"图谱里没有名称包含「{entity}」的{kind}。请换一个更贴近业务登记的写法。",
        )

    resolved = str(found[0]["name"])
    others = [str(r["name"]) for r in found[1:]]
    records = _run(cypher, name=resolved, cap=HARD_CAP)
    rows, total, capped = _rows_from(records)
    return GraphResult(
        query_type=query_type,
        query_label=label,
        entity=entity,
        resolved=resolved,
        candidates=others,
        total=total,
        total_capped=capped,
        rows=rows,
        truncated=total > len(rows),
    )


def _subject_profile(entity: str) -> GraphResult:
    """标的物全景：交易笔数/成交总额/供应商数/采购商数/地区分布。"""
    query_type = "subject_profile"
    label = "标的物全景"
    found = _locate(L_ITEM, entity)
    if not found:
        return GraphResult(
            query_type=query_type,
            query_label=label,
            entity=entity,
            note=f"图谱里没有名称包含「{entity}」的标的物。请换一个更贴近业务登记的写法。",
        )

    resolved = str(found[0]["name"])
    base = _run(_Q_PROFILE_BASE, name=resolved) or [{}]
    parties = _run(_Q_PROFILE_PARTIES, name=resolved) or [{}]
    places = _run(_Q_PROFILE_PLACES, name=resolved, cap=HARD_CAP)
    rows, place_total, capped = _rows_from(places, with_amount=False)

    return GraphResult(
        query_type=query_type,
        query_label=label,
        entity=entity,
        resolved=resolved,
        candidates=[str(r["name"]) for r in found[1:]],
        total=place_total,
        total_capped=capped,
        rows=rows,
        truncated=place_total > len(rows),
        extra={
            "交易笔数": float(base[0].get("tx") or 0),
            "成交总额": float(base[0].get("amt") or 0.0),
            "供应商数": float(parties[0].get("sup") or 0),
            "采购商数": float(parties[0].get("pur") or 0),
        },
    )


def _top_subjects(entity: str) -> GraphResult:
    """交易频次排行。不需要 entity——entity 非空时当作筛选关键词。"""
    query_type = "top_subjects"
    label = "交易频次排行"
    if entity:
        # 模型有时会顺手把关键词也填上。与其报错，不如用它过滤，行为更接近它的预期
        return _subject_rank(query_type, label, entity)

    records = _run(_Q_TOP_SUBJECTS, cap=HARD_CAP)
    rows, total, capped = _rows_from(records, with_amount=False)
    return GraphResult(
        query_type=query_type,
        query_label=label,
        total=total,
        total_capped=capped,
        rows=rows,
        truncated=total > len(rows),
    )


def _subject_rank(query_type: str, label: str, entity: str) -> GraphResult:
    """带关键词的排行：在匹配到的标的物里按频次排。

    用 HARD_CAP 而非 LOCATE_CAP 定位：要报**确切的命中总数**，
    否则会把「匹配到 15 个、只列了前 10」说成「一共 10 个」。
    """
    found = _locate(L_ITEM, entity, cap=HARD_CAP)
    if not found:
        return GraphResult(
            query_type=query_type,
            query_label=label,
            entity=entity,
            note=f"图谱里没有名称包含「{entity}」的标的物。",
        )
    rows = [
        GraphRow(label=str(r["name"]), count=int(r.get("freq") or 0))
        for r in found[: settings.graph_max_rows]
    ]
    return GraphResult(
        query_type=query_type,
        query_label=label,
        entity=entity,
        total=len(found),
        total_capped=len(found) >= HARD_CAP,
        rows=rows,
        truncated=len(found) > len(rows),
    )


def _top_entities(
    query_type: str, label: str, kind: str, rel: str
) -> Callable[[str], GraphResult]:
    """列举某一类实体的全量排行（采购商 / 供应商）。

    entity 可空——空即全量排行，「有哪些采购企业」要的正是这个；模型顺手把关键词也
    填上了就当作名称过滤，行为更接近它的预期（同 _top_subjects）。
    """

    def handler(entity: str) -> GraphResult:
        records = _run(
            _top_entity_query(kind, rel, filtered=bool(entity)), entity=entity, cap=HARD_CAP
        )
        rows, total, capped = _rows_from(records)
        return GraphResult(
            query_type=query_type,
            query_label=label,
            entity=entity,
            total=total,
            total_capped=capped,
            rows=rows,
            truncated=total > len(rows),
        )

    return handler


# 类别词 → 该改用的列举型查询。
#
# 模型（和用户）问「大类」时会本能地拿类别词去 entity_search，而它按 `名称 CONTAINS`
# 模糊匹配、**必然 0 命中**——图谱里存的都是具体单位的登记全称（实测「采购人」
# 「采购企业」都是 0）。与其让它空手而归、可能就此放弃，不如直接告诉它该改用哪个类型：
# 这条 note 会被原样回传给模型，是「选错工具」最便宜的自我纠正点。
_CATEGORY_HINTS: dict[str, str] = {
    "采购人": "top_purchasers",
    "采购企业": "top_purchasers",
    "采购单位": "top_purchasers",
    "采购商": "top_purchasers",
    "采购方": "top_purchasers",
    "供应商": "top_suppliers",
    "中标人": "top_suppliers",
    "供货商": "top_suppliers",
    "标的物": "top_subjects",
    "货物": "top_subjects",
}


def _entity_search(entity: str) -> GraphResult:
    """跨三个标签模糊查实体名，用于模型拿不准登记名称时先探一下。"""
    query_type = "entity_search"
    label = "实体名查找"
    if not entity:
        return GraphResult(
            query_type=query_type, query_label=label, note="entity_search 需要给出 entity。"
        )
    records = _run(_Q_ENTITY_SEARCH, entity=entity, cap=HARD_CAP)
    if not records:
        hint = _CATEGORY_HINTS.get(entity)
        if hint:
            note = (
                f"「{entity}」是**类别词**而不是实体名——图谱里的名称都是具体单位或货物的"
                f"登记全称，所以按名称匹配不到。要列举这一类实体，请改用 query_type={hint}"
                f"（entity 留空即可，它会给出该类实体的全量排行）。"
            )
        else:
            note = f"图谱里没有名称包含「{entity}」的标的物、采购商或供应商。"
        return GraphResult(
            query_type=query_type,
            query_label=label,
            entity=entity,
            note=note,
        )
    total = len(records)
    rows = [
        GraphRow(
            # 把类型拼进 label：结果混着三种实体，不标类型模型分不清
            label=f"{r.get('kind')}·{r.get('label')}",
            count=int(r.get("cnt") or 0),
        )
        for r in records[: settings.graph_max_rows]
    ]
    return GraphResult(
        query_type=query_type,
        query_label=label,
        entity=entity,
        total=total,
        rows=rows,
        truncated=total > len(rows),
    )


def _unknown(query_type: str) -> Callable[[str], GraphResult]:
    def handler(entity: str) -> GraphResult:
        available = "、".join(sorted(QUERY_TYPE_LABELS))
        return GraphResult(
            query_type=query_type,
            query_label="未知查询",
            entity=entity,
            ok=False,
            note=f"没有名为 {query_type} 的查询类型。可用类型：{available}。",
        )

    return handler


def _edge_handler(
    query_type: str, label: str, kind: str, cypher: str
) -> Callable[[str], GraphResult]:
    return lambda entity: _edge_query(query_type, label, kind, cypher, entity)


# 白名单：query_type → 处理器。**只有这里登记的才会被执行**
_HANDLERS: dict[str, Callable[[str], GraphResult]] = {
    **{qt: _edge_handler(qt, lb, kd, cy) for qt, (lb, kd, cy) in _EDGE_QUERIES.items()},
    **{qt: _top_entities(qt, lb, kd, rl) for qt, (lb, kd, rl) in _TOP_ENTITIES.items()},
    "subject_profile": _subject_profile,
    "top_subjects": _top_subjects,
    "entity_search": _entity_search,
}

# 给 agent.py 生成工具 schema 用：需要 entity 的类型（列举型都不需要）
QUERY_TYPE_LABELS: dict[str, str] = {
    **{qt: lb for qt, (lb, _, _) in _EDGE_QUERIES.items()},
    "subject_profile": "标的物全景（交易笔数/总额/供应商数/采购商数/地区分布）",
    "top_subjects": "交易频次排行（无需 entity）",
    "entity_search": "实体名查找（拿不准登记名称时先探一下）",
    **{qt: lb for qt, (lb, _, _) in _TOP_ENTITIES.items()},
}
# 这几个类型 entity 留空即可（会给出全量排行），不适用实体定位那套「空关键词」拦截
_NO_ENTITY_NEEDED = {"top_subjects", *_TOP_ENTITIES}
ENTITY_REQUIRED = frozenset(QUERY_TYPE_LABELS) - _NO_ENTITY_NEEDED

# 工具 schema 里 enum 列的类型、渲染用的中文名、实际能执行的处理器——
# 这三个集合必须一致。import 期炸掉，好过模型传了个 schema 里有的类型却 KeyError
assert set(_HANDLERS) == set(QUERY_TYPE_LABELS), "查询类型表与处理器表不一致"


def query(query_type: str, entity: str = "") -> GraphResult:
    """执行一次图谱查询。**本函数不抛异常**——失败一律转成带 note 的 GraphResult。

    Agent 的工具执行漏掉 role="tool" 响应会让下一轮请求 400，所以「不抛异常」是硬约定。
    """
    handler = _HANDLERS.get(query_type)
    if handler is None:
        return _unknown(query_type)(entity)

    # ⚠ 空关键词必须挡在这里。定位用的是 `名称 CONTAINS $entity`，而 CONTAINS ""
    #   恒为真——空 entity 会静默返回**交易最频繁的那个实体**的数据，
    #   模型却以为那是它指定的东西。这是最隐蔽的一类错答案：看起来有数据、有数字。
    entity = (entity or "").strip()
    if not entity and query_type in ENTITY_REQUIRED:
        return GraphResult(
            query_type=query_type,
            query_label=QUERY_TYPE_LABELS.get(query_type, query_type),
            ok=False,
            note=(
                f"{query_type} 需要给出 entity（实体名称或关键词）。"
                "不确定图谱里的准确写法时，先用 entity_search 传一个短关键词探一下。"
            ),
        )

    started = time.perf_counter()
    try:
        result = handler(entity)
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识图谱查询失败（%s / %r）: %s", query_type, entity, exc)
        result = GraphResult(
            query_type=query_type,
            query_label=QUERY_TYPE_LABELS.get(query_type, query_type),
            entity=entity,
            ok=False,
            note=(
                f"图谱查询失败（{exc}）。图谱只是增益数据源，请基于已有资料作答；"
                "若这个问题只能靠图谱数据回答，请如实说明图谱暂时不可用，不要凭常识编造。"
            ),
        )
    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "图谱查询 %s(entity=%r) 耗时 %.0fms，命中 %d 条",
        query_type,
        entity,
        result.elapsed_ms,
        result.total,
    )
    return result


# ---------- 渲染给模型 ----------

def _money(amount: float) -> str:
    """金额统一按元显示，带千分位。"""
    return f"{amount:,.0f} 元"


# 这句话必须贴在数据旁边：模型在系统提示词里读到过一遍，但真正决定它怎么写答案的是
# 它刚拿到手的那段 tool 消息。图谱结果不是【资料】，没有 [n] 可标。
_NO_CITE = (
    "（以上是图谱的结构化统计结果：直接陈述数字与结论即可，**不要标注 [n] 引用序号**；"
    "也不要给出这里没有列出的名次或数字。）"
)


def render_for_model(result: GraphResult) -> str:
    """把结果渲染成回传给模型的文本。

    与给前端的结构化数据**刻意分开**：前端可以多显示几行、可以画表格，模型这边的上下文
    要克制，且失败时的措辞直接决定它会不会编造（所以失败文案在第一行就说清「别编」）。
    """
    head = f"【知识图谱查询】{result.query_label}"
    if result.entity:
        head += f"（关键词：{result.entity}）"

    if not result.ok:
        return f"{head}\n{result.note}"
    if result.note and not result.rows and not result.extra:
        return f"{head}\n{result.note}"

    lines = [head]
    if result.resolved and result.resolved != result.entity:
        lines.append(f"实际命中的实体：{result.resolved}")

    if result.extra:
        stats = "、".join(
            f"{k} {_money(v) if '额' in k else int(v)}" for k, v in result.extra.items()
        )
        lines.append(f"汇总：{stats}")

    if result.rows:
        # 撞上服务端硬上限时 total 只是下限，措辞必须跟着变——否则模型会把
        # 「取数上限」当成「图谱里的真实总数」说出去
        total_text = f"{result.total} 条以上" if result.total_capped else f"共 {result.total} 条"
        lines.append(f"明细（{total_text}，此处列出前 {len(result.rows)} 条）：")
        for i, row in enumerate(result.rows, start=1):
            piece = f"{i}. {row.label} —— {row.count} 笔"
            if row.amount is not None:
                piece += f"，{_money(row.amount)}"
            lines.append(piece)
        if result.truncated:
            if result.total_capped:
                lines.append("（还有更多未列出。）")
            else:
                lines.append(f"（另有 {result.total - len(result.rows)} 条未列出。）")
    elif not result.note:
        lines.append("（没有查到记录。）")

    if result.note:
        lines.append(result.note)

    # 命中多个近似实体时，明确告诉模型「以上只针对某一个」，并给出换名的办法。
    # 不合并统计——把「激光打印机」和「标签机/条码打印机」的金额加起来没有意义。
    if result.candidates:
        others = "、".join(result.candidates[:6])
        lines.append(
            f"注意：关键词还匹配到其他实体（{others}）。以上结果**仅针对「{result.resolved}」**。"
            "如果问的不是它，请用上面的准确名称重新查询；"
            "不同实体的交易数据**不能相加**。"
        )

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


def graph_status() -> dict[str, Any]:
    """可用性摘要（诊断用，如 /api/health）。不抛异常、不触发连接。"""
    if not _has_module("neo4j"):
        return {"configured": False, "available": False, "detail": "未安装 neo4j 驱动"}
    if not settings.neo4j_uri.strip():
        return {"configured": False, "available": False, "detail": "未配置 NEO4J_URI"}
    if _failed_at is not None:
        cooling = _FAIL_COOLDOWN - (time.monotonic() - _failed_at)
        suffix = f"，{cooling:.0f}s 后重试" if cooling > 0 else ""
        return {
            "configured": True,
            "available": False,
            "detail": f"{_last_error}{suffix}",
        }
    if _driver is None:
        return {"configured": True, "available": False, "detail": "尚未建立连接"}
    return {"configured": True, "available": True, "detail": settings.neo4j_database}
