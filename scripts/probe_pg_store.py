"""独立验证采购记录层：pg_store 的筛选 / 聚合 / 白名单 / 只读防线 / 降级行为。

这一层不依赖大模型、不依赖 Qdrant/BM25，可以单独跑——数据库工具的成败全在这里。

三条最要紧的断言，都是「静默出错」那一类（不报错、不异常，只是数字悄悄不对）：

1. **跨库对账**（②）：`contains='激光打印机'` 必须给出 107 笔 / 973172 元，与
   `probe_graph_store.py` 里那个常数**逐位相同**。两套完全独立的技术栈（PostgreSQL 平表
   vs Neo4j 图）对同一份源数据给出同一个数，是这层最有力的正确性证据；一旦哪天对不上，
   要么本模块的 WHERE 拼错了，要么两份数据不同源了。
2. **只读断言的是行为、不是属性**（⑦）：必须在真连接上执行一条 `DELETE` 并确认它被拒。
   ⚠ **绝不能写成 `assert conn.read_only is True`** —— 实测（psycopg 3.3.5 + autocommit）
   那个属性**不生效时也照样返回 True**，`DELETE` 直接放行。那种断言只制造安全感。
3. **白名单 / 空筛选 / 非法值**（③⑥）：`group_by="province; DROP TABLE ..."` 必须被拒；
   一个条件都不给必须被拒（否则是静默返回全表 8751 行）；`amount_min="五十万"` 必须报错
   而不是**静默丢掉这个条件**（丢掉 = 给出一个看起来完全合理的错误答案）。

跑法：

    uv run python scripts/probe_pg_store.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import pg_store  # noqa: E402
from bidding_qa.config import settings  # noqa: E402

# ---------- 基线常数（全部为实测值，见计划 §〇 的数据画像）----------
TOTAL_ROWS = 8751
TOTAL_AMOUNT = 21856725663.93
# 与 probe_graph_store.py 的 LASER_TOTAL 是**同一个数**：同一份源数据、两套技术栈
LASER_COUNT = 107
LASER_TOTAL = 973172.0
SCHOOL_ROWS = 3202
# 源数据里那笔疑似录入错误的记录（与图谱那笔同源）
ANOMALY_AMOUNT = 6600000000.0

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


# ---------- 1. 接线 ----------
def probe_wiring() -> None:
    banner("① 接线 / 开关一致性")

    # 这一条必须在最前面：Settings 是 extra="ignore"，.env 里写了 POSTGRES_* 而 config.py
    # 没声明字段的话会被**静默丢弃**（NEO4J_* 就这么白写了很久），症状是「明明配了却连不上」
    check(
        "config.py 声明了 postgres_* 字段且读到了 .env 的值",
        hasattr(settings, "postgres_host") and bool(settings.postgres_host.strip()),
        detail=f"postgres_host={settings.postgres_host!r}",
    )
    check(
        "ENABLED == bool(postgres_host.strip())",
        pg_store.ENABLED == bool(settings.postgres_host.strip()),
    )
    for name in ("ENABLED", "warmup", "query", "render_for_model", "pg_status"):
        check(f"模块导出 {name}（与 graph_store 的接口对齐）", hasattr(pg_store, name))

    # **自相矛盾检查**：提示词让模型用某个工具、工具列表里却没有（或反过来）
    from bidding_qa import agent, rag

    has_pg_schema = any(s["function"]["name"] == agent.TOOL_PG for s in agent.TOOL_SCHEMAS)
    check(
        f"agent.TOOL_SCHEMAS 含数据库工具 == pg_store.ENABLED ({pg_store.ENABLED})",
        has_pg_schema == pg_store.ENABLED,
    )
    check(
        "提示词提到数据库工具 == ENABLED",
        ("search_procurement_records" in rag.AGENT_SYSTEM_PROMPT) == pg_store.ENABLED,
    )
    check(
        "agent._HANDLERS 登记了数据库工具",
        agent.TOOL_PG in agent._HANDLERS,
    )


# ---------- 2. 基线与跨库对账 ----------
def probe_baseline() -> None:
    banner("② 基线 / 跨库对账（最有力的一组）")

    result = pg_store.query({"sort_by": "amount"})
    check(f"全表 count(*) == {TOTAL_ROWS}", result.total == TOTAL_ROWS, detail=f"实际 {result.total}")
    check(
        f"全表 sum(中标金额) == {TOTAL_AMOUNT:,.2f}",
        result.extra.get("中标金额合计") == TOTAL_AMOUNT,
        detail=f"实际 {result.extra.get('中标金额合计')}"
        "（差一点点就是被写成了 sum(DISTINCT ...)，见 CLAUDE.md）",
    )

    # 跨库对账：同一份源数据，PostgreSQL 走 ILIKE、Neo4j 走图查询，两个数必须一样
    laser = pg_store.query({"contains": "激光打印机"})
    check(
        f"跨库对账：contains='激光打印机' → {laser.total} 笔（图谱也是 {LASER_COUNT} 笔）",
        laser.total == LASER_COUNT,
        detail=f"实际 {laser.total}",
    )
    check(
        f"跨库对账：金额合计 == {LASER_TOTAL:,.0f}（与 probe_graph_store 的 LASER_TOTAL 同源）",
        laser.extra.get("中标金额合计") == LASER_TOTAL,
        detail=f"实际 {laser.extra.get('中标金额合计')}",
    )

    school = pg_store.query({"category": "学校"})
    check(f"类别=学校 → {SCHOOL_ROWS} 条", school.total == SCHOOL_ROWS, detail=f"实际 {school.total}")

    # 数据画像断言（计划 §0.2）：这几条一旦变了，说明重新入库换了数据源，
    # 那么 group_by 的取舍（为什么没有省份/年份/代理机构）就必须重新标定
    check(
        "group_by 白名单不含 province / year / agency（画像变了要重新标定）",
        not ({"province", "year", "agency"} & set(pg_store.GROUP_COLS)),
        detail=str(sorted(pg_store.GROUP_COLS)),
    )
    pur = pg_store.query({"group_by": "province"})
    check("group_by='province' 被拒（不在白名单里）", not pur.ok)


# ---------- 3. 筛选 ----------
def probe_filters() -> None:
    banner("③ 各筛选槽位 / 转义 / 空值")

    for slot, value, want in (
        ("purchaser", "中国科学技术大学", "采购人"),
        ("supplier", "科技有限公司", "中标人"),
        ("province", "安徽省", "省份"),
        ("city", "合肥市", "市区"),
        ("county", "蜀山区", "县城"),
        ("category", "学校", "类别"),
        ("source", "政府采购", "来源"),
        ("agency", "招标", "代理机构"),
    ):
        result = pg_store.query({slot: value})
        check(
            f"{slot}={value!r} → {result.total} 条，回显「{want}」",
            result.ok and result.total > 0 and want in result.filters,
            detail=f"ok={result.ok} total={result.total} filters={result.filters}",
        )

    # 空串/空白 = 没给，**不能被拼进 WHERE**（否则 ILIKE '%%' 命中全表）
    blank = pg_store.query({"province": "   ", "group_by": "category"})
    check(
        "空白筛选值视同没给",
        blank.ok and "省份" not in blank.filters,
        detail=f"filters={blank.filters}",
    )

    # ILIKE 通配符转义（不转义的话裸 % 命中 8751 行）
    for evil in ("%", "_", "%%%"):
        result = pg_store.query({"contains": evil})
        check(
            f"contains={evil!r} 被转义 → 0 行（不转义会命中全表）",
            result.ok and result.total == 0,
            detail=f"total={result.total}",
        )

    # 值里的 SQL 片段当字面量处理
    result = pg_store.query({"purchaser": "' OR 1=1 --"})
    check(
        "值里的 ' OR 1=1 -- 被当字面量（0 行，不是全表）",
        result.ok and result.total == 0,
        detail=f"total={result.total}",
    )

    # 金额与日期区间的解析（宽容：亿/万/千分位/中文日期）
    result = pg_store.query({"amount_min": "1亿"})
    check(f"amount_min='1亿' 解析为 100000000（{result.total} 条 ≥1 亿）", result.ok and result.total > 0)
    result = pg_store.query({"amount_min": "50万", "amount_max": "1000000"})
    check(
        f"金额区间 50万~100万 → {result.total} 条，回显两个边界",
        result.ok and "金额下限" in result.filters and "金额上限" in result.filters,
        detail=str(result.filters),
    )
    result = pg_store.query({"date_from": "2023-10-16", "date_to": "2023-10-17"})
    check(f"日期区间 → {result.total} 条", result.ok and result.total > 0)
    result = pg_store.query({"date_from": "2023年10月16日", "date_to": "2023-10-17"})
    check("中文日期写法也能解析", result.ok and result.total > 0, detail=str(result.filters))


# ---------- 4. 聚合 ----------
def probe_group() -> None:
    banner("④ 分组聚合 / NULL 组 / 排序稳定")

    for column in pg_store.GROUP_COLS:
        # 不带筛选：这一轮验的是「每个分组列本身能不能跑」，不是筛选（③ 已经验过）。
        # 别在这里给 column 塞一个别的列的值当筛选——那是「城市 = 安徽省」，必然 0 行。
        result = pg_store.query({"group_by": column})
        check(
            f"group_by={column:10s} → {result.total} 组，首组「{result.rows[0].label if result.rows else ''}」",
            result.ok and bool(result.rows) and result.mode == "group",
            detail=f"ok={result.ok} mode={result.mode} note={result.note}",
        )
        counts = [r.count for r in result.rows]
        check(
            f"group_by={column} 按记录数降序",
            counts == sorted(counts, reverse=True),
            detail=str(counts[:6]),
        )

    # NULL 组绝不能霸榜：`代理机构` 有 7698/8751 为空，若把 agency 放进 GROUP_COLS，
    # 排出来第一条就是「（空）7698 条」——不是报错，是看起来完全合理的废话
    result = pg_store.query({"group_by": "city"})
    check(
        "分组结果里没有空标签（NULL 自成一组且往往最大）",
        all(r.label and r.label != "（未填写）" for r in result.rows),
        detail=str([r.label for r in result.rows[:5]]),
    )
    check(
        "有 NULL 被排除时 note 如实说明排除了多少行",
        "未计入分组" in (result.note or ""),
        detail=f"note={result.note}",
    )

    # 明细与聚合**共用同一个 WHERE**：聚合若自己重拼、漏掉筛选，会静默返回全局排行。
    # 用无空值的列（category）验证，这样两个数必须严格相等
    detail = pg_store.query({"category": "学校"})
    grouped = pg_store.query({"category": "学校", "group_by": "category"})
    check(
        f"聚合的记录数（{grouped.extra.get('记录数')}）== 明细的总数（{detail.total}）",
        grouped.extra.get("记录数") == float(detail.total),
        detail="不等说明聚合漏掉了筛选条件",
    )
    check(
        "聚合的金额合计 == 明细的金额合计",
        grouped.extra.get("中标金额合计") == detail.extra.get("中标金额合计"),
        detail=f"{grouped.extra.get('中标金额合计')} vs {detail.extra.get('中标金额合计')}",
    )

    # 同分行的次序必须稳定，否则同一问题两次问会给出不同的「前几名」
    first = [(r.label, r.count) for r in pg_store.query({"group_by": "purchaser"}).rows]
    second = [(r.label, r.count) for r in pg_store.query({"group_by": "purchaser"}).rows]
    check("两次相同调用的分组行序完全一致（有稳定次序键）", first == second)


# ---------- 5. 排序安全 + 异常金额 ----------
def probe_sort() -> None:
    banner("⑤ 排序 / 66 亿异常金额提醒")

    result = pg_store.query({"sort_by": "amount"})
    top = result.rows[0] if result.rows else None
    check(
        f"sort_by=amount 首行是那笔 {ANOMALY_AMOUNT:,.0f}（源数据疑似录入错误）",
        top is not None and top.amount == ANOMALY_AMOUNT,
        detail=f"实际 {top.amount if top else None}",
    )
    check("金额排行时带「疑似录入错误」提醒", "存疑" in pg_store.render_for_model(result))

    normal = pg_store.query({"province": "安徽省"})
    check("普通查询不带那句提醒（数据驱动，不命中就不出现）", "存疑" not in pg_store.render_for_model(normal))

    check(
        "sort_by 默认按发布时间（不让异常记录默认领跑）",
        "发布时间" in pg_store.render_for_model(pg_store.query({"category": "学校"})),
    )


# ---------- 6. 白名单 / 注入 ----------
def probe_whitelist() -> None:
    banner("⑥ 白名单 / 注入（模型给的字符串绝不进 SQL 文本）")

    for field, evil in (
        ("group_by", "province; DROP TABLE bid_records"),
        ("group_by", "category' --"),
        ("sort_by", "金额; --"),
        ("date_field", "published; DELETE FROM bid_records"),
    ):
        result = pg_store.query({field: evil, "province": "安徽省"})
        check(
            f"{field}={evil!r} 被拒（未执行）",
            not result.ok and bool(result.note),
            detail=f"ok={result.ok} note={result.note}",
        )

    # 事后确认表还在、行数没变
    check("注入尝试之后表仍在且行数不变", pg_store.query({"sort_by": "amount"}).total == TOTAL_ROWS)


# ---------- 7. 只读防线 ----------
def probe_readonly() -> None:
    banner("⑦ 只读防线（断言行为，不断言属性）")

    # ⚠ 这段**不能**写成 `assert conn.read_only is True`：实测那个属性在 autocommit 下
    # 完全不生效时也返回 True，而 DELETE 照样放行。只有真执行一次才知道防线在不在。
    # `WHERE false` 保证即使防线失效也是 0 行——是安全的探针。
    try:
        pg_store._run("DELETE FROM bid_records WHERE false")
        check(
            "只读连接拒绝 DELETE",
            False,
            detail="写操作竟然被执行了（WHERE false 所以没有副作用，但只读防线是失效的！）",
        )
    except Exception as exc:  # noqa: BLE001
        check("只读连接拒绝 DELETE", True, detail=f"{type(exc).__name__}: {str(exc)[:90]}")

    check("事后行数不变", pg_store.query({"sort_by": "amount"}).total == TOTAL_ROWS)

    # 结构级第三道防线：源码里不该有**以写操作开头**的 SQL 字符串。
    # ⚠ 正则必须锚在「引号 + 可选的空白 + 关键字」上，即**一条语句的开头**。写成
    # `["'][^"']*DELETE` 那种中间夹任意字符的会在整个文件范围内乱匹配——注释里为了
    # 说明只读原理而提到的 `DELETE FROM ...` 会被判成违规（实测踩过）。
    source = (Path(__file__).resolve().parents[1] / "src" / "bidding_qa" / "pg_store.py").read_text(
        encoding="utf-8"
    )
    offenders = re.findall(
        r"""["']\s*(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b""",
        source,
        flags=re.IGNORECASE,
    )
    check(
        "源码里没有以写操作开头的 SQL 字符串",
        not offenders,
        detail=f"发现 {offenders}",
    )


# ---------- 8. 降级 ----------
def probe_degradation() -> None:
    banner("⑧ 降级：连不上时不抛异常、不泄露密码")

    keep_port = settings.postgres_port
    pg_store._drop_conn(RuntimeError("模拟连接故障"))
    pg_store._failed_at = None  # 清掉冷却，强制真的去连一次
    settings.postgres_port = 1  # 无人监听
    try:
        result = pg_store.query({"province": "安徽省"})
        check(
            "连接不可用时 query() 返回 ok=False 而非抛异常",
            (not result.ok) and bool(result.note),
            detail=f"ok={result.ok} note={result.note}",
        )
        text = pg_store.render_for_model(result)
        check("失败文案明确告诉模型「不要编造」", "不要凭常识编造" in text)
        check(
            "note / 渲染文本里不含密码",
            settings.postgres_password not in text and settings.postgres_password not in (result.note or ""),
            detail="密码泄露到回传给模型的文本里了！",
        )
        print(f"\n--- 降级时回传给模型的文本 ---\n{text}\n")
    except Exception as exc:  # noqa: BLE001
        check("连接不可用时 query() 不抛异常", False, detail=f"{type(exc).__name__}: {exc}")
    finally:
        settings.postgres_port = keep_port
        pg_store._drop_conn(RuntimeError("清理"))
        pg_store._failed_at = None
        pg_store._last_error = None

    # warmup() 必须吞异常（否则 api.py 的 _bootstrap 会把整个服务拖成 503）
    settings.postgres_port = 1
    pg_store._failed_at = None
    try:
        pg_store.warmup()  # 不抛就是通过
        check("warmup() 连不上时吞掉异常（不让服务起不来）", True)
    except Exception as exc:  # noqa: BLE001
        check("warmup() 连不上时吞掉异常（不让服务起不来）", False, detail=f"{type(exc).__name__}: {exc}")
    finally:
        settings.postgres_port = keep_port
        pg_store._drop_conn(RuntimeError("清理"))
        pg_store._failed_at = None
        pg_store._last_error = None

    print(f"  数据库状态: {pg_store.pg_status()}")


# ---------- 9. 渲染 ----------
def probe_render() -> None:
    banner("⑨ render_for_model 的三种文本")

    for label, filters in (
        ("明细", {"contains": "空调"}),
        ("聚合", {"category": "学校", "group_by": "city"}),
        ("失败", {"group_by": "province"}),
    ):
        text = pg_store.render_for_model(pg_store.query(filters))
        # 数据库行不是【资料】，标了 [n] 会被前端和正文角标联动
        if label != "失败":
            check(f"{label}文本带「不要标 [n]」提醒", "不要标注 [n]" in text)
        check(f"{label}文本不提「图谱」（工具各说各的）", "图谱" not in text or label == "失败")
        print(f"\n--- {label} ---\n{text}")

    # 空筛选守卫：一个条件都不给 = 全表 8751 行，模型会当成「你要的都在里面」
    result = pg_store.query({})
    check(
        "空筛选被拒（不静默返回全表）",
        (not result.ok) and bool(result.note) and not result.rows,
        detail=f"ok={result.ok} rows={len(result.rows)}",
    )
    # 但「有排序无筛选」是合法问句（「中标金额最高的项目是哪个」），**不能一起挡掉**
    check(
        "只有 sort_by 无筛选时放行（「金额最高」是正常问句）",
        pg_store.query({"sort_by": "amount"}).ok,
    )

    # 0 行时的自我纠正线索
    result = pg_store.query({"province": "火星省"})
    check(
        "0 行时回吐该字段的真实取值（模型同一轮内自我纠正）",
        result.ok and result.total == 0 and "安徽省" in (result.note or ""),
        detail=f"note={result.note}",
    )

    # 非法筛选值必须报错，**不能静默丢掉这个条件**
    for bad in ({"amount_min": "五十万"}, {"date_from": "去年"}):
        result = pg_store.query({**bad, "province": "安徽省"})
        check(
            f"非法筛选值 {bad} 报错而非忽略",
            (not result.ok) and bool(result.note) and not result.rows,
            detail=f"ok={result.ok} note={result.note}",
        )


def main() -> int:
    print(
        f"PostgreSQL: {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}  "
        f"pg_max_rows={settings.pg_max_rows}  查询超时={settings.postgres_query_timeout}s"
    )
    if not settings.postgres_host.strip():
        print("✗ 未配置 POSTGRES_HOST，无法验证")
        return 1

    pg_store.warmup()
    status = pg_store.pg_status()
    if status["available"]:
        print(f"预热: 连接正常（{status['detail']}）")
    else:
        print(f"✗ 预热失败: {status['detail']}")
        return 1

    probe_wiring()
    probe_baseline()
    probe_filters()
    probe_group()
    probe_sort()
    probe_whitelist()
    probe_readonly()
    probe_degradation()
    probe_render()

    banner(f"结果：{_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
