"""验证「列举型问句」的识别与它触发的提示词改动。

这两样都是纯字符串逻辑，不需要大模型、也不需要 Neo4j，所以可以单独跑、跑得飞快。

判错的方向是**不对称**的，用例里两种都覆盖：
- 误判成真 → 首轮白调一次图谱（白掉 agent_max_rounds 里的一轮），所以反例要写得狠
- 误判成假 → 退回纯提示词路径，只是没有确定性兜底

`供应商有哪些情形不得参加投标？` 是最要紧的那条反例：它同时含类别词和「有哪些」，
但问的是法规，实测就是这么被绕过去的。

跑法：

    uv run python scripts/probe_enum_route.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import graph_store, pg_store, rag, web_search  # noqa: E402
from bidding_qa.mcp import web_search_exa  # noqa: E402
from bidding_qa.schemas import Source  # noqa: E402
from bidding_qa.tools import rag_tools  # noqa: E402

# 提示词是按工具拼装的，所以「哪个工具名出现在哪段文本里」得按 2×2 组合逐个验。
# 这几个标记是各工具段独有的短语，用来定位段落（而不是靠「第几个工具」这种序数——
# 序数在工具组合一变就错，这也是本轮把序数措辞去掉的原因）。
MARK_SEARCH = "再次检索知识库"
MARK_GRAPH = "用于查询**历史成交交易的客观记录**"
MARK_DB = "用于查询**采购记录数据库**"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        _failures.append(f"{label}\n   {detail}" if detail else label)


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


# ---------- 1. 列举型问句 ----------
# 用户报的那两条验收问题打头
POSITIVE = [
    ("采购企业有哪些？", "验收问题①"),
    ("有哪些采购人？", "验收问题②"),
    ("有哪些供应商", "同一类 bug 的供应商版"),
    ("列出所有采购单位", "「列出」也是列举标记"),
    ("采购人都有哪些", "类别词在前、列举标记在后"),
    ("都有哪些供应商", "口语化"),
    # 带具体名称的图谱问题也会命中——这是可接受的：强制的是图谱工具本身，
    # 具体填哪个 query_type 由模型定（这里它该选的是 subject_suppliers）
    ("激光打印机都有哪些供应商？", "带具体名称，仍是图谱问题"),
]

NEGATIVE = [
    ("供应商有哪些情形不得参加投标？", "同时含类别词和「有哪些」，但问的是法规"),
    ("评标委员会的专家人数和构成有什么要求？", "法规题"),
    ("单一来源采购公示被质疑怎么办？", "流程题"),
    ("采购人应当在什么时候发布公告？", "有类别词、无列举标记"),
    ("供应商质疑和投诉的时限规定是什么？", "法规题"),
    ("", "空串不能炸"),
]


def probe_is_enum_question() -> None:
    banner("① is_enum_question 正反用例")
    for question, why in POSITIVE:
        check(f"应命中：{question!r}（{why}）", rag.is_enum_question(question), "应为 True")
    for question, why in NEGATIVE:
        check(f"不该命中：{question!r}（{why}）", not rag.is_enum_question(question), "应为 False")


# ---------- 2. 提示词接线 ----------
def probe_prompts() -> None:
    banner("② 提示词接线（普通模式必须零改动）")

    src = [Source(doc_id="d1", index=1, question="q", answer="a", snippet="s", score=1.0)]

    # 普通模式走 /api/chat（答辩主演示），一个字都不该被这次改动碰到
    plain = rag.build_messages("采购企业有哪些？", src)
    check(
        "普通模式 system 不含图谱工具名",
        "search_knowledge_graph" not in plain[0]["content"],
    )
    check("普通模式 user 不含【提醒】", "【提醒】" not in plain[1]["content"])

    agent_msgs = rag.build_agent_messages("采购企业有哪些？", src)
    system = agent_msgs[0]["content"]
    user = agent_msgs[1]["content"]

    # 顺序即优先级：相关性规则必须排在检索规则之前，否则压不过 SYSTEM_PROMPT 第 1 条。
    # 加工具时只允许往**末尾**追加，所以这里断言的是「相关性段 < 检索段 < 每个工具段」。
    i_rel = system.find("它优先于上面的第 1 条")
    i_search = system.find(MARK_SEARCH)
    positions = [i_rel, i_search]
    if graph_store.ENABLED:
        positions.append(system.find(MARK_GRAPH))
    if pg_store.ENABLED:
        positions.append(system.find(MARK_DB))
    check(
        f"system 里各段顺序正确（相关性在前、工具段在后）: {positions}",
        all(p >= 0 for p in positions) and positions == sorted(positions),
        f"rel={i_rel} search={i_search} 全部={positions}",
    )
    check("相关性规则明确要求丢弃无关资料", "就整段丢掉它" in system)

    # 提醒要贴在资料旁边——模型准备照资料作答的那一刻它就在眼皮底下
    check("Agent user 里【提醒】贴在【参考资料】之后", "【提醒】" in user and user.index("【提醒】") > user.index("【参考资料】"))
    check("提醒把列举问题指向 top_purchasers", "top_purchasers" in user)

    # 图谱规则里要写清「列举用 top_*，别拿类别词去 entity_search」
    check("图谱规则含列举路由（top_purchasers / top_suppliers）", "top_purchasers" in system and "top_suppliers" in system)
    check("图谱规则警告不要拿类别词去 entity_search", "拿「采购人」这种类别词去搜必然 0 命中" in system)
    check("图谱规则要求真的列出名称、不要概括成类别", "把这些实体的名称真的列出来" in system)

    # 检索为空时的主力引导语也要有列举路由
    check("AGENT_NO_RESULT_PROMPT 含列举路由", "top_purchasers" in rag.AGENT_NO_RESULT_PROMPT)

    # 数据库那条路由：模型得知道「带条件的筛选」该找谁，而不是继续拿图谱硬撞
    if pg_store.ENABLED:
        check("system 里有数据库工具规则段", MARK_DB in system)
        check(
            "数据库规则写明「一个问题不要既查图谱又查数据库」",
            "不要既查图谱又查数据库" in system,
        )
        check("数据库规则要求不标 [n]", "不要标 [n]" in system)
        check(
            "无命中引导语里也有数据库路由",
            "search_procurement_records" in rag.AGENT_NO_RESULT_PROMPT,
        )


# ---------- 3. 工具组合的 2×2：不该提没挂出去的工具 ----------
def _collect_variant(*, graph: bool, pg: bool, tools_ok: bool = True) -> tuple[str, str, str]:
    """临时翻转两个 ENABLED，取 (系统提示词, 无命中引导语, 资料提醒)。

    三个函数都是**调用时**才读 ENABLED 的，所以这里能直接翻。用完必须还原——
    翻的是模块级常量，漏还原会让后面的用例全跑在错误的前提上。

    ⚠ `tools_ok=False` 模拟的是「后端不支持工具」那一格（`BaseLLMClient.supports_tools`
    为假，`agent.py` 把整张工具表剥空）。它不走 ENABLED，所以是**第三个维度**，
    与前两个交叉 —— 真机上是「Ollama + 这台机器配了图谱/采购库」那种组合。
    """
    keep_g, keep_p = graph_store.ENABLED, pg_store.ENABLED
    graph_store.ENABLED, pg_store.ENABLED = graph, pg
    try:
        return (
            rag._agent_system_prompt(tools_ok),
            rag._no_result_prompt(tools_ok),
            rag._sources_reminder(tools_ok),
        )
    finally:
        graph_store.ENABLED, pg_store.ENABLED = keep_g, keep_p


def probe_tool_matrix() -> None:
    banner("③ 工具组合：提示词里不得出现没挂出去的工具")

    for graph_on in (False, True):
        for pg_on in (False, True):
            system, no_result, reminder = _collect_variant(graph=graph_on, pg=pg_on)
            tag = f"图谱={'开' if graph_on else '关'} 数据库={'开' if pg_on else '关'}"
            named = {
                "search_knowledge_graph": graph_on,
                "search_procurement_records": pg_on,
            }
            for tool, should_appear in named.items():
                for label, text in (("system", system), ("无命中", no_result), ("提醒", reminder)):
                    check(
                        f"[{tag}] {label} 提到 {tool} == {should_appear}",
                        (tool in text) == should_appear,
                        f"{label} 里 {'缺少' if should_appear else '多出了'} {tool}",
                    )
            # 两个可选工具都关掉时必须退回原文——那条路径是没配可选能力时的实际线上行为
            if not graph_on and not pg_on:
                check(
                    f"[{tag}] 无命中引导语退回 SEARCH_ONLY 原文",
                    no_result == rag._AGENT_NO_RESULT_SEARCH_ONLY,
                )
                check(
                    f"[{tag}] 资料提醒退回 SEARCH_ONLY 原文",
                    reminder == rag._SOURCES_REMINDER_SEARCH_ONLY,
                )

    # ---- 第三维：`tools_ok=False`（后端根本不接工具，如官方 deepseek-r1:8b 的 Ollama）----
    # ⚠⚠ 这一格**必须**与上面那两个 ENABLED 交叉着测，不能只测「全关」那一格：
    #   真机上最常见的恰恰是「Ollama + 这台机器图谱/采购库都配好了」——ENABLED 全是真，
    #   只有工具表被剥空。若 `tools_ok` 忘了往上报，提示词就会让模型去调三个本轮
    #   **根本没挂出去**的工具，而模型调不动时会顺着预检索到的无关资料编（盐化工那次）。
    banner("③b 后端不接工具时（tools_ok=False）：两个可选工具一个都不许提")
    for graph_on in (False, True):
        for pg_on in (False, True):
            system, no_result, reminder = _collect_variant(
                graph=graph_on, pg=pg_on, tools_ok=False
            )
            tag = f"图谱={'开' if graph_on else '关'} 数据库={'开' if pg_on else '关'} 工具=不接"
            for tool in ("search_knowledge_graph", "search_procurement_records"):
                for label, text in (("system", system), ("无命中", no_result), ("提醒", reminder)):
                    check(
                        f"[{tag}] {label} 不提 {tool}",
                        tool not in text,
                        f"{label} 里多出了 {tool}（本轮它没挂出去）",
                    )
            # 退回原文这条与 ENABLED 全关时**同形**：那时确实一个工具都没有，措辞该一样
            check(
                f"[{tag}] 无命中引导语退回 SEARCH_ONLY 原文",
                no_result == rag._AGENT_NO_RESULT_SEARCH_ONLY,
            )
            check(
                f"[{tag}] 资料提醒退回 SEARCH_ONLY 原文",
                reminder == rag._SOURCES_REMINDER_SEARCH_ONLY,
            )
            # ⚠ 检索那几段**照旧保留**：预检索仍然会发生，模型得知道那些资料是什么。
            #   整段压掉的话它连「参考资料未必相关」这句安全提示都看不到了。
            check(f"[{tag}] 检索规则仍在（压的是可选工具，不是整段提示词）",
                  "search_bidding_knowledge" in system, True)


# ---------- 3c. 联网 / Exa 那一维：两个 ENABLED × 请求开关 ----------
#
# ⚠⚠ **这一格与上面 ③/③b 是两套机制，别合并。** 图谱/采购库/技能是**进程级**的，提示词
#   在 `_agent_system_prompt()` 那三个 composer 里读 `ENABLED`，所以翻 ENABLED 再直接调它们
#   就能覆盖全组合。而联网与 Exa 是**每请求**的：那两段提示词不在 composer 里，由
#   `build_agent_messages(..., web=, exa=)` 追加，开关值从**工具表反推**。
#   ⇒ 只翻 `ENABLED` 是测不到的，必须连着 `tools_for()` 与 `build_agent_messages` 一起走，
#   这正是 `agent.py` 生产路径上的那两步。
#
# 断言的是 CLAUDE.md 那条不变量：**提示词提到工具 ⟺ 工具列表里有它**。
# 漏了它的表现是「提示词让模型去调一个本轮没挂的工具」，而模型调不动时会顺着
# 预检索到的无关资料编 —— 盐化工那次的形状。
def _collect_web_variant(*, enable_web: bool, tavily_ready: bool, exa_ready: bool) -> tuple[str, list[str]]:
    """临时翻转两个联网 ENABLED，取 (系统提示词, 本轮工具名列表)。用完还原。

    ⚠ 两个 ENABLED 都是**调用期**读的模块级全局（`tools_for()` 里），所以能直接翻；
      漏还原会让后面的用例全跑在错误的前提上（与 `_collect_variant` 同一条纪律）。
    """
    keep_t, keep_e = web_search.ENABLED, web_search_exa.ENABLED
    web_search.ENABLED, web_search_exa.ENABLED = tavily_ready, exa_ready
    try:
        tools = rag_tools.tools_for(enable_web)
        names = [t["function"]["name"] for t in tools]
        # ⚠ 开关值**从工具表反推**，与 `agent.py` 那两行逐字同形 —— 这里若另算一遍
        #   (enable_web and ENABLED)，测的就是探针自己那一份逻辑，生产路径上错了也测不出来。
        system = rag.build_agent_messages(
            "测试问题",
            [],
            None,
            web=rag_tools.TOOL_WEB in names,
            exa=rag_tools.TOOL_EXA in names,
        )[0]["content"]
    finally:
        web_search.ENABLED, web_search_exa.ENABLED = keep_t, keep_e
    return system, names


def probe_web_tool_matrix() -> None:
    banner("③c 联网两维：请求开关 × 两个 ENABLED，提示词与工具表必须同进同出")

    for enable_web in (False, True):
        for tavily_ready in (False, True):
            for exa_ready in (False, True):
                system, names = _collect_web_variant(
                    enable_web=enable_web, tavily_ready=tavily_ready, exa_ready=exa_ready
                )
                tag = (
                    f"开关={'开' if enable_web else '关'} "
                    f"Tavily={'有Key' if tavily_ready else '无Key'} "
                    f"Exa={'有Key' if exa_ready else '无Key'}"
                )
                for tool in (rag_tools.TOOL_WEB, rag_tools.TOOL_EXA):
                    check(
                        f"[{tag}] 提示词提到 {tool} == 工具表里有它",
                        (tool in system) == (tool in names),
                        f"工具表={names}；提示词里"
                        f"{'缺少' if tool in names else '多出了'} {tool}",
                    )
                # ⚠⚠ Exa **依附于** search_web：它单独在场是不允许的组合
                #   （`tools_for()` 里三个条件合取，Exa 的提示词段也只讲「换引擎」，
                #   前一段不在的话它是在讲一个不存在的对照物）。
                check(
                    f"[{tag}] Exa 不会单独在场",
                    not (rag_tools.TOOL_EXA in names and rag_tools.TOOL_WEB not in names),
                    f"工具表={names}",
                )

    # 全关那一格：两段提示词**一个字都不许出现**。这是默认配置（两个 Key 都可能为空），
    # 也是「不传 web/exa 时逐字节等于改动前」那条冻结路径的前提。
    system, names = _collect_web_variant(enable_web=False, tavily_ready=True, exa_ready=True)
    check(
        "[两个 Key 都有、开关关] 提示词完全不提两个联网工具",
        rag_tools.TOOL_WEB not in system and rag_tools.TOOL_EXA not in system,
        "开关没开却提了工具",
    )
    check(
        "[两个 Key 都有、开关关] 工具表只有进程级那几个",
        rag_tools.TOOL_WEB not in names and rag_tools.TOOL_EXA not in names,
        f"工具表={names}",
    )


def probe_routing_is_tool_neutral() -> None:
    banner("④ 资料提醒必须工具中立（盐化工 bug 的复活点）")
    # 带筛选条件的问题（「安徽省 2023 年中标金额超 500 万的项目」）**必然**预检索到
    # 几篇字面相近的无关资料。若这段提醒点名某个具体工具，模型就会去调那个——
    # 而图谱根本没有省份/时间/金额区间这几个维度，返回 0 行后它只能回头顺着无关资料编。
    _system, _no_result, reminder = _collect_variant(graph=True, pg=True)
    check(
        "提醒里给的是分工，不是「改用某个工具」",
        "按系统提示词里的工具路由换一个更合适的工具" in reminder,
    )
    check("提醒同时列出图谱与数据库两条路", "search_knowledge_graph" in reminder and "search_procurement_records" in reminder)


def main() -> int:
    probe_is_enum_question()
    probe_prompts()
    probe_tool_matrix()
    probe_web_tool_matrix()
    probe_routing_is_tool_neutral()

    banner("结果")
    if _failures:
        print(f"{len(_failures)} 项失败：")
        for f in _failures:
            print(" -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
