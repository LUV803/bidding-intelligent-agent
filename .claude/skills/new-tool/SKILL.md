---
name: new-tool
description: 新增或改一个 Agent 工具要动的四处（BaseTool 子类 / 进 TOOL_SCHEMAS 还是 tools_for / rag.py 提示词片段 / ToolCallBlock 的 TOOLS 表），含参数名撞车、类型声明写法、进程级与每请求之分、结构化结果的五处联动，以及必须手工扩的表和必跑的探针。Use when 用户说「加个工具」「新增工具」「模型不调我新加的工具」「改一下工具的参数」「让模型能查 X」。
---

# 新增一个 Agent 工具

⚠ 前提：本文件说的「工具」是**挂给模型自主调用**的那几个（`search_bidding_knowledge` /
`search_knowledge_graph` / `search_procurement_records` / `search_web`）。
**图片解析不是工具**（它在上传那一刻就发生，模型没有选择权）—— 要加那种能力别照本文件抄。

## 先判断：进程级还是每请求

这一步决定后面至少三处怎么写，**先定下来**。

| | 进程级 | 每请求 |
| --- | --- | --- |
| 例子 | 图谱 / 采购库（`ENABLED` 是模块常量，import 期按 `.env` 算死） | 联网搜索（`ChatRequest.web_search`） |
| 工具表 | 进 `TOOL_SCHEMAS`（`rag_tools.py:542-548`） | **不进**，由 `tools_for()` 拼 |
| 提示词 | 模块级常量直接拼 | 片段也得按请求拼 |

⚠ **别把每请求的塞进 `TOOL_SCHEMAS`**（`probe_pg_store.py` 会当场发现「`TOOL_SCHEMAS` 里
有个默认不挂的工具」）；也**别把进程级的塞进 `tools_for()`**（那就得给每个调用点加形参）。
现在四个工具里只有 `search_web` 是每请求的，`tools_for(enable_web)` 就是为它存在的。

---

## 要改的四处

Pydantic 化之后是**四处，不是六处**了。原先的「手写 schema dict / `_HANDLERS` 加处理器 /
`_peek_display` 加分支」三条并成了一条 —— 写一个 `BaseTool` 子类，那三样由引擎派生。

| # | 文件 | 改什么 | 漏了的表现 |
| --- | --- | --- | --- |
| 1 | `tools/rag_tools.py` | 写 `BaseTool` 子类 + `register()`；进程级的进 `TOOL_SCHEMAS` | 见下面「忘记 `register()`」 |
| 2 | `rag.py` | 加提示词片段（**只动 Agent 专属后缀**） | 模型不知道有这个工具，从不调用 |
| 3 | `frontend/components/ToolCallBlock.tsx` | `TOOLS` 表（`:19-24`）加展示名 + 运行中文案 | 折叠区显示成一串原始工具名 |
| 4 | 结构化结果要独立成块时 | `ToolOutcome` → `tool_result` 载荷 → `types.ts` → `useChat.ts` → `ChatMessage.tsx` | 界面上那个块永远不出现 |

---

## 第一处：`BaseTool` 子类

拿现成的抄，最省事的是 `KnowledgeBaseTool`（`rag_tools.py:85-133`）。骨架：

```python
class MyArgs(_Args):                        # _Args 在 rag_tools.py:57-68
    entity: str | None = Field(default=None, description="…")

class MyTool(BaseTool):                     # BaseTool 在 tools/base.py:292-381
    name = TOOL_MINE                        # 常量写在 rag_tools.py:51-54 那一段
    args_model = MyArgs
    schema_required = ("entity",)           # 默认 ()
    description = "…"                       # 模型选不选它全靠这段
    route_label = "一句话短标签"             # 空手而归时给模型看的
    name_scoped_aliases = {}                # 默认 {}，只有联网用得上

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutcome:
        ...                                 # ⚠ 不得抛异常，见下
```

- **`name` / `description` / `args_model` 都没有默认值** —— 漏了在 import 期就报错，不会静默。
- **`run()` 是 `@abstractmethod`，docstring 里写着「不得抛异常」**（`base.py:375-381`）。
  这不是洁癖：`execute_tool` 最后有一道 `except Exception` 兜底，但**漏掉 `role:"tool"`
  消息会让下一轮请求因为 `tool_call_id` 无人匹配直接 400**。所有错误都必须转成文本回传给模型
  （`ToolOutcome(..., ok=False, error=…)`），照 `KnowledgeBaseTool` 那几支抄。
- **`route_label` 一定要填**：结果为空时 `assess_outcome()` 会把它拼进提示词，点名本轮还挂着的
  其他工具。⚠ **别把 `description` 抄一遍** —— 那段有十几行，抄进去会吃掉一大块上下文预算。
  `probe_quality.py` ④ 逐个断言四个工具都配了它。
- **`peek_display()` / `empty_reason()` 按需覆写**。基类默认：`peek_display` 读 `query` 字段
  （`base.py:342-350`，只有检索工具用得上）、`empty_reason` 回「没有拿到任何新条目」
  （`base.py:356-373`）。图谱/采购库覆写了 `empty_reason`（读自己那份 `total`，判据必须是
  **结构性**的行数/条数，不能是相似度分数 —— 见 `base.assess_outcome()` 的注释）。
- 四个现有工具里覆写的位置：`route_label` 在 `:98/:178/:333/:485`，`empty_reason` 在
  `:180/:335/:487`，`peek_display` 在 `:191/:346/:496`。

### ⚠⚠ 参数名撞车：`_match_schema` 是简单多数投票

`recover_tool_calls()` 在名字对不上时会「每个参数给每个工具投一票」。**0 票或并列即整条丢弃**。
所以：

- ❌ 别用已被占的名字：`query`（检索）/ `top_k`（检索）/ `query_type` + `entity`（图谱）。
- ❌ 别用会被 `_PARAM_ALIASES`（`base.py:536-542`）改写成 `entity` 的：
  `keyword` / `keywords` / `entity_name`。
- ❌ 别用 `limit` / `count` 这类泛用词 —— 它们是泄漏标记的「票仓」，投给谁都合理，等于没投。
  （所以行数上限叫 `max_rows`、联网的查询词叫 `web_query`。）
- 要给自己限定一个别名（比如把模型的 `query` 纠正成 `web_query`），**走 `name_scoped_aliases`
  而不是全局 `_PARAM_ALIASES`**：`_PARAM_ALIASES` 在 `_match_schema` **之前**生效，把 `query`
  全局改写成 `web_query` 会让一次**检索**的泄漏从「稳赢」变成打平 → 整条丢弃。
  ⚠ 实例见 `WebSearchTool.name_scoped_aliases`（`rag_tools.py:472`），消费点在 `base.py:597-602`
  —— **只在名字精确命中时才叠加**。（CLAUDE.md 里有一处把它写成模块级 dict
  `_NAME_SCOPED_ALIASES`，**那个东西不存在**，以代码为准。）

### ⚠⚠ 类型声明：整数字段一律 `Any` + `json_schema_extra`

这是本项目最大的技术地雷，照抄这张表：

| 字段种类 | 写法 | 为什么 |
| --- | --- | --- |
| 文本参数 | `str \| None = Field(default=None, …)` | 正常走 Pydantic |
| `top_k` / `max_rows` / `max_results` | `Any` + `json_schema_extra={"type":"integer","minimum":…,"maximum":…}` | 见下 |
| `amount_min` / `amount_max` / `sort_desc` | `Any` + `{"type":"number"}` / `{"type":"boolean"}` | 原样透传给 `pg_store` 解析 |
| 枚举（`query_type` / `sort_by`…） | `str \| None` + `json_schema_extra={"enum": […]}` | **刻意不用 `Literal[...]`** |

**别用 `int \| None`**：`model_json_schema()` 对它产出的是
`anyOf: [{type:integer,…},{type:null}]`，**没有顶层 `type`**。而 `_normalize_args()` 直接读裸
JSON Schema 键（`.get("type") == "integer"`），拿到 `None` 就掉进 `else` **把原值原样收下**
—— 恢复路径传进来的是从标记里抠出来的**字符串**，一次 `top_k="5"` 的恢复就变成字符串
`"5"` 交给 handler。**不报错、不崩溃，只是类型变了。**

**别用 `Literal[...]`**：枚举值对不上时，`graph_store` 会回一句**带可用值列表**的中文错误
（「错误：没有名为 X 的查询类型。可用类型：…」），模型能在同一轮内自我纠正；交给 Pydantic 拒
就只剩通用英文文案了。schema 里**照样**有 `enum`（经 `json_schema_extra`），
`_normalize_args` 看得到。

⚠ `description` 里那些**中文逐值说明**（`"；".join(f"{k}={v}" …)`）**必须保留** ——
那是模型选对枚举值的唯一依据。

---

## 注册 ≠ 挂出去

两个概念，差别有意义：

```python
SEARCH_TOOL = KnowledgeBaseTool()                 # rag_tools.py:524-530
for _tool in (SEARCH_TOOL, GRAPH_TOOL, PG_TOOL, WEB_TOOL):
    register(_tool)                               # base.py:134-144，四个都无条件登记
```

- **注册表**（`_REGISTRY` / `_HANDLERS`，`base.py:127/131`）回答「**引擎认识谁**」——
  四个工具无条件全注册，所以被禁用的工具真被调用时，拿到的是**该工具自己如实的失败**，
  而不是「没有这个工具」。
- **`TOOL_SCHEMAS`**（`rag_tools.py:542-548`）回答「**这轮挂给模型的是谁**」，按
  `graph_store.ENABLED` / `pg_store.ENABLED` 过滤。

⚠ `register()` 遇到重名会抛 `ValueError(f"工具名重复登记: {tool.name!r}")` —— 这是好事，
import 期就炸。⚠ `agent.py:87-90` 有一条 `assert set(_HANDLERS) == {t.name for t in
registered_tools()}` 挡两边漂移。

### 忘记 `register()` 会怎样（实测，不是推断）

不是「没有这个工具」这么干净，是**两个症状同时出现**：

1. **界面上那一格是空的** —— `_peek_display(name, arguments)`（`base.py:384-399`）对未知名字
   返回 `""`，不抛异常。表现是工具调用折叠区显示一对空的 `「」`。
2. **模型拿到的是**「`错误：没有名为 X 的工具。可用工具：A、B、C。`」，`error="unknown_tool"`
   （`execute_tool`，`base.py:484-488`）。

⚠ 而且 `unknown_tool` 在 `_SELF_CORRECTING_ERRORS`（`base.py:407-409`）里 ⇒ **不会**触发
「结果质量」那段改调提示。模型只看到一句「没有这个工具」，然后自己决定怎么办。

⚠ 那条错误里的可用工具列表取自 `ctx.tool_names`（本轮真挂出去的那批），不是进程级的
`TOOL_SCHEMAS`（`base.py:483` 有三种回退）。别去改那个顺序。

---

## 第二处：`rag.py` 提示词片段

**只动 Agent 专属后缀**，`SYSTEM_PROMPT` / `build_messages()` 一个字不改。

提示词是**按片段拼装**的，不是组合写死 —— 三个拼装函数：
`_agent_system_prompt()` / `_no_result_prompt()` / `_sources_reminder()`，都接 `tools_ok`
（`_collect_variant` 直接调它们）。加片段即可，但有两个约束：

- ⚠ **「提示词提到工具 ⟺ 工具列表里有它」这条不变量** —— 所以片段要跟着
  `graph_store.ENABLED` / `pg_store.ENABLED` 走。有的片段只在**图谱与数据库都启用**时才拼
  （如 `_AGENT_DB_CROSS_TOOL_RULE`），否则是在让模型用一个没注册的工具。
- ⚠ 转向指令（「改用某工具」）**必须保持工具中立**。原先 `_SOURCES_REMINDER` 里写死「按
  系统提示词的**图谱**规则改用 `search_knowledge_graph`」，带筛选条件的问题会因此被送进没有
  省份/时间/金额维度的图谱，返回 0 行后模型回头顺着无关资料编 —— 那是**盐化工那个 bug 原地
  复活**。新增工具时同理，别在公共片段里点名某一个工具。

---

## 第三处：前端 `ToolCallBlock.TOOLS`

`frontend/components/ToolCallBlock.tsx:19-24` 那张表加一行：

```tsx
const TOOLS: Record<string, { label: string; running: string }> = {
  search_bidding_knowledge:   { label: "检索知识库",   running: "检索中…" },
  search_knowledge_graph:     { label: "查询知识图谱", running: "查询图谱中…" },
  search_procurement_records: { label: "查询采购数据库", running: "查询数据库中…" },
  search_web:                 { label: "联网搜索",     running: "联网搜索中…" },
};
```

⚠ `doneText()`（`:32-61`）里若新工具**结果为空时不该说「与已有资料重复」**（比如联网那种
「网上没搜到」是完全相反的结论），要加一支判在**兜底 `return` 之前**、且**按 `step.name` 判**
（`ToolStep` 上不一定有你想要的载荷）。`search_web` 那支（`:57`）就是这么加的。

---

## 第四处：结构化结果的五处联动

只有「工具返回结构化数据、要在前端独立成块」时才需要（图谱 / 采购库两个块是这么来的）。
**五处缺一不可，漏一处都是静默的**：

```
ToolOutcome 加字段 → tool_result 载荷加 key → lib/types.ts → useChat.ts 的 tool_result 分支 → ChatMessage.tsx
```

- `ToolOutcome` 在 `schemas.py`；载荷里 `graph` / `pg` 是**已经 `.model_dump()` 过的字典**，
  直接塞 pydantic 模型会在 `api.py` 的 `json.dumps` 处炸成 `TypeError`。
- ⚠ 新模型里**不能留 `Decimal` / `date` / `datetime`** —— 在构造时就转掉
  （金额 → `float`，日期 → ISO 字符串）。这是本项目记过**五次**的坑。
- ⚠ `useChat.ts` 那个 `newCount` 守卫**要记得把新工具列上**（如果它不是检索工具）：
  漏了，那个工具的步骤会显示成「新增 0 条资料」—— 一个看起来合理但完全错误的说法。
  ⚠ 反过来，**结果是真带 `[n]` 编号进来源面板的（如联网），就不要加进那个名单**。
- ⚠ 新块**不要接 `SourcePanel`**：来源面板是带 `[n]` 编号、会被正文角标联动的问答对；
  结构化结果是另一回事，混在一起会让人以为那些行也是「资料」。
- ⚠ `useChat.ts` 消费事件的 `else if` 链**没有 default 分支也不做穷尽检查** ——
  漏了不报错，只是「事件到了但什么都不显示」。

---

## ⚠⚠ 还得手工扩的表（这些不会自己跟着走）

1. **`scripts/probe_enum_route.py` 的 `probe_tool_matrix()`**（`:168-228`）。它逐组合验
   「提示词提到工具 ⟺ 工具列表里有它」，但**工具名是硬编码字面量**：
   `:175-178` 那张 `named` dict、`:209` 那个元组。⚠ `rag_tools.py:568-570` 的注释说
   「矩阵探针盯的正是它」，读起来像是从 `tools_for()` 派生的 —— **不是**。
   进程级的新工具要扩这三处：`_collect_variant()`（`:146-165`，加一个形参 + 翻转 + 还原）、
   `named` dict、`:209` 的元组。
2. **`rag.py` 的提示词片段**（第二处已说，但它属于「不扩就静默」那一类）。
3. 每请求的工具（如新的联网类工具）要看 `tools_for()` 的合取条件，以及 `agent.py` 里
   **三个** `tools=tools` 的调用点（强制图谱轮、正式轮、收尾轮）与泄漏恢复的 `schemas=tools`
   —— 漏掉正式轮等于那个工具只在强制轮存在。

---

## 改完必跑

```bash
uv run python scripts/probe_recover.py     # 恢复机制；⚠ 头两组是用例 0 / 0b，先看它们
uv run python scripts/probe_quality.py     # 48 条：结果质量提示 + 撞顶不算空手（全离线）
uv run python scripts/probe_enum_route.py  # 列举句识别 + 提示词接线（不需要 Neo4j/大模型）
```

- ⚠ **`probe_recover.py` 的用例 0 / 0b 是「改清洗层或给任何字段加 `Optional` 之前」的必看项**
  （0 钉 schema 形状：每个整数字段清洗后顶层必须有 `type:"integer"`、枚举字段必须有 `enum`、
  全表无 `title`；0b 钉「投票路径的夹具确实没注册」）。漏了是**静默降级**。
- ⚠ 那个脚本里的假工具名（`query_knowledge_graph` 等旧名）**别顺手改成新名**：它们既不存在
  又长得极像真名，正是模型会真吐出来的形状。改成真名会让那些用例**静默切换路径、断言全绿、
  覆盖却没了**（2026-09-20 改名时真踩过）。
- 真机验路由：`python scripts/probe_graph_tool.py` / `probe_pg_tool.py`（需服务 + Key），
  或直接打一发 `/api/agent` 看模型会不会在对的时机选它。
- 前端改了就 `cd frontend && npm run build`（**先停 dev server**，两者共用 `.next`）。

## 三条已知的文档/代码不一致（别被绕进去）

1. CLAUDE.md 有一处写的 `_NAME_SCOPED_ALIASES` 模块级 dict **不存在** —— 实际是
   `BaseTool.name_scoped_aliases` 类属性（`base.py:314`，赋值在 `rag_tools.py:472`，
   消费在 `base.py:597-602`）。以代码为准。
2. `rag_tools.py:47` 的注释写「这三个名字」，实际有**四个** `TOOL_*` 常量（`:51-54`）。
3. `rag_tools.py:568-570` 暗示矩阵探针从工具表派生，实际是硬编码（见上）。
