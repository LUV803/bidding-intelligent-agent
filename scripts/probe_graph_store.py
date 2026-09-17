"""独立验证知识图谱层：graph_store 的 9 个查询类型 + 只读防线 + 降级行为。

这一层不依赖大模型、不依赖 Qdrant/BM25，可以单独跑——图谱工具的成败全在这里，
所以先把它验穿，再去接 Agent。

最要紧的一条是 **sum(DISTINCT) 陷阱的回归断言**：金额写错不会报错、不会异常，
只会静默少算几个百分点（实测 5.3%）。没有这条断言，日后有人「顺手优化」成
sum(DISTINCT ...) 谁都不会发现。

跑法：

    uv run python scripts/probe_graph_store.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import graph_store  # noqa: E402
from bidding_qa.config import settings  # noqa: E402

# 激光打印机的成交总额。这个数字=973172 是**用 sum() 实测**的，
# sum(DISTINCT) 会给出 921785（107 笔里 19 笔金额重复，被去重掉了）。
LASER_TOTAL = 973172.0

_pass = 0
_fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _pass, _fail
    if ok:
        _pass += 1
        print(f"  [OK  ] {label}")
    else:
        _fail += 1
        print(f"  [FAIL] {label}")
        if detail:
            print(f"         {detail}")


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


# ---------- 1. 九个查询类型各跑一次 ----------
def probe_query_types() -> None:
    banner("① 九个查询类型（正常关键词）")

    cases = [
        ("subject_suppliers", "激光打印机", True),
        ("subject_purchasers", "激光打印机", True),
        ("subject_profile", "激光打印机", True),
        ("purchaser_items", "幼儿园", True),
        ("supplier_items", "科技有限公司", True),
        ("top_subjects", "", True),
        ("entity_search", "打印机", True),
        ("top_purchasers", "", True),
        ("top_suppliers", "", True),
    ]
    for query_type, entity, want_ok in cases:
        result = graph_store.query(query_type, entity)
        ok = (result.ok == want_ok) and (not want_ok or bool(result.rows))
        check(
            f"{query_type:20s} entity={entity!r:16s} → {len(result.rows)}/{result.total} 条"
            f"  {result.elapsed_ms:.0f}ms",
            ok,
            detail=f"ok={result.ok} note={result.note}",
        )


# ---------- 2. 金额正确性（sum(DISTINCT) 陷阱的回归断言）----------
def probe_money() -> None:
    banner("② 金额回归断言（sum(DISTINCT) 陷阱）")
    result = graph_store.query("subject_profile", "激光打印机")
    total = result.extra.get("成交总额")
    check(
        f"激光打印机成交总额 = {total} （期望 {LASER_TOTAL}）",
        total == LASER_TOTAL,
        detail="金额对不上！先检查 _Q_PROFILE_BASE 是不是被改成了 sum(DISTINCT ...)",
    )

    # 明细里的金额之和不应超过总额（超了说明聚合时 fan-out 了）
    detail_sum = sum(r.amount or 0 for r in graph_store.query("subject_suppliers", "激光打印机").rows)
    check(f"供应商明细金额合计 {detail_sum:,.0f} ≤ 总额 {LASER_TOTAL:,.0f}", detail_sum <= LASER_TOTAL)


# ---------- 3. 模糊定位与精确名优先 ----------
def probe_locate() -> None:
    banner("③ 模糊定位 / 精确名优先 / 候选回传")

    # 图谱里真有一个叫「打印机」的节点，也有「激光打印机」。精确名必须赢，
    # 否则用户问「打印机」会静默拿到「激光打印机」的数据
    result = graph_store.query("subject_suppliers", "打印机")
    check(
        f"entity='打印机' 精确命中「打印机」而非「激光打印机」（实际={result.resolved!r}）",
        result.resolved == "打印机",
        detail=f"resolved={result.resolved}",
    )
    check(
        f"其余候选一并回传（{len(result.candidates)} 个：{'、'.join(result.candidates[:4])}）",
        "激光打印机" in result.candidates,
    )

    # 只有模糊词时按规模降序
    result = graph_store.query("subject_profile", "复印纸")
    check(
        f"entity='复印纸' 模糊匹配到 {result.resolved!r}，共 {result.total} 条地区分布",
        result.resolved is not None and result.ok,
    )

    # 公司类实体（走的是 WITH + ORDER BY CASE 那条模板）
    result = graph_store.query("purchaser_items", "幼儿园")
    check(
        f"采购商模糊定位 → {result.resolved!r}，{result.total} 个标的物",
        result.ok and result.resolved is not None and result.total > 0,
        detail=f"note={result.note}",
    )


# ---------- 3b. 列举型查询（「有哪些采购企业」）----------
def probe_enum_queries() -> None:
    banner("③b 列举型查询：top_purchasers / top_suppliers")

    for query_type in ("top_purchasers", "top_suppliers"):
        result = graph_store.query(query_type, "")
        # entity 留空是**正常用法**（列举全部），不能被 ENTITY_REQUIRED 那道空关键词拦截
        check(
            f"{query_type} 空 entity 可用（列举全部）→ {len(result.rows)}/{result.total} 条"
            f"  {result.elapsed_ms:.0f}ms",
            result.ok and bool(result.rows),
            detail=f"ok={result.ok} note={result.note}",
        )
        check(
            f"{query_type} 命中数超上限时 total_capped=True（不谎报总数）",
            result.total_capped and result.truncated,
            detail=f"total={result.total} capped={result.total_capped} truncated={result.truncated}",
        )
        # **按笔数排序、不是按金额**：源数据里那笔 66 亿（行号 3121，疑似录入错误）
        # 若按金额排会霸榜，用户问「有哪些采购企业」先看到的是一笔错账
        counts = [r.count for r in result.rows]
        check(
            f"{query_type} 按交易笔数降序（不被单行金额异常绑架）",
            counts == sorted(counts, reverse=True),
            detail=str(counts),
        )
        check(
            f"{query_type} 首行是具体单位名：{result.rows[0].label if result.rows else '（空）'}",
            bool(result.rows) and len(result.rows[0].label) > 3 and result.rows[0].count > 0,
        )

    # 模型顺手把关键词也填上时，当作名称过滤，而不是回一句「不需要 entity」
    result = graph_store.query("top_purchasers", "管理局")
    check(
        f"top_purchasers(entity='管理局') 只返回含该词的采购商（{result.total} 条）",
        result.ok and bool(result.rows) and all("管理局" in r.label for r in result.rows),
        detail=str([r.label for r in result.rows[:3]]),
    )

    # 类别词走 entity_search 必然 0 命中（它按名称模糊匹配，图谱里存的是具体单位名）。
    # note 必须把模型引到正确的类型上——这是「拿采购人去 entity_search」那条死路的
    # 自我纠正点，也是用户报的那个 bug 里走不通的方案之一
    for category, want in (("采购人", "top_purchasers"), ("供应商", "top_suppliers")):
        result = graph_store.query("entity_search", category)
        check(
            f"entity_search({category!r}) 0 命中，note 指向 {want}",
            (not result.rows) and want in (result.note or ""),
            detail=f"rows={len(result.rows)} note={result.note}",
        )
    # 反向：普通未命中词不该被误报成类别词（note 文案被改坏时这条会红）
    result = graph_store.query("entity_search", "完全不存在的实体XYZ123")
    check(
        "普通未命中词不会被误报成类别词",
        (not result.rows) and "类别词" not in (result.note or ""),
        detail=f"note={result.note}",
    )


# ---------- 4. 空结果与白名单 ----------
def probe_empty_and_whitelist() -> None:
    banner("④ 空结果 / 未知 query_type")

    result = graph_store.query("subject_suppliers", "完全不存在的标的物XYZ123")
    check(
        "不存在的实体：ok=True 且有可读的 note，不抛异常",
        result.ok and bool(result.note) and not result.rows,
        detail=f"ok={result.ok} note={result.note}",
    )
    check("不存在的实体：note 里给了模型下一步怎么办", "换一个" in (result.note or ""))

    for evil in ("DROP DATABASE neo4j", "locate", "", "SELECT * FROM x"):
        result = graph_store.query(evil, "x")
        check(
            f"未知类型 {evil!r} 被白名单挡住（未执行任何 Cypher）",
            not result.ok and "search_knowledge_graph" not in (result.note or ""),
            detail=f"note={result.note}",
        )

    # 缺 entity 的必填类型。这条最要命：定位走 `CONTAINS $entity`，而 CONTAINS ""
    # 恒为真，不挡住就会静默返回「交易最频繁的那个实体」的数据
    result = graph_store.query("subject_suppliers", "")
    check(
        "entity 为空被挡住（不会静默返回别的实体）",
        (not result.ok) and not result.rows and bool(result.note),
        detail=f"ok={result.ok} rows={len(result.rows)} note={result.note}",
    )


# ---------- 5. 只读防线 ----------
def probe_readonly() -> None:
    banner("⑤ 只读防线（写操作必须被拒）")

    # 这条 Cypher 即便真的执行也改不了任何东西：标签不存在，匹配不到节点。
    # 用它试探只读模式是否生效，零副作用。
    probe_cypher = "MATCH (n:__probe_不存在的标签XYZ) SET n.__probe = 1"
    try:
        graph_store._run(probe_cypher)
        rejected, detail = False, "写入竟然被执行了（没有任何副作用，但只读模式没生效）"
    except Exception as exc:  # noqa: BLE001
        rejected, detail = True, f"{type(exc).__name__}: {str(exc)[:120]}"
    check("只读会话拒绝写操作", rejected, detail=detail)

    banner("⑤b 渲染给模型的文本")
    for query_type, entity in (
        ("subject_suppliers", "激光打印机"),
        ("subject_profile", "激光打印机"),
        ("subject_suppliers", "完全不存在的标的物XYZ123"),
    ):
        text = graph_store.render_for_model(graph_store.query(query_type, entity))
        print(f"\n--- {query_type}({entity!r}) 回传给模型的文本 ---")
        print(text)
    print()


# ---------- 6. 降级 ----------
def probe_degradation() -> None:
    banner("⑥ 降级：连接不可用时不抛异常")

    graph_store._drop_driver(RuntimeError("模拟连接故障"))
    try:
        result = graph_store.query("subject_suppliers", "激光打印机")
        check(
            "driver 不可用时 query() 返回 ok=False 而非抛异常",
            (not result.ok) and bool(result.note),
            detail=f"ok={result.ok} note={result.note}",
        )
        text = graph_store.render_for_model(result)
        check("失败文案里明确告诉模型「不要编造」", "不要凭常识编造" in text or "不要编造" in text)
        print(f"\n--- 降级时回传给模型的文本 ---\n{text}\n")
    except Exception as exc:  # noqa: BLE001
        check("driver 不可用时 query() 不抛异常", False, detail=f"{type(exc).__name__}: {exc}")

    # 未知 URI 的启动路径也要能走通（warmup 吞异常）
    graph_store._failed_at = None
    graph_store._last_error = None
    print(f"  图谱状态: {graph_store.graph_status()}")


def main() -> int:
    print(f"Neo4j: {settings.neo4j_uri}  库={settings.neo4j_database}  "
          f"graph_max_rows={settings.graph_max_rows}")
    if not settings.neo4j_uri.strip():
        print("✗ 未配置 NEO4J_URI，无法验证")
        return 1

    graph_store.warmup()
    if graph_store.graph_status()["available"]:
        print("预热: 连接正常")
    else:
        print(f"✗ 预热失败: {graph_store.graph_status()['detail']}")
        return 1

    probe_query_types()
    probe_money()
    probe_locate()
    probe_enum_queries()
    probe_empty_and_whitelist()
    probe_readonly()
    probe_degradation()

    banner(f"结果：{_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
