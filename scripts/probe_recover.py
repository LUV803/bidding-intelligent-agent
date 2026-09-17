"""验证从「漏进正文的工具调用标记」里恢复 ToolCall。

第一个用例就是线上真实泄漏的原文：模型把两个工具名揉成了一个不存在的名字、
参数写成 keyword、枚举写成 entity_search 的反序。**用例 4 起全是「必须丢弃」
的负例**——恢复逻辑宁可返回空也不能猜，猜错会执行一个模型根本没打算发起的调用。

⚠⚠ **夹具里出现的「假名」一律用 2026-09-20 改名前的旧名**
（`query_knowledge_graph` / `search_knowledge_base` / `query_procurement_records`）：
它们现在既不存在、又长得极像真名，是「幻觉名」最自然的测试夹具，也正是模型会
真吐出来的形状（旧文档 / 旧 few-shot 残留）。**别把它们「顺手改成新名」** ——
改了之后名字会精确命中真工具，本文件里靠「名字对不上 → 退回参数投票」的用例
（用例 1、反向 ③）会**静默切换成「名字优先」路径**，断言照样绿、覆盖没了。

反过来，凡是要验「名字精确命中」的用例（用例 2、3c、反向 ①②）夹具名**必须**
是与 `tools/rag_tools.py` 对齐的真名，否则测的也是投票路径。

⚠ 两者一起构成完整覆盖：**真名走名字路径**，**旧名走投票路径**。

跑法：

    uv run python scripts/probe_recover.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel  # noqa: E402

from bidding_qa.agent import recover_tool_calls  # noqa: E402
from bidding_qa.llm import strip_tool_markup  # noqa: E402
from bidding_qa.tools import get_tool, registered_names, to_openai_schema  # noqa: E402
from bidding_qa.tools import rag_tools as rt  # noqa: E402

BAR = chr(0xFF5C)
M = BAR * 2 + "DSML" + BAR * 2  # 标记 token

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(f"{label}\n   got ={got}\n   want={want}")


# ---- 用例 0：schema 形状（Pydantic 生成 + `tools/base.py` 的清洗层）----
#
# ⚠⚠ 这一组守的东西**没有任何别的测试看得见**：清洗层一旦被删/被简化，`top_k` 会退化成
# Pydantic 的天然形状 `anyOf: [{type:integer,…},{type:null}]` —— **没有顶层 `type`**。
# 而 `_normalize_args` 判的是 `prop.get("type") == "integer"`：`.get` 返回 `None`，
# 于是它**掉进 else 分支把原值原样收下**。恢复路径传进来的是从标记里抠出来的**字符串**，
# 所以一次 `top_k="5"` 的恢复会变成字符串 `"5"` 交给 handler —— 不报错、不崩溃，
# 只是类型变了。本仓库最警惕的那类静默降级。
#
# ⚠ `required` 也在这里：`search_procurement_records` 的 `required` **必须仍为空**，
# 它是两处逻辑的前提（见「用例 3b」「反向 ②」的注释）—— 少了它，
# `recover_tool_calls` 那条「名字对上了但一个参数都不沾 → 退回投票」的守卫就没了前提，
# 会执行一次**没有任何筛选条件的全表查询**。
EXPECTED_SHAPE: dict[str, dict] = {
    rt.TOOL_SEARCH: {
        "required": ["query"],
        "properties": ["query", "top_k"],
        "integers": ["top_k"],
        "enums": [],
    },
    rt.TOOL_GRAPH: {
        "required": ["query_type"],
        "properties": ["query_type", "entity"],
        "integers": [],
        "enums": ["query_type"],
    },
    rt.TOOL_PG: {
        "required": [],  # ⚠⚠ 见上，别「顺手补一个」
        "properties": [
            "contains", "purchaser", "supplier", "agency", "province", "city", "county",
            "category", "source", "amount_min", "amount_max", "date_from", "date_to",
            "date_field", "sort_by", "sort_desc", "group_by", "max_rows",
        ],
        "integers": ["max_rows"],
        "enums": ["date_field", "sort_by", "group_by"],
    },
    rt.TOOL_WEB: {
        "required": ["web_query"],
        "properties": ["web_query", "max_results"],
        "integers": ["max_results"],
        "enums": [],
    },
}

for tool_name, want in EXPECTED_SHAPE.items():
    tool = get_tool(tool_name)
    params = tool.to_openai_schema()["function"]["parameters"]
    props = params["properties"]

    # ⚠⚠ **分两步**：先断言这个键**存在**，再比对它的值。只写 `params.get("required")`
    #   的话，`to_openai_schema` 里哪天把 `if required:` 写成条件式，采购记录工具会
    #   **静默少掉整个键** —— 而 `.get` 返回 None ≠ `[]`？None == [] 为假所以值比对会红，
    #   但那是一条会被人当成「预期值写错了」的 FAIL。这里让它自己明说。
    check(f"[{tool_name}] parameters 有 required 键（空列表也要在）", "required" in params, True)
    check(f"[{tool_name}] required = {want['required']}", params.get("required"), want["required"])
    # ⚠ 参数集合本身也是多数决投票的输入：多一个参数会改变票数分布，
    #   少一个会让「至少有一个参数落在它身上」的守卫失效。所以逐名钉死。
    check(f"[{tool_name}] 参数集合", sorted(props), sorted(want["properties"]))
    for field, prop in props.items():
        check(f"[{tool_name}] {field} 无顶层 anyOf（清洗层生效）", "anyOf" in prop, False)
        check(f"[{tool_name}] {field} 无 title（Pydantic 噪声已剥）", "title" in prop, False)
    for field in want["integers"]:
        check(f"[{tool_name}] {field} 顶层 type == integer", props[field].get("type"), "integer")
    for field in want["enums"]:
        check(
            f"[{tool_name}] {field} 顶层有非空 enum",
            isinstance(props[field].get("enum"), list) and len(props[field]["enum"]) > 0,
            True,
        )

# ⚠⚠ 反向：**证明清洗层真的在干活**，别让它哪天被当成冗余删掉。
#
# 上面的整数字段现在声明成 `Any` + `json_schema_extra={"type": "integer"}`，
# 所以 Pydantic **压根不会**给它们产出 anyOf —— 换句话说，今天这四条断言全绿
# **并不能说明清洗层有用**，删掉它照样绿。真正的风险在**明天**：
# 谁把某个整数参数按直觉写成 `int | None`（Python 里最自然的写法），Pydantic 就会
# 产出 `anyOf:[{type:integer},{type:null}]` 且**没有顶层 type**，
# `_normalize_args` 的 `.get("type")` 拿到 None → 掉进 else → 把字符串原样收下。
#
# 所以这里**现场造一个 `int | None` 的模型**，逐环钉住：
# 原始产出无顶层 type → 有 anyOf → 清洗后回到顶层 type == integer。
# 这三条一起才构成「清洗层必需」的证明。
class _IntUnionProbe(BaseModel):  # noqa: D101 - 探针用的一次性模型
    top_k: int | None = None  # noqa: RUF012


_raw = _IntUnionProbe.model_json_schema()["properties"]["top_k"]
check("⚠ Pydantic 对 `int | None` 的原始产出**没有**顶层 type（.get 会拿到 None）", "type" in _raw, False)
check("⚠ …它带 anyOf（所以清洗层不是冗余）", "anyOf" in _raw, True)
_cleaned = to_openai_schema(_IntUnionProbe, name="probe", description="")["function"]["parameters"][
    "properties"
]["top_k"]
check("⚠ 清洗后回到顶层 type == integer（`top_k=\"5\"` 因此才会被转成 int）", _cleaned.get("type"), "integer")

# 反向再确认一次：今天这些 `str | None` 字段的原始产出**确实**带 anyOf，
# 也就是说清洗层每天都有活干，不是只为假想的未来存在。
check(
    "⚠ `str | None` 字段的原始产出同样带 anyOf",
    "anyOf" in rt.SEARCH_TOOL.args_model.model_json_schema()["properties"]["query"],
    True,
)

# ---- 用例 0b：⚠⚠ 投票路径的夹具名**必须真的没注册** ----
#
# `_schema_by_name` 是**精确相等**查表：名字一旦命中注册表，归属就走「名字优先」，
# **参数投票整段不会被执行**。所以下面这两条不是装饰 —— 它们是「用例 1 / 反向 ③
# 究竟测的是哪条路径」的唯一凭证。少了它们，日后谁把夹具名改成一个真名
# （或给旧名加了 back-compat 别名），那些用例会**照样全绿**，覆盖却已经没了。
for _fake in ("query_knowledge_graph", "query_knowledge_graph_v2", "a_made_up_name", "whatever"):
    check(f"⚠ 投票路径夹具 {_fake!r} 确实不在注册表里", _fake in registered_names(), False)

# 反向再钉一次改名本身：三个旧名都必须已经不存在，一个都不许留别名。
for _old in ("search_knowledge_base", "query_knowledge_graph", "query_procurement_records"):
    check(f"⚠ 旧名 {_old!r} 已彻底注销（不留 back-compat 别名）", _old in registered_names(), False)

# ---- 用例 1：用户报的那次泄漏原文 ----
# ⚠ 名字用的是**改名前的旧名** `query_knowledge_graph`（见模块 docstring）：
#   当年这个位置写的是 `search_knowledge_graph`——那是模型把 `search_knowledge_base`
#   与 `query_knowledge_graph` 揉出来的幻觉名；2026-09-20 改名后它**成了真名**，
#   再留在这里就会静默改走「名字优先」路径，本用例就不再测参数投票了。
#   换回旧名恰好保住原意：一个**不存在的、但看着很像的**名字。
leaked = (
    f"</{M}tool_calls> <{M}invoke name=\"query_knowledge_graph\"> "
    f"<{M}parameter name=\"keyword\" string=\"true\">管理局</{M}parameter> "
    f"<{M}parameter name=\"query_type\" string=\"true\">search_entity</{M}parameter> "
    f"</{M}invoke> "
    f"<{M}invoke name=\"query_knowledge_graph\"> "
    f"<{M}parameter name=\"keyword\" string=\"true\">中学</{M}parameter> "
    f"<{M}parameter name=\"query_type\" string=\"true\">search_entity</{M}parameter> "
    f"</{M}invoke> </{M}tool_calls>"
)

calls = recover_tool_calls(leaked, prefix="r1")
check("泄漏原文恢复出 2 个调用", len(calls), 2)
check(
    "工具名归一到 search_knowledge_graph",
    [c.name for c in calls],
    ["search_knowledge_graph", "search_knowledge_graph"],
)
check(
    "参数：keyword→entity，search_entity→entity_search",
    [json.loads(c.arguments) for c in calls],
    [
        {"entity": "管理局", "query_type": "entity_search"},
        {"entity": "中学", "query_type": "entity_search"},
    ],
)
check("id 互不相同", len({c.id for c in calls}), 2)

# ---- 用例 2：模型没写错名字、参数也对（正常情况不该被改动）----
clean_case = (
    f"<{M}invoke name=\"search_knowledge_graph\"> "
    f"<{M}parameter name=\"query_type\">top_subjects</{M}parameter> "
    f"</{M}invoke>"
)
calls = recover_tool_calls(clean_case, prefix="r1")
check(
    "正确的调用原样通过",
    [(c.name, json.loads(c.arguments)) for c in calls],
    [("search_knowledge_graph", {"query_type": "top_subjects"})],
)

# ---- 用例 3：检索工具也能恢复（靠参数 query 判定归属）----
search_case = (
    f"<{M}invoke name=\"search_bidding_knowledge\"> "
    f"<{M}parameter name=\"query\">履约保证金 最高限额</{M}parameter> "
    f"<{M}parameter name=\"top_k\">3</{M}parameter> "
    f"</{M}invoke>"
)
calls = recover_tool_calls(search_case, prefix="r1")
check(
    "检索工具：query + top_k 转 int",
    [(c.name, json.loads(c.arguments)) for c in calls],
    [("search_bidding_knowledge", {"query": "履约保证金 最高限额", "top_k": 3})],
)

# ---- 用例 3b：采购数据库工具（第三个工具）也能靠参数判定归属 ----
# 归属完全靠参数集合：contains / province / category 这些只有数据库工具带。
pg_case = (
    f"<{M}invoke name=\"a_made_up_name\"> "
    f"<{M}parameter name=\"category\">学校</{M}parameter> "
    f"<{M}parameter name=\"amount_min\">500000</{M}parameter> "
    f"<{M}parameter name=\"group_by\">city</{M}parameter> "
    f"</{M}invoke>"
)
calls = recover_tool_calls(pg_case, prefix="r1")
check(
    "数据库工具：按参数判定归属，枚举值照常解析",
    [(c.name, json.loads(c.arguments)) for c in calls],
    [
        (
            "search_procurement_records",
            {"category": "学校", "amount_min": "500000", "group_by": "city"},
        )
    ],
)

# 参数类型：_normalize_args 只对 schema 里标了 "type":"integer" 的做 int()，
# number / boolean 会**原样保留字符串**。泄漏路径恢复出来的就是这种形态，
# 所以 pg_store 必须两种都认（那里用 _amount / _bool 各自解析）。
check(
    "amount_min 保持字符串（number 类型不转 int，pg_store 侧负责解析）",
    json.loads(calls[0].arguments)["amount_min"],
    "500000",
)

# ---- 用例 3c：名字精确时优先按名字归属（2026-09 新加，原先一律只认结构）----
# 归属判定的原则从此是「**名字精确对上已注册工具时优先，对不上仍只认结构**」。
# 动机是一次线上泄漏：模型写的是 name="search_web" + 参数 query，而 query 恰是
# search_bidding_knowledge 的主参数 → 只按参数投票时它 1:0 独得选票 → **一次联网被当成
# 检索执行了**。联网那条在 probe_web_search.py ⑧（要按请求挂工具表），这里只验规则本身。

# 名字精确 + 必填齐全，但参数集合**同时**命中另一个工具（过去判据打平 → 整条丢弃）。
# 现在按名字执行：模型明确写了要用哪个工具，不算猜。
name_wins = (
    f"<{M}invoke name=\"search_knowledge_graph\"> "
    f"<{M}parameter name=\"query_type\">top_suppliers</{M}parameter> "
    f"<{M}parameter name=\"category\">学校</{M}parameter> "
    f"</{M}invoke>"
)
check(
    "名字精确 + 参数打平 → 按名字执行（query_type 留给图谱，category 不沾它、静默跳过）",
    [(c.name, json.loads(c.arguments)) for c in recover_tool_calls(name_wins, prefix="r1")],
    [("search_knowledge_graph", {"query_type": "top_suppliers"})],
)

# 反向 ①：名字精确**但归一化失败**（必填 query 缺失）→ 退回投票，原逻辑一字未改。
# 换了新规则之后这条最容易退化——短路若「只认名字」，这里会执行一次没有 query 的检索。
name_unfit = (
    f"<{M}invoke name=\"search_bidding_knowledge\"> "
    f"<{M}parameter name=\"category\">学校</{M}parameter> "
    f"<{M}parameter name=\"province\">安徽省</{M}parameter> "
    f"<{M}parameter name=\"amount_min\">500000</{M}parameter> "
    f"</{M}invoke>"
)
check(
    "名字精确但必填缺失 → 退回投票，恢复成数据库工具",
    [(c.name, json.loads(c.arguments)) for c in recover_tool_calls(name_unfit, prefix="r1")],
    [
        (
            "search_procurement_records",
            {"category": "学校", "province": "安徽省", "amount_min": "500000"},
        )
    ],
)

# 反向 ② ⚠ 这条守着一个**静默错答案**的口子：数据库工具没有必填参数，短路若只看
# 「名字对上了」就放行，_normalize_args 会「成功」返回一个**空 args**，于是执行一次
# 没有任何筛选条件的查询——一个看起来完全合理的错误答案（全表 8751 行）。
# 所以短路还要求「至少有一个参数真的落在它身上」，一个都不沾就退回投票。
name_hollow = (
    f"<{M}invoke name=\"search_procurement_records\"> "
    f"<{M}parameter name=\"query\">履约保证金 最高限额</{M}parameter> "
    f"</{M}invoke>"
)
check(
    "⚠ 名字精确但参数一个都不沾它 → 不得执行空查询，退回投票恢复成检索",
    [(c.name, json.loads(c.arguments)) for c in recover_tool_calls(name_hollow, prefix="r1")],
    [("search_bidding_knowledge", {"query": "履约保证金 最高限额"})],
)

# 反向 ③：名字写得**像**已注册工具但不是（用例 1 的 query_knowledge_graph 就是这条——
# `_schema_by_name` 必须是**精确相等**、不许做任何归一化，否则一个近似名会被「修」成
# 它看着最像的那个工具，用例 1 整条判错）。
# 这里再钉一个更露骨的：在旧名后面多挂一个后缀。
almost = (
    f"<{M}invoke name=\"query_knowledge_graph_v2\"> "
    f"<{M}parameter name=\"entity\">空调</{M}parameter> "
    f"<{M}parameter name=\"query_type\">top_subjects</{M}parameter> "
    f"</{M}invoke>"
)
check(
    "近似名（多一个后缀）不算命中，仍按参数投票恢复",
    [(c.name, json.loads(c.arguments)) for c in recover_tool_calls(almost, prefix="r1")],
    [("search_knowledge_graph", {"entity": "空调", "query_type": "top_subjects"})],
)

# ⚠ 这是本轮**结构性加固**针对的那条：`keyword` 被 _PARAM_ALIASES 改写成了 entity，
# 而 entity 不是数据库工具的属性。旧行为是静默 continue —— 表现是「模型以为按关键词
# 筛过了，实际筛没了」，正是「对不上就整条丢弃，绝不猜」要消灭的静默错答案。
# 这里数据库参数占多数、投票会选数据库工具，但**必须整条丢弃**。
aliased_mismatch = (
    f"<{M}invoke name=\"whatever\"> "
    f"<{M}parameter name=\"keyword\">大学</{M}parameter> "
    f"<{M}parameter name=\"province\">安徽省</{M}parameter> "
    f"<{M}parameter name=\"category\">学校</{M}parameter> "
    f"</{M}invoke>"
)
check(
    "别名改写后落不到所选工具上 → 整条丢弃（不能丢着它执行）",
    recover_tool_calls(aliased_mismatch, prefix="r1"),
    [],
)

# 反向：别名改写在**正确**的工具上（图谱确实有 entity 属性）时照旧恢复。
# 加固不能把 2026-09 那次真实泄漏的恢复路径一起掐死——那条路是线上真出过事的。
alias_ok = (
    f"<{M}invoke name=\"search_knowledge_graph\"> "
    f"<{M}parameter name=\"keyword\">管理局</{M}parameter> "
    f"<{M}parameter name=\"query_type\">entity_search</{M}parameter> "
    f"</{M}invoke>"
)
check(
    "别名改写落在正确工具上 → 照旧恢复",
    [(c.name, json.loads(c.arguments)) for c in recover_tool_calls(alias_ok, prefix="r1")],
    [("search_knowledge_graph", {"entity": "管理局", "query_type": "entity_search"})],
)

# ---- 用例 4：对不上的一律丢弃，绝不猜 ----
check("空标记 → 空列表", recover_tool_calls("", prefix="r1"), [])
check("普通正文 → 空列表", recover_tool_calls("这段是正常回答。[1]", prefix="r1"), [])

# 枚举值离谱：query_type 拼不出任何真实取值 → 整条丢弃
bad_enum = (
    f"<{M}invoke name=\"search_knowledge_graph\"> "
    f"<{M}parameter name=\"entity\">空调</{M}parameter> "
    f"<{M}parameter name=\"query_type\">list_all_everything</{M}parameter> "
    f"</{M}invoke>"
)
check("枚举对不上 → 丢弃", recover_tool_calls(bad_enum, prefix="r1"), [])

# 参数集合一个都不认识 → 判不出归属
unknown = f"<{M}invoke name=\"some_tool\"> <{M}parameter name=\"foo\">bar</{M}parameter> </{M}invoke>"
check("参数全不认识 → 丢弃", recover_tool_calls(unknown, prefix="r1"), [])

# 参数被揉混：query（检索工具）和 entity（图谱工具）同时出现 → 判据打平，必须放弃。
# 这时按顺序取第一个等于抛硬币——执行一个模型没打算发起的调用比不执行更糟。
tied = (
    f"<{M}invoke name=\"whatever\"> "
    f"<{M}parameter name=\"query\">履约保证金</{M}parameter> "
    f"<{M}parameter name=\"entity\">空调</{M}parameter> "
    f"</{M}invoke>"
)
check("参数打平 → 放弃", recover_tool_calls(tied, prefix="r1"), [])

# 缺必填：图谱工具只有 entity 没有 query_type
missing_req = (
    f"<{M}invoke name=\"search_knowledge_graph\"> "
    f"<{M}parameter name=\"entity\">空调</{M}parameter> "
    f"</{M}invoke>"
)
check("缺必填 query_type → 丢弃", recover_tool_calls(missing_req, prefix="r1"), [])

# 空值的参数按「没给」处理，因此 top_subjects 仍能过（它不需要 entity）
empty_val = (
    f"<{M}invoke name=\"search_knowledge_graph\"> "
    f"<{M}parameter name=\"entity\"></{M}parameter> "
    f"<{M}parameter name=\"query_type\">top_subjects</{M}parameter> "
    f"</{M}invoke>"
)
check(
    "空值参数被忽略，必填仍在 → 通过",
    [(c.name, json.loads(c.arguments)) for c in recover_tool_calls(empty_val, prefix="r1")],
    [("search_knowledge_graph", {"query_type": "top_subjects"})],
)

# ---- 用例 5：strip 只剥 token，标签结构必须完整留下 ----
stripped = strip_tool_markup(leaked)
check("剥完不再有标记 token", "DSML" in stripped, False)
check("标签名还在", 'invoke name="query_knowledge_graph"' in stripped, True)
check("闭合标签还在", stripped.count("</invoke>"), 2)
check("参数标签还在", stripped.count("parameter name="), 4)
# strip 只负责剥 token，标签属性原样留着（清洗属性是解析阶段的事，见用例 1 的值）
check("标签属性原样保留", 'string="true"' in stripped, True)

print()
if failures:
    print(f"{len(failures)} 项失败：")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("全部通过")
