---
name: bid-analysis
description: 用已部署的招投标问答系统做端到端数据分析 —— 按意图选数据源（RAG 查法规 / 知识图谱查企业关系 / PostgreSQL 查采购统计）、组合多个查询、产出对比与排名类结构化报告。含两种调用方式（POST /api/agent 交服务端模型路由，或直调 execute_tool 拿确定性结构化结果，后者在后端运行时也能用）与三个已实测的口径陷阱（同一个词三个数字 / total 是下限不是总数 / 没有时间维度）。Use when 用户说「分析一下空调采购」「对比A公司和B公司的中标情况」「哪个供应商中标最多」「某某单位采购过什么」「按类别统计采购金额」「帮我出一份招投标分析报告」「招投标数据」。
---

# 招投标数据分析（消费方）

这个 skill 与前几个**形状不同**：`new-tool` / `add-provider` / `diagnose-capability` 是**改系统**，
这里是**用系统**做分析。你不需要理解 `agent.py` 的内部循环，需要的是**选对数据源、
读懂口径、不编数字**。

⚠ 系统本身长什么样见 `CLAUDE.md`；起停服务见 `start-services`；
「配了却不工作」见 `diagnose-capability`。

---

## 一、两条路，先选一条

| | **模式 A：端到端** | **模式 B：直调工具** |
| --- | --- | --- |
| 怎么发 | `POST /api/agent`（SSE） | `execute_tool(ToolCall, ToolContext)` |
| 谁决定查什么 | **服务端模型**自己路由 | **你**指定工具与参数 |
| 拿回来什么 | 一段自然语言回答 + `tool_call`/`tool_result` 事件 | `ToolOutcome`，带 `graph`/`pg` **结构化结果** |
| 什么时候用 | 开放问题、要成文的回答、想让模型自我纠错 | 报告要**确定的数字**、多查询组合、可复现 |
| 需要后端 | ✅ 必须跑着 | ❌ 见下 |

**推荐：模式 B 做取数，你自己写报告。** 理由不是偏好 —— A 模式里模型的答案**不可复现**
（同一句问两次，数字可能来自不同的筛选路径），而分析报告一旦要引用具体金额就经不起这个。

### ⚠⚠ 模式 B 在后端跑着的时候也能用（只限图谱/采购库）

**已实测**：`ctx.retriever` 全仓库**只有一个使用点** —— `tools/rag_tools.py:118`，
在 `KnowledgeBaseTool.run()` 里。图谱工具（`:218`）、采购库工具（`:403`）、
联网工具都**不读它**。所以：

```python
ctx = ToolContext(registry=SourceRegistry(cap=20), retriever=None,   # ← 传 None 是安全的
                  mode="hybrid", rerank=False, tool_names=TOOL_NAMES)
```

⇒ **不碰检索器的分析不会抢 Qdrant 的排他锁，后端在跑也照做。**

| 你要做的事 | 后端在跑时 |
| --- | --- |
| 模式 A（任何问题） | ✅ 可以 |
| 模式 B · 图谱 / 采购库 | ✅ 可以（不碰 retriever） |
| 模式 B · 知识库检索 | ❌ **必须先停后端**（要真 retriever → 抢 Qdrant 锁） |

⚠ 只有「查法规」这一路需要停后端。**能不停就别停** —— 重启后头几十秒 `/api/health` 是 503。

### 模式 B 的执行入口

本 skill 目录里带一个跑通的 CLI，**不要手搓脚本**：

```bash
uv run python .claude/skills/bid-analysis/tool.py tools       # 本轮挂着哪些工具
uv run python .claude/skills/bid-analysis/tool.py schema      # 两个工具的参数定义
uv run python .claude/skills/bid-analysis/tool.py --help

# 图谱
uv run python .claude/skills/bid-analysis/tool.py graph top_suppliers
uv run python .claude/skills/bid-analysis/tool.py graph subject_suppliers --entity 空调
# 采购库
uv run python .claude/skills/bid-analysis/tool.py pg --contains 空调 --group-by category
uv run python .claude/skills/bid-analysis/tool.py pg --sort-by amount --max-rows 10 --json
```

⚠ `--json` 挂在**子命令上**（`pg --json`，不是 `--json pg`）。
⚠ 自己写脚本时的两个坑：`ToolCall.arguments` 是 **JSON 字符串不是 dict**
（`clients/base_client.py:120-126`）；`execute_tool` 是 **async**，要 `asyncio.run`。

---

## 二、意图 → 数据源

| 用户问的是… | 用什么 | 为什么 |
| --- | --- | --- |
| 法规条文、政策口径、办事流程 | **RAG**（`search_bidding_knowledge`） | 74 条问答对是这套系统唯一的知识库 |
| **实体之间的关系**：某企业的供应商/采购商、某标的物谁供过、地区分布 | **图谱**（`search_knowledge_graph`） | 以「交易」为中心建的图，8751 笔 |
| **带条件的筛选与精确统计**：按省份/时间/金额/类别筛、精确条数、按维度分组 | **采购库**（`search_procurement_records`） | `count(*)` 精确、`GROUP BY` 现成 |
| 知识库里没有的、有时效性的 | **联网**（`search_web`） | ⚠ 仅模式 A；且要先拨开开关 |

⚠⚠ **图谱与采购库是同一份源数据的两套技术栈** —— `bid_records` 表就是这个图的源 xlsx。
所以「某公司的中标情况」两边都能答，**但数字口径不同**（见第三节）。
**一个问题不要两个工具都调**，选一个并说明口径。

---

## 三、⚠⚠ 三个已实测的口径陷阱

这三条是这份数据上做分析**最容易出错**的地方，全部实测过。

### 陷阱 1：同一个词，三个数字，三个含义

以「空调」为例（本次实测）：

| 怎么问 | 得到的数 | **这个数是什么意思** |
| --- | --- | --- |
| 图谱 `entity_search` → `标的物·空调` | **180** | 交易**笔数** |
| 图谱 `subject_suppliers(entity=空调)` | **145** | 不同的**供应商家数**（有人供多笔，所以 145 < 180） |
| 采购库 `contains=空调` | **219** | 记录**条数** |

**差在哪**：图谱 `subject_suppliers` 把「空调」**精确解析**到那个节点（`resolved="空调"`），
只统计它名下的交易；采购库 `contains` 是**子串模糊**、跨 `标题`/`项目名称`/`标的物` 三列的
**或**匹配，所以把「空调配件」21 笔、「中央空调套装」5 笔、「柜式空调」等一起捞了进来。

⚠ 图谱那条路会**主动告诉你**它只答了一半 —— 回传文本末尾有：

> 注意：关键词还匹配到其他实体（空调配件、中央空调套装、空调机组…）。以上结果**仅针对「空调」**。
> ……不同实体的交易数据**不能相加**。

**规矩：报告里写死用哪个数、并写明口径。** 绝不做 `180 + 39 = 219` 这种拼凑 ——
两边统计的**根本不是同一批记录**。

### 陷阱 2：图谱的 `total` 是**下限**，采购库的才是总数

```python
data["total_capped"]   # True ⇒ total 撞上了服务端硬上限（500），只是「至少这么多」
```

实测对比同一个问题（有哪些供应商）：

| | total | `total_capped` | 能不能说「一共 N 家」 |
| --- | --- | --- | --- |
| 图谱 `top_suppliers` | 500 | **True** | ❌ 只能说「**至少** 500 家」（渲染文本也写的是「500 条以上」） |
| 采购库 `group_by=supplier` | **4151** | 不存在这个字段 | ✅ 精确，`count(*)` |

⚠ `total_capped=True` 而你看漏了，就会把一个**下限**当成全集报出去 —— 不报错，
看起来完全合理。CLI 会把这条打到 stderr。
⚠ 反过来也成立：`truncated=True` 只说明 **`rows` 被截断**，不代表 `total` 不准。

**好消息**：两份数据**互相印证** —— 两边排行第一名都是「乙文体用品销售有限公司
**59 笔 / 312,516 元**」，逐位相同。跨栈对账一致时，数字可以放心用。

### 陷阱 3：⚠⚠ **没有时间维度，「价格趋势」做不了**

本次实测 `发布时间` 这一列：

```
min / max / 不同日期数： 2023-10-09 / 2023-10-20 / 12
年份分布： 2023 → 8751（全部）
空值行数： 0
```

**8751 条记录全部落在 2023-10-09 ~ 2023-10-20 这 12 天里**
（分布：10-12 起量 36 → 10-13 的 1439 → 10-16~10-19 每天 1300~1700）。

⇒ **这是某一次采集的快照，不是时间序列。** 后果：

- **用户举的「分析空调采购的价格趋势」在这份数据上无法回答。** 说「空调采购量在 10 月中旬
  明显上升」是把**采集节奏**读成了采购趋势 —— 一个**看起来完全合理的错误结论**。
- `date_from` / `date_to` **加了等于没加**：本次实测「空调」+ `2023-01-01~2024-12-31`
  与不加，结果**一模一样（219 / 6,488,861 元）**，因为全部记录都在这个区间内。
  ⚠ 所以「筛了日期结果没变」**不是 bug**，别去查代码。
- 年份筛选同理，只有一个年份可筛。

**正确做法**：把「趋势」类问题**改写成能答的形状**，并**明说为什么**：

| 用户说 | 改成 |
| --- | --- |
| 分析空调采购的价格趋势 | 空调采购的**构成**：按类别分组（学校/政府/货物…）、金额分布、最大单笔 |
| 近两年的采购变化 | （说清数据只有 12 天）→ 改为**横向对比**：按地区 / 按类别 / 按采购人 |
| 同比 / 环比 | 同上，**没有第二个时间点可比** |

⚠ 这与 CLAUDE.md 里「`GROUP_COLS` 不含 `province`/年份」是**同一条纪律**：
按一个**几乎没有区分度**的维度分组，出来的**不是报错，是看起来完全合理的废话**。

---

## 四、图谱：9 个查询类型

`query_type` 只有这 9 个（`graph_store.py:619-625`，CLI 的 `schema` 子命令能打出来）。
参数**只有 `entity` 一个** —— ⚠ **没有 `top_n`**（传了会被静默丢弃）。

| `query_type` | 中文标签 | `entity` |
| --- | --- | --- |
| `subject_suppliers` | 标的物的供应商 | 必填 |
| `subject_purchasers` | 标的物的采购商 | 必填 |
| `purchaser_items` | 采购商采购过的标的物 | 必填 |
| `supplier_items` | 供应商供过的标的物 | 必填 |
| `subject_profile` | 标的物全景（笔数/总额/供应商数/采购商数/地区分布） | 必填 |
| `entity_search` | 实体名查找（**拿不准登记名称时先探一下**） | 必填 |
| `top_subjects` | 交易频次排行 | 可空（空=全量） |
| `top_purchasers` | 采购商排行 | 可空（空=全量） |
| `top_suppliers` | 供应商排行 | 可空（空=全量） |

「必填」= `ENTITY_REQUIRED`（`graph_store.py:628`）；留空会直接 `ok=False` 拦住
（因为 `CONTAINS ""` 恒真，会静默返回「交易最多」那个实体的数据）。

**几条实测行为**：

- ⚠ **`top_purchasers` / `top_suppliers` 按「成交笔数」排，不是金额**
  （`ORDER BY cnt DESC, amt DESC`）。故意的 —— 源数据里那笔 66 亿异常记录会让按金额排的结果
  被它霸榜。**别以为是 bug，也别自己改排序**。
- **名字拿不准就先用 `entity_search`**：它返回带类型前缀的标签
  （`标的物·空调`、`供应商·甲科技有限公司`），一眼能看出重名的是哪一类。
- **模糊名会给出 `candidates`**：`entity_search("志洋")` → `供应商·甲科技有限公司 —— 44 笔`；
  `subject_suppliers("打印机")` → `resolved="打印机"` + `candidates=[激光打印机, 针式打印机, …]`。
  拿候选名**重查一次**，比猜可靠。
- `resolved` 是**实际命中的实体名**，和你给的关键词不一致时以它为准。

---

## 五、采购库：参数表

⚠ 类名是 **`PgArgs`**（不是 `ProcurementArgs`），参数名与下面这张表逐字对应。
工具**没有必填参数**（`schema_required = ()`），但**空参数 = 全表 8751 行**。

| 类别 | 参数 |
| --- | --- |
| 模糊匹配 | `contains`（标题/项目名称/标的物 **三列或匹配**）、`purchaser`、`supplier`、`agency` |
| 地区 | `province`、`city`、`county` |
| 分类 | `category`（学校/机关/医院…）、`source`（政府采购/企业采购/招标采购） |
| 金额 | `amount_min`、`amount_max`（单位**元**；「超过 500 万」填 `5000000`） |
| 日期 | `date_from`、`date_to`（⚠ 见陷阱 3）、`date_field`（`published`/`won_at`） |
| 排序 | `sort_by`（`amount`/`published`，**默认 `published`**）、`sort_desc` |
| 分组 | `group_by` = `city`/`county`/`category`/`source`/`purchaser`/`supplier` |
| 条数 | `max_rows`（1~50） |

**几条实测行为**：

- ⚠ **「金额最高的项目」必须显式给 `--sort-by amount`** —— 不给就按发布时间排，
  得到的是**答非所问但看起来很正常**的结果。
- ⚠ **`group_by` 的候选里没有 `province`**（99.2% 是安徽，分组出来一行 = 废话），
  没有 `agency`（7698/8751 为空，NULL 会霸榜）。**但 `province` 作为筛选是保留的。**
- **非法值会报错并列出有效值**：`group_by="星座"` → `ok=False`，
  note 里是「group_by 只能是 city / county / category / source / purchaser / supplier 之一」。
  **照提示改，别猜。**
- **66 亿那笔异常记录仍在**（某县残疾人联合会，与图谱那笔是同一笔）：
  按金额排它必然第一。工具会**主动附一句**「疑似录入错误，请向用户说明存疑」——
  **报告里必须保留这句**，不能当正常成交额写进去。
- 分组模式的行形状 `{label, count, amount}`；明细模式 `{title, project, purchaser,
  supplier, amount, published, region, subject, category, source}`。**两种模式行形状没有交集**，
  别按「哪些字段非空」去猜模式，看 `mode` 字段。

---

## 六、报告模板

⚠ **`graph`/`pg` 的结果不能标 `[n]` 引用序号** —— 那是知识库问答对的编号。
工具回传文本自己写着：「直接陈述数字与结论即可，**不要标注 [n] 引用序号**」。
`[n]` 只属于 RAG 检索到的法规条目。

### 排名（最稳的一类）

1. 口径一句话：查的什么库、什么筛选、`total` 是精确值还是下限
2. 表格：名次 / 实体 / 笔数 / 金额
3. ⚠ 异常值单独点出（66 亿那笔）
4. 数据边界：`truncated` 就说明「列出前 N 条」

### 对比（用户举例的那个 —— **能做到**）

「对比 A 公司和 B 公司的中标情况」实测可行，每家三发：

```bash
uv run python .claude/skills/bid-analysis/tool.py graph entity_search --entity 志洋          # ① 确认登记名
uv run python .claude/skills/bid-analysis/tool.py graph supplier_items --entity 甲科技有限公司
uv run python .claude/skills/bid-analysis/tool.py pg --supplier 甲科技有限公司 --group-by category
```

`entity_search` 那一步**别省** —— 公司登记名常带「有限公司」全称，
名字写不全时 `supplier_items` 会解析到错的那个（或 0 命中）。

实测两家真实输出（可直接当格式样板）：

| | 甲科技有限公司 | 丙文化用品有限公司 |
| --- | --- | --- |
| 交易笔数 | 44 | 35 |
| 金额合计 | 146,741 元 | 274,259 元 |
| 主营标的物 | 硒鼓 11 笔 / 粉盒 6 笔 | 政治类书籍 31 笔 |
| 类别构成 | 政府 23 / 学校 10 / 货物 8 / 项目管理 3 | 政府 27 / 货物 4 / 学校 3 / 项目管理 1 |

⚠ **笔数多的金额不一定大**（44 笔 14.7 万 vs 35 笔 27.4 万）——
「谁更活跃」和「谁金额更高」是两个结论，别合并成一句。

### 趋势（⚠ 见陷阱 3，这份数据上**做不了**）

先如实说明「数据是 12 天快照」，再把问题**改写成可答的横向对比**。
**不要**把快照内的采集节奏说成趋势。

---

## 七、编排多查询时的三条纪律

1. **先探名再查数**：实体类查询前先 `entity_search`（同名不同类、全称与简称都是坑）。
2. **一个事实只用一个来源**：同一批记录别既用图谱又用采购库 —— 口径不同，
   拼在一份报告里就是两个自相矛盾的数字。**跨栈对账**（两边第一名是否一致）可以做，
   但那是**验证**，不是取数。
3. **失败要如实**：`ok=False` 时 `note` 里是中文原因（多半直接告诉你合法值）。
   照它改参数重跑；改不动就如实说「查不到」，**别用常识补足**。

⚠ 模式 A 里模型也会踩这个 —— 所以拿到 A 的答案后，**引用具体数字前用模式 B 复核一遍**。

---

## 八、验证

```bash
# CLI 自检：三条路各打一发（不需要后端；图谱/采购库各自需要 Neo4j / PostgreSQL 在跑）
uv run python .claude/skills/bid-analysis/tool.py tools
uv run python .claude/skills/bid-analysis/tool.py graph top_suppliers | head -5
uv run python .claude/skills/bid-analysis/tool.py pg --contains 空调 --group-by category
```

**判据**：图谱那发应当看到「乙文体用品销售有限公司 —— 59 笔」；
采购库那发应当看到「学校 82 条 / 2,002,165 元」。数字对得上说明两条链路都通。

⚠ 中文输出在 Git Bash 里要 `PYTHONIOENCODING=utf-8`（脚本自己 reconfigure 了，
但 `| head` 之类的管道仍可能踩到）。
