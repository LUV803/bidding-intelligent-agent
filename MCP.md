# 这个项目适合加哪些 MCP 工具

> **结论先说**：「加 MCP」在这个仓库里有**三种完全不同的意思**，而它们**一个该做、一个不该做、一个是唯一真正的增量**。
> 不把这三种拆开，后面每一条建议都会被误读成另一种的意思。
>
> | | 一句话结论 |
> | --- | --- |
> | **方向 A** · 开发期给 Claude Code 挂 MCP | ✅ **推荐**，且**零依赖** —— 本仓库最划算的一件 |
> | **方向 B** · 让 `/api/agent` 用上 MCP | ⚠ **拆成两半、结论相反**：把现有五个工具**搬出去** ❌ ／ 用 MCP **新增一个**工具 ✅ —— 见 3.3 |
> | **方向 C** · 把本项目包成 MCP 服务器 | ✅ **唯一真正的对外增量**，但有一道硬墙（Qdrant 排他锁） |

⚠ 本文是**选型分析**，不是施工单。第 5 节给了落地清单，但**本次一行代码都没改**：
没有 `.mcp.json`、没有加依赖、没有服务器代码。

---

## 0. 破题：三种「加 MCP」不是一回事

| | 谁在调用 | 加什么 | 值不值 |
| --- | --- | --- | --- |
| **A. 开发期** | Claude Code（我） | `.mcp.json` 指向几个现成的第三方服务器 | ✅ 值 —— 少写一堆一次性探针 |
| **B. 运行期** | `bidding_qa` 自己的 Agent | 重写 `src/bidding_qa/tools/`，改走 MCP 客户端 | ⚠ **两种读法，结论相反 —— 见 3.3** |
| **C. 对外** | Claude Desktop / Cursor / 别人的 Agent | 给本项目**写一个** MCP 服务器 | ✅ 值 —— 唯一让项目能力出圈的方向 |

⚠⚠ **方向 B 是最容易读错的一个，先把它钉死**：它有两种读法，
而**只有第二种可用** ——

| B 的读法 | 结论 |
| --- | --- |
| ① 把**现有五个工具**搬到进程外的 MCP 服务器上 | ❌ **不值** —— 会把 3.1 那批本项目的语义机制整层拆掉 |
| ② 用 MCP **新增一个**工具（`search_web_exa`），客户端藏在 `BaseTool.run()` 里 | ✅ **可做** —— 出进程的只是新工具的实现细节，不变量全在 Agent 侧 |

**两者的分界线是「谁在进程外」**：搬走**现有工具** ⇒ 机制跟着走 ⇒ 拆掉；
只把**新工具的后端**放外面 ⇒ Agent 侧完全没变 ⇒ 不破。细则见 **3.3**。

**前提**：这个仓库现在**零 MCP**。不是「还没配好」，是**一行都没有** ——

```
.mcp.json              不存在
pyproject.toml / uv.lock   没有 mcp / fastmcp
src/ scripts/ eval/ docs/ CLAUDE.md README.md   全部零命中
git 历史               零命中（git log --all -i --grep=mcp 无输出）
用户级 ~/.claude.json   mcpServers 键存在，但是个空对象 {}
```

所以这是**从零选型**，不是改配置。

---

## 1. 结论速览

| 候选 | 方向 | 结论 | 一句话理由 |
| --- | --- | --- | --- |
| **Neo4j** MCP | A | ✅ **最值** | 9 个 Cypher 模板的调参、金额对账、那笔 66 亿错账，现在每次都靠「写个探针→跑→删」 |
| **PostgreSQL** MCP | A | ✅ 值 | 顺带解决一个**已记载的真痛点**：Git Bash 里 `psql -c "中文"` 按 GBK 发出去会 400 |
| **Chrome DevTools / Playwright** | A | 🔶 可选 | 前端有真实调试面，但那些问题多半是「看」而不是「查数据」 |
| **Qdrant** MCP | A | ❌ 不值 | 定位是「语义记忆存储」，不是翻已有 collection；且本项目是**嵌入式排他锁** |
| filesystem / fetch / Tavily | A | ❌ 不值 | 与已有能力重复（Claude Code 自带；Tavily 已在进程内且每次花钱） |
| **Exa** MCP —— **由 Agent 调用** | B | ✅ **可做** | MCP 客户端藏在 `BaseTool.run()` 里，**3.1 那五条不变量一条不破**。⚠ 唯一会静默出错的是 score 量纲，见 **3.3** |
| **GitHub** 官方 | A | ⏸ 以后 | 仓库还没传 |
| 把 5 个工具都换成 MCP | B | ❌ **明确不建议** | 见第 3 节，逐条列了会被拆掉的东西 |
| 把本项目包成服务器（全量） | C | 🔶 有硬墙 | 与后端**二选一**，见 4.2 |
| 把本项目包成服务器（**C-min**） | C | ✅ **推荐** | 只包不碰 Qdrant 的三个工具，**可与后端并存** |

---

## 2. 方向 A：开发期给 Claude Code 挂 MCP —— ✅ 推荐

### 2.1 为什么这条路便宜

**依赖成本是零。** 第三方 MCP 服务器走 `uvx` / `npx` 起**独立进程**，
`pyproject.toml` **一个字不用改**。

⚠ 这一条在本项目里比在别处更值钱：本仓库**连 `[project.optional-dependencies]` 都没有**
（已核实 —— `pyproject.toml` 只有 `[project]` / `[project.scripts]` / `[build-system]` /
`[[tool.uv.index]]` / `[tool.uv.build-backend]`，17 个依赖全是硬依赖）。
为「开发期用一下」的东西往主依赖表里塞一个包，是在引入一个**新概念**；而 `.mcp.json`
只是一份配置。

### 2.2 值得挂的

#### ✅ Neo4j —— 最值的一个

**理由不是「图谱很酷」，是取证成本**。这个项目里发生过这些事，每一件当时都是
「写个 `probe_*.py` → 跑 → 删」：

- `sum(DISTINCT t.交易额)` 静默算错钱（激光打印机少算 5.3%）
- 空 `entity` 让 `CONTAINS ""` 恒真、静默返回「交易最多」那个实体的数据
- 「打印机」有精确节点，只按频次排会静默命中「激光打印机」（107 笔 > 19 笔）
- 那笔 **66 亿的异常交易**（`某县残疾人联合会`，交易表行号 3121）
- `probe_graph_store.py` ② 里那条「与 PostgreSQL 跨技术栈对账」的常数

直连查图省掉的正是这一圈。

⚠ **一条必须澄清的边界**：`graph_store.py` 那条铁律「**绝不接受模型给的 Cypher**」
约束的是**生产 Agent**（`query_type` 走白名单分派、参数全走 `$param`），
**不是开发者**。挂个 Neo4j MCP 让我手写 Cypher **不违背它** —— 两件事的威胁模型不同：
前者防的是线上模型被提示注入诱导，后者是本地开发者在自己的库上只读查询。

⇒ 配置时**把 `NEO4J_READ_ONLY` 打开**，是这条边界的具体落点。

#### ✅ PostgreSQL —— 值，且顺带解决一个已有记载的痛点

`CLAUDE.md` 自己记着：

> ⚠ **Windows Git Bash 里 `psql -c "…中文…"` 会 400 级失败**
> （`invalid byte sequence for encoding "UTF8"`）：bash 把参数按 GBK 发出去。
> 中文 SQL 写成 UTF-8 文件后用 `psql -f file.sql`，并设 `PGCLIENTENCODING=UTF8`。

**MCP 走的是 JSON-RPC over stdio，UTF-8 全程无损 —— 这个病从根上不存在。**

另外三张表现在都只能靠探针查：`bid_records`（8751 行画像）、`conversations`、
`feedback`。其中 `bid_records` 的画像标定（省份 99.2% 是安徽、发布时间只有 12 个不同日期）
直接决定了 `pg_store.GROUP_COLS` 里为什么没有 `province` / `year` ——
这类「重新导入后要重测」的活，有个直连的查询口会顺手很多。

⚠ **取服务器要挑一下**：PostgreSQL 这边**没有权威的第一方服务器** ——
`modelcontextprotocol/server-postgres` 是**参考实现**，社区版有 Postgres MCP Pro 等。
选之前确认三件事：只读模式、凭据怎么传、有没有 statement timeout。
（本项目的 `pg_store` 把 `statement_timeout` 设成**建连参数**，正是因为
「设置了就算数」的安全断言必须验行为 —— 见 `CLAUDE.md` 的「采购记录库」一节。）

#### ↪ Exa MCP —— **搬去 3.3 了**，因为它的调用方是 Agent 而不是 Claude Code

⚠ **它与本节其余候选不是一类**：那些是「给 Claude Code 挂个工具」，
而 Exa 真正该用它的调用方是**本项目的 Agent（LLM 客户端）** ——
**完整分析在 3.3**，那里才是重点。
（所以它**不在**本节这组「方向 A 候选」里，也别按那个理解。）

Claude Code 侧当然也挂得上（`claude plugin install exa@claude-plugins-official`），
但那是顺带，别按那个理解这个提案。

#### 🔶 Chrome DevTools / Playwright —— 可选

前端有真实调试面：`.next` 被 `build` 与 `dev` 争用、SSE 边生成边到、搜索结果跳转时
两个 effect 抢同一个滚动容器。但这些问题的验证方式是 `npm run build` + curl + 肉眼看，
MCP 帮不上太大忙。**先不挂也行。**

### 2.3 不值得挂的，以及本项目特有的理由

#### ❌ Qdrant MCP —— 这条有一个别人没有的硬理由

`qdrant/mcp-server-qdrant` 是官方的，但**用不上**：

1. **它的定位不对**：那个服务器的设计意图是「把片段存进 Qdrant、以后相似检索」——
   即**语义记忆存储**。本项目要的是「翻一个已经建好的 `bid_qa` collection」，
   不是往里写。
2. **⚠ 本项目特有的硬墙**：本地 Qdrant 是**嵌入式文件模式**，对
   `data/qdrant_local/.lock` 加**排他锁**。这是仓库里反复记过的一条限制 ——
   起了 API 就不能 `ingest`、就不能跑 `agent_eval.py`。**再加一个进程去开同一个目录，
   当场抛 `RuntimeError`。**

⇒ 这条同时预告了第 4 节那道墙。**Qdrant 的排他锁是这个项目里所有「再加一个进程」
想法的共同天花板。**

#### ❌ filesystem / fetch / Tavily

与已有能力重复：Claude Code 自带文件工具和 WebFetch/WebSearch；Tavily 已经在
`web_search.py` 里、且**每次提问花 1 个 credit**（免费档 1000/月）。
再挂一层只是多一个花钱的口子。

### 2.4 ⚠⚠ `.mcp.json` 的安全红线 —— 因为你要把这个仓库公开

`.mcp.json` 是**项目级作用域**的文件，设计意图是**提交进版本库、团队共享**
（config 里三种作用域：`local` 存 `~/.claude.json` 不共享、`project` 存 `.mcp.json` 共享、
`user` 存 `~/.claude.json` 全局）。

**而本仓库的 `.gitignore` 不挡它**（已实测：`git check-ignore -v .mcp.json` 无输出，
`exit=1` —— 当前只挡了 `.env` / `.env.local` / `models/` / `data/index/` /
`data/qdrant_local/`）。

⇒ **两条纪律**：

1. **`.mcp.json` 里绝不出现字面凭据**，一律用 `${VAR}` / `${VAR:-默认值}` 展开
   （Claude Code 支持，作用范围含 `command` / `args` / `url` / `headers`）。
2. ⚠⚠ **但 `${VAR}` 展开的是 Claude Code 进程的环境变量，而本项目的 `.env`
   只有 Python 进程读得到**（`config.py:124` 的 `env_file=PROJECT_ROOT / ".env"`
   是 pydantic-settings 内部读的，**不 export 给 shell**）。
   ⇒ 直接写 `${NEO4J_PASSWORD}` 大概率展开成**空串**，而且**不报错**，
   表现是「服务器起来了但连不上库」。

   **推荐做法是绕开这个坑**：用 **local 作用域**把凭据存在 `~/.claude.json`，

   ```bash
   claude mcp add --scope local --env NEO4J_PASSWORD=… -- uvx mcp-neo4j-cypher@latest
   ```

   它**不进仓库**，也就不存在「提交了带凭据的 `.mcp.json`」这件事。
   要共享的那份 `.mcp.json` 里只留不带凭据的骨架。

⚠ 这条与本仓库**那 6 个至今未轮换的凭据**（`docs/项目现状评估与后续建议.md:58`
白纸黑字记着「密钥曾在会话记录里被完整打印过、至今未轮换」）叠加，风险是实打实的。

另外两条机制性的：项目级服务器**首次使用要一次批准**（`claude mcp list` 里显示 pending
until then），要挡可以用 `disabledMcpjsonServers`；工具在会话里长这样
`mcp__<服务器名>__<工具名>`。

---

## 3. 方向 B：让 `/api/agent` 用上 MCP

⚠⚠ **本节只否掉读法 ①「把现有五个工具搬到进程外」，不否掉 MCP 本身。**
读法 ②（用 MCP **新增**一个工具）的完整分析在 **3.3** ——
判据是「**谁在进程外**」：搬走现有工具 ⇒ 机制跟着走 ⇒ 拆掉；
只把新工具的后端放外面 ⇒ Agent 侧没变 ⇒ 不破。

核心论点一句话：

> **现有那五个工具不是「五个函数」，是长在一套本地机制上的。**
> 把工具挪到进程边界之外，等于把这些机制**逐条拆掉**。

### 3.1 会被拆掉的东西（逐条）

| 机制 | 在哪儿 | 换成 MCP 之后 |
| --- | --- | --- |
| **`SourceRegistry` 全局编号** | `tools/base.py:194-233` | 正文 `[n]` 与 `Markdown.tsx` 的 `/\[(\d{1,2})\]/g` 靠它连续；`add()` 返回的 `(fresh, known, dropped)` 还被 `empty_reason()` 用来区分「没命中」与「撞来源上限」。MCP 只回文本，**这两件事一起没了** |
| **泄漏恢复 `recover_tool_calls()`** | `tools/base.py:704-783` | 要拿**本轮工具表的 JSON Schema** 做名字/参数归属投票。工具表在远端进程里，这套机制得整体重写。⚠ 它**不是装饰** —— 是为 DeepSeek 偶发把工具标记当正文吐出来而建的 |
| **结果质量检查 `assess_outcome()`** | `tools/base.py:412-465` | 依赖 `ToolOutcome` 的**结构化字段**（`dropped` / `graph.total` / `pg.total`），MCP 返回的是纯文本 |
| **提示词按工具表拼装** | `agent.py:253` | 它**从工具表反推**提示词开关（`tools = tools_for(enable_web) ...`），让「提示词提到工具 ⟺ 工具列表里有它」**由构造成立**。工具搬出进程，这条不变量要么重写要么失效 |
| **`load_skill` 的描述 import 期扫盘生成** | `tools/rag_tools.py:548` | 往 `skills/` 丢一个 `<名字>/SKILL.md` 就自动生效，**不用改任何代码** —— MCP 里没有对应物 |
| **工具的 schema 清洗层** | `tools/base.py:48-110` | `_clean_property` / `_drop_noise` 是为了让 `_normalize_args` 读得懂；换成 MCP 的 `inputSchema` 后这层要么重接要么报废 |

### 3.2 收益呢？

**没有。**

MCP 的三个卖点在这里**一个都用不上**：

- **跨进程** —— 五个工具的数据源（Qdrant / Neo4j / PostgreSQL / Tavily）全在**同一台机器**，
  现在**同一个进程**里，调用是普通的函数调用或 `asyncio.to_thread`
- **跨语言** —— 全是 Python
- **复用现成的服务器** —— 见第 2 节，真正值得复用的（Neo4j / PostgreSQL）是**开发期**
  需求，不是生产链路的需求

⇒ **把现有工具搬出去，是纯成本。**

⚠ 措辞要说准，而且**这条限制只作用于读法 ①**：这**不是**「MCP 不好」。是「**这个项目
已经把 MCP 想解决的问题，在自己这一层解掉了**」—— `BaseTool` + `to_openai_schema()` +
`execute_tool()` 就是一套自制的工具协议层，而且它比通用 MCP 多知道很多本项目的语义
（来源编号、结果质量、泄漏归属）。通用协议换不回这些。

⚠ **但那一层多出来的语义，都是「工具被谁调用」这一侧的属性，不是「工具内部怎么取数」
的属性。** 所以只要新工具**照常产出一个 `ToolOutcome`**，它的后端走 HTTP、走 SQL、
还是走 MCP，Agent 那一侧**看不出区别** —— 这正是 3.3 成立的原因。

### 3.3 Exa MCP：让 **Agent** 多一个搜索引擎

⚠⚠ **调用方是本项目的 Agent（LLM 客户端），不是 Claude Code。** 这是本节与第 2 节
所有候选的分野 —— 那边是「给开发者挂个工具」，这边是「给线上 Agent 多挂一个工具」。

#### 需求侧：成立，但比「补充 Tavily」这个说法**窄得多**

「多一个引擎」听起来总是好的，但逐条对一遍本项目**已记载**的 Tavily 问题，
第二引擎真正治的只有**一条**：

| 已记载的问题 | 第二引擎有用吗 |
| --- | --- |
| 17% 偶发空结果 | ❌ 基本没用 —— `_EMPTY_RETRY=2` 已把残率压到 ~0.5% |
| **偶发 top 分整体塌陷**（同一句 0.8263 → 0.5356） | ✅ **有用 —— 这是唯一无缓解的故障** |
| 1000 credits/月 的额度 | ❌ 没用 —— Exa 同样收费，换汤不换药 |
| 「最新公告」的时效性 | ⚠ **可能更差** —— Exa 的语义索引**每小时**更新，<1 小时的事件可能还没有 |

⇒ **真正的动机只有第二条**：闸门整批丢弃时**没人兜底**。其余三条都别拿来说事。

⚠ 顺带一条要写进结论的：**两家的跑分不能作为选型依据**。Exa 自己的榜单上 Exa 赢、
Tavily 自己的评测里 Tavily 赢，独立来源给出的置信区间是**重叠的**。
⇒ 谁都不是中立的。选它的理由是「**另一个独立索引**」这件事本身（相关性不同源、
覆盖面不同源），不是「它更准」。

#### 形态：MCP 客户端藏在 `BaseTool.run()` 里面

```
Agent 侧看到的：  一个普通的 BaseTool（名字 search_web_exa）
实际执行的：      BaseTool.run() 内部起 MCP 客户端 → 调 mcp.exa.ai/mcp → 解析 → 回 ToolOutcome
```

⇒ **3.1 那五条不变量一条都不破**，因为出进程的只是**一个新工具的实现细节**，
不变量全在 Agent 这一侧：

| 3.1 列的不变量 | 为什么不受影响 |
| --- | --- |
| `SourceRegistry` 全局编号 | 工具回的是 `WebRow`，`[n]` 仍由 Agent 侧最后分配 |
| `recover_tool_calls()` 归属投票 | 新工具与另外五个一样在**本轮工具表**里，schema 照 `to_openai_schema()` 静态生成 |
| `assess_outcome()` 结果质量 | 仍走 `ToolOutcome`，`empty_reason()` 照常覆写 |
| 提示词按工具表拼装 | 照 `tools_for()` 拼 —— 加了就提到，没加就不提，`agent.py:253` 的反推照旧成立 |
| `load_skill` 的描述 import 期扫盘 | 与 MCP 无关，原样 |

⚠ 所以 **3.1 否掉的是「把现有五个工具搬出去」，不是「新工具的实现可以用 MCP」** ——
两者别混。

#### 关键设计：**不新增「该用哪个引擎」的服务端判据**

两个工具**同时在** `tools_for()` 里，**模型自己选**：

```
search_web        → Tavily（现有，一个字不动）
search_web_exa    → Exa（新增）
```

这是本仓库处理工具路由的既有方式（法规→检索、实体→图谱、条件筛选→数据库，
全写进提示词让模型判）。**没有服务端替它做语义判断** —— 那正是 `/api/chat` 那三条
已删除的自动联网触发死于的坑。

⚠⚠ 特别不要做的是**「Tavily 塌陷时自动兜底到 Exa」**：那个判据是「塌陷了没有」，
而它落在闸门那一句上 —— 即**服务端猜语义**。塌陷与「网上真没有」在响应上
完全分不开（同一段代码、同一个 `score`），这就是它不能做兜底的原因。

#### 适配层：用 `web_search_advanced_exa`，**不是** `web_search_exa`

⚠ 这条是查证后才定下来的，差别很实 —— 我上一版以为「MCP 只回 markdown」，
**那个判断是错的**，Exa 托管端点上有一个直接回 JSON 的：

| 工具 | 托管端点上的返回 |
| --- | --- |
| `web_search_exa` | **markdown 文本**（`{content:[{type:"text", text: formatSearchResults(...)}]}`） |
| `web_search_advanced_exa` | **JSON text** → `json.loads()` 直接用 |
| `web_fetch_exa` | 整页正文 |
| `agent_run` | 一次带推理的深搜 |

```jsonc
// web_search_advanced_exa 的 results[] —— 与 WebRow 几乎同名
{"results": [{"id": "...", "title": "...", "url": "...",
              "publishedDate": "...", "author": "...",
              "text": "...", "score": 0.87}]}
```

映射到 `WebRow`：`url`/`title` 同名；`text` → `content`；`publishedDate` → `published_date`；
`score` → `score`。**`WebRow` 一个字不用改** —— 它是对的抽象边界，两个引擎都映得进去。

⚠⚠ **别挂 `web_fetch_exa`**：`web_search.py` 的 docstring 写着「只取抽好的 `content` 片段，
不取 `include_raw_content`（整页 markdown），否则 token 成本与注入面同时爆炸」。
那个工具的定位与本项目这条纪律**直接冲突**，挂上等于把已经决定不要的东西请回来。

⚠⚠ **`:…/mcp?tools=` 是「替换」语义，不是追加** —— 只写 `web_search_advanced_exa`
会把默认集整个换掉，所以想要的**必须列全**。

#### ⚠⚠ 必须过的一道关：Exa 的 `score` **不是** Tavily 的 `score`

**这是本提案唯一会静默出错的地方**，其余都是工程琐事。

```python
# web_search.py:343-345
threshold = settings.web_search_min_score          # 0.70
if threshold > 0 and rows[0].score < threshold:
    return []
```

`0.70` 是**针对 Tavily 的分数分布实测标定的**（答得出的 0.7863~0.9077、
没答案的 0.093~0.6594，空带只剩 0.127 宽）。
而 **Exa 的 `score` 是 embedding 语义相似度**，与 Tavily 那个面向 grounding 的相关性分
**是不同的量**（第三方口径：Exa 0.85+ 算高置信、0.6~0.7 是弱信号）。

⇒ 拿 Exa 的行去套 `0.70`，**不报错、不崩溃**，只是要么大面积误杀、要么形同虚设 ——
与 `MIN_VECTOR_SCORE` / `MIN_BM25_SCORE` / `MIN_RERANK_SCORE` 换语料后不重标是**同一个病**。
**必须单开一个 `exa_min_score`，重新量分布再取值**（照 Tavily 那次的量法：
相关问题与无答案问题各取一批，看两簇之间的空带落在哪）。

⚠ 附带一条：混用两个引擎时，**闸门只能在同一个引擎内部做** ——
两家的分不同量纲，混进一个 list 再按 score 排序是**没有意义的排序**。

#### ⚠⚠ 参数名必须与 `search_web` **错开**（否则泄漏恢复会静默退化）

`_match_schema()` 是「每个参数给每个工具投一票」的多数决，**0 票或并列即整条丢弃**。
若新工具照抄 `web_query` + `max_results`，那么一个**名字对不上**的泄漏标记
（模型把两个工具名揉在一起，这正是它会干的事）会得到 **1:1 平票 → 整条丢弃** ——
而今天同样的标记是能稳定判给 `search_web` 的。**不报错，只是恢复不出来。**

⇒ 新工具用自己的参数名（如 `exa_query` / `exa_max_results`），并在类上挂
`name_scoped_aliases = {"query": "exa_query"}`（照 `WebSearchTool` 的成例）。
⚠ 那条 **`named is not None` 的闸必须保留** —— 它保证「本轮没挂的工具不享受名字限定别名」，
`probe_web_search.py` ⑧ 钉着。

#### 代价（说清楚，但都不拦路）

1. **要加 `mcp` 依赖** —— 本项目**连 `[project.optional-dependencies]` 都还没有**
   （已核实），所以是往主依赖表里加，这是比前几节候选重一点的一步，值得单独决定。
2. **会话生命周期**：streamable HTTP 是**有状态**的（`Mcp-Session-Id`），
   要么接进 lifespan，要么每次调用现连现断。
3. **多一跳**：Exa 的托管 MCP 是它自己 REST API 的包装，比直连多一次中转和一层
   第三方可用性依赖（延迟上大致可忽略）。
4. ⚠ **Exa 索引每小时更新**：<1 小时的事件可能搜不到。而「最新公告」恰好是这个功能的
   主场 —— 这一条要如实告诉用户，别让 `search_web_exa` 看起来像「一定能搜到最新的」。

#### 落地清单

1. `pyproject.toml` 加 `mcp`
2. **写一个 `BaseTool` 子类**（名字 `search_web_exa`）—— 走「新增工具要改四处」那份清单：
   `tools/rag_tools.py` 注册 + `rag.py` 提示词片段 + `ToolCallBlock.TOOLS` + （如需）前端
3. `config.py` + `.env` + `.env.example` **三处一起**加 `exa_api_key` / `exa_min_score`
   —— ⚠ 漏一处会被 `extra="ignore"` **静默丢弃**（本仓库记过四次）
4. ⚠ **手写自己的 `_NOT_CONFIGURED`**（`base_client.py` 有默认那句）——
   `rag._no_key_hint()` 是从它里面切键名的，漏写会退成中性话
5. 真机验：`scripts/probe_web_search.py`（151 条，按单引擎形状钉着）+
   打一次 `/api/agent` 看模型会不会在对的场合选 Exa、参数名有没有串

---

## 4. 方向 C：把本项目包成 MCP 服务器 —— ✅ 唯一真正的对外增量

**谁得到什么**：Claude Desktop / Cursor / 别人的 Agent 能**直接问这个招投标知识库**。
这是方向 A 和 B 都给不了的 —— A 只是给开发者省事，B 根本不产生新能力。

### 4.1 适配层确实很薄（贴事实，不是感觉）

| 要做的 | 现成的东西 |
| --- | --- |
| 「注册表 → MCP 工具列表」 | `BaseTool.to_openai_schema()` 产出的 `{"type":"function","function":{…,"parameters":…}}` 里，**`parameters` 正是 MCP 要的 `inputSchema`**；`schema_required` 直接对应 `required`（它本来就是**显式声明**、不从 Pydantic 推导的 —— `base.py:312`） |
| 「MCP 调用 → 执行」 | `async def execute_tool(call: ToolCall, ctx: ToolContext) -> ToolOutcome`（`tools/base.py:469`），把 MCP 的 `(name, arguments)` 包成一个 `ToolCall` 即可 |
| 「谁在本轮可用」 | `_REGISTRY`（全部已知）vs `TOOL_SCHEMAS`（挂出去的）—— 见下 |

两个方向都是**薄转发**。用官方 SDK 的话就是 `from mcp.server.fastmcp import FastMCP`
配 `@mcp.tool`。

### 4.2 ⚠⚠ 硬墙：Qdrant 排他锁

**这是本节最值钱的一条**，也是「拍脑袋说『包一层就行』」一定会撞上的东西。

- `ToolContext` 的构造参数里有 `retriever`（`tools/base.py:263-280`），
  而 `get_retriever()` 打开的是**嵌入式** `data/qdrant_local/`，**同目录独占锁**。
- 后端在跑时，第二个进程直接抛 `RuntimeError`。

⇒ **一个「五个工具全包出去」的 MCP 服务器，与后端二选一，不能并存。**

三条出路，各有各的适用范围：

| 出路 | 覆盖 | 代价 |
| --- | --- | --- |
| ① 代理到已有的 `POST /api/search` | 只有检索那一个工具；纯检索、不经大模型 | **只在后端跑着时可用** |
| ② 直连 import（`get_retriever()`） | 全部五个 | **只在后端停着时可用** |
| ③ **只包不需要 Qdrant 的工具** | 图谱 / 采购库 / 技能 | ✅ **可与后端并存** |

### 4.3 建议范围：**C-min**

> 只包 `search_knowledge_graph` + `search_procurement_records` + `load_skill`。

**为什么这三个能独立跑 —— 有现成证据，不是推测**：
`probe_graph_store.py` 与 `probe_pg_store.py` **本来就是独立进程**直连 Neo4j / PostgreSQL
执行的（它们连大模型都不需要）。所以「不依赖 Qdrant 的独立进程」这条路**已经验证过**。
`load_skill` 更是纯文件读（`skill_store.load()`），连网络都没有。

三个工具都不碰那把锁 ⇒ **可以与后端同时运行**。

⚠ **一条会绊人的缝，要点明**：`ToolContext.retriever` 是**必填字段**
（类型标注是 `Any`，因为只有检索工具会调 `.search`）。C-min 只挂那三个工具时
它**从头到尾没被读过**，但 dataclass **仍然要求传**。
⇒ 别以为可以 `ToolContext()` 空构造；传个占位对象，并写好注释说明为什么。

⚠ **另一条已核实的好消息**（能省掉一个 stdio 服务器常见的坑）：

```python
# config.py:122-128
model_config = SettingsConfigDict(
    env_file=PROJECT_ROOT / ".env",     # ← 绝对路径，不是 ".env"
    ...)
```

`env_file` 是**绝对路径**（`PROJECT_ROOT` = `D:\rag_agent`，已实测打印确认）。
⇒ MCP 服务器**不依赖 CWD**，从任何目录起来都读得到正确的 `.env`。
「cwd 不对就读不到配置」在这里**不成立**。

⚠ 还有一条：`graph_store.ENABLED` / `pg_store.ENABLED` / `skill_store.ENABLED` 都是
**import 期常量**（`skill_store.py:155` 的判据甚至是「目录里有没有文件」）。
所以 C-min 服务器要用**同一份 `.env`** 起，否则工具表会与后端不一致。

---

## 5. 落地清单

> ⚠ 标题原为「**只是清单，本次不执行**」。2026-09-30 起 A 之外的**两条主路径都已落地**
> （Exa = 方向 B，自建服务器 = 方向 C-min），清单改记「实际做了什么」与**三处偏离**。
> ⚠ **权威记录是 `CLAUDE.md` 的「Exa 备用搜索引擎」与「自建 MCP 服务器」两节**，
> 本节只保留当初的判断轨迹与差异。

### 若要 A

1. 用 **local 作用域**加服务器（凭据不进仓库）：
   `claude mcp add --scope local --env … -- uvx …`
2. 要共享的部分另写一份不带凭据的 `.mcp.json` 骨架；
   ⚠ 加之前先想清楚它**会被提交**（`.gitignore` 不挡它）。
3. 验证：`claude mcp list`（看有没有 pending 批准）→ `/mcp` 面板 →
   真问一句「激光打印机一共多少笔、合计多少钱」（期望 107 笔 / 973172.0，
   与 `probe_pg_store.py` ② 和 `probe_graph_store.py` 的 `LASER_TOTAL` 互相印证）。

### 若要 Exa（**方向 B，给 Agent 用** —— 主路径）✅ **已落地（2026-09-30）**

**清单在 3.3 末尾，这里不重复。** 一句话版：`pyproject.toml` 加 `mcp` →
写一个 `BaseTool` 子类 `search_web_exa`（MCP 客户端在 `run()` 里面）→
`config.py` 三处加 Key → 真机验。⚠ 三件最容易漏的事：**score 要单开常量重标**、
**参数名要与 `search_web` 错开**、**别挂 `web_fetch_exa`**。

⚠⚠ **实测推翻了两处本文档的预设**（细节见 CLAUDE.md 的「Exa 备用搜索引擎」一节）：
① **那个托管端点根本不返回 `score`** ⇒ `exa_min_score` 是个**死旋钮**（留着是为了端点哪天
开始带分数时自动生效，不是没接线）；② 参数名错开**不够** —— 两个引擎的查询词各成一派，
会经 `recover_tool_calls()` 的**参数投票**把一次 `search_web_exa` 拐去跑 Tavily
（**双向**都成立），解药是工具类上的 `name_scoped_aliases` 两条**互为镜像**的配置。

⚠ 端点与认证（两条路共用）：

```
https://mcp.exa.ai/mcp                    # streamable HTTP
```

- 认证走 `Authorization: Bearer` / `x-api-key` **头**
  ⚠ **别用 `?exaApiKey=…`** —— Key 进 URL 就会出现在日志与异常信息里，
  与本仓库「认证走 Header，绝不进 URL」那条既有纪律冲突（`web_search.py:100`）
- `?tools=` 是**替换**语义：要 `web_search_advanced_exa`，就得把它和其他想要的**列全**

<details><summary>若只想给 Claude Code 挂（顺带，不是本提案的重点）</summary>

```bash
claude plugin install exa@claude-plugins-official     # 走官方远程端点，无需 uvx/npx
# 或手工
claude mcp add --scope local --transport http exa https://mcp.exa.ai/mcp \
  --header "Authorization: Bearer $EXA_API_KEY"
```

</details>

### 若要 C-min —— ✅ **已落地（2026-09-30）**

1. ~~`uv add mcp`~~ ✅ **不需要**：`mcp>=1.19.0` 早已是**硬依赖**（方向 B 的 Exa 那次加的），
   `mcp.server.lowlevel` 与 `mcp.server.streamable_http_manager` 同包。
2. 新写一个入口 ✅ `src/bidding_qa/mcp_server.py`（`build_server()` + `_serve_http()` +
   `main()`；`python -m bidding_qa.mcp_server`），由 `settings.mcp_server_url` **反解**监听三件套。
   客户端半是 `src/bidding_qa/mcp/mcp_bridge.py`，路由分支落在三个 `run()` 里。
3. 验证 ✅ **后端跑着起了服务器**（4.2 那面墙的验收）、真调了三个工具、**并做了杀服务器反证**。
   证据见 `CLAUDE.md` 的「自建 MCP 服务器」一节（那一节也是本节各项实现的**权威记录**）。

**三处与本文档原计划的偏离**（都是有意的，理由写在 CLAUDE.md 那一节里）：

| # | 本文档原计划 | 实际做法 | 为什么 |
| --- | --- | --- | --- |
| 1 | 去重在服务器侧（`ToolContext` 占位 + 每次新建） | **去重留在客户端的 `run()` 里**，分支在它**之后** | 提到 `execute_tool` 会把 `ctx.seen` 的键**提前**写进去：模型枚举写错拿到报错、改对了重试时撞 `duplicate_query`，而正确行为是拿到「可用类型：…」 |
| 2 | `call_tool` 直接包 `ToolCall` 调 `execute_tool()` | 同上，**但白名单查在它之前** | 服务器一 import `rag_tools`，`_REGISTRY` 里就躺着**六个**工具；不在白名单就直接回错误文本，否则检索工具会以 `retriever=None` 被真执行 |
| 3 | （未提及）只回文本 | **回 2-tuple**：`content=[中文正文]` + `structuredContent=载荷` | 只回 dict 的话 SDK 会把载荷**美化打印成 JSON 塞进 content**，第三方客户端看到一屏 JSON；而正文与结构化结果要被**分开**消费（前者给模型、后者给前端块） |

⚠ 另有两条**本文档没预料到**的实现约束（已实测）：**必须用 lowlevel `Server`**（FastMCP 会从
Pydantic 重新推导 schema，毁掉 `Any` + `json_schema_extra` 那套容错），以及
**`@server.call_tool(validate_input=False)` + 不设 `outputSchema`**（前者防中文报错被 English 化、
后者是因为 SDK 在「声明了却没返回」时会抛错）。

### 若要 B（读法 ①：把现有五个工具搬出去）

**不建议**，见第 3 节。真要做，先准备好重写来源编号、泄漏恢复、结果质量三套机制。
⚠ 注意这与上面那条 Exa 清单**不是同一件事**（读法 ②），别混。

---

## 6. 明确不做

> ⚠⚠ **本节记录的是写这份文档时（2026-09-30 之前）的状态，其中两条已经过期。**
> 保留原文是为了留下当初的判断轨迹，**别按它理解现在的代码**。

- ❌ 写这份文档时**只写了这一份 `MCP.md`**：没建 `.mcp.json`、没加依赖、没写服务器代码。
  ⚠ **`.mcp.json` 至今仍然没有**（`.gitignore` 不挡它，所以是**有意**不建的）。
- ❌ 写这份文档时没改 `pyproject.toml` / `.gitignore` / `.env` / `.env.example` / `CLAUDE.md`。
  ⚠ 后来这几处**都改了**：`mcp>=1.19.0` 进了硬依赖、Exa 那五组键 + `mcp_server_url`
  进了 `config.py` / `.env.example`、`CLAUDE.md` 加了「Exa 备用搜索引擎」与「自建 MCP 服务器」两节。
- ❌ ~~**Exa 也没接**：`web_search.py` / `config.py` 一个字没动，`exa_api_key` /
  `exa_min_score` 都还不存在。~~ ⚠⚠ **这句已过期**：方向 B（Exa）与方向 C-min **都已落地**，
  `exa_api_key` / `exa_min_score` / `mcp_server_url` 都在 `config.py` 里。
  ⚠ 但 Exa 那条**端到端仍未打过**（本机 `EXA_API_KEY` 为空）—— 见 CLAUDE.md 那节的「已知缺口」。

---

## 附录 · 本次核实过的事实

> ⚠⚠ **本节是写这份文档时（2026-09-30 之前）的实测快照，几条已经过期。**
> 命令原样保留是为了留下当初的判断轨迹与复现手法，**但今天重跑会得到相反的输出** ——
> 每条下面都标了今天的结果（`grep -ril mcp …` 从 0 变成 25，就是整个方向 B + C-min 的落地）。

写这份文档时逐条跑过，都可以复现：

```bash
grep -ril mcp src scripts eval docs CLAUDE.md README.md pyproject.toml
#   → 无输出。全仓零 MCP（.venv / node_modules 除外）
#   ⚠ 今天：**25 个文件命中**（mcp_client / mcp_bridge / mcp_server / web_search_exa /
#     probe_mcp_server / CLAUDE.md 两节 / pyproject.toml / .env.example …）。

git check-ignore -v .mcp.json
#   → 无输出，exit=1。⚠ 也就是说 .mcp.json 会被提交，不是被忽略
#   ⚠ 今天同前 —— **且这条仍然有效、仍然是有意的**：`.mcp.json` 至今没建（§6）。

uv run python -c "from bidding_qa.config import PROJECT_ROOT; print(PROJECT_ROOT)"
#   → D:\rag_agent

grep -ci 'skill\|技能\|load_skill' CLAUDE.md
#   → 0
#   ⚠ 今天：**14**。但那个 0 想说的问题**只解决了一半**，见下面「顺带发现 1」。
```

以及读 `pyproject.toml` 确认：17 个依赖里没有 `mcp` / `fastmcp`；
**没有** `[project.optional-dependencies]` 段。
⚠ 今天前半句**已过期**：`mcp>=1.19.0` 是**硬依赖**（`pyproject.toml:25`，方向 B 那次加的，
这次 C-min 直接复用）。后半句仍成立 —— 本仓库**没有** optional-dependencies 这个概念，
这正是「Exa 与本服务器的 SDK 都是硬依赖」那条判断的依据。

### 3.3 那节引用的 `web_search.py` 事实（已逐条核对）

```bash
sed -n '343,345p' src/bidding_qa/web_search.py    # 闸门：rows[0].score < web_search_min_score
sed -n '242,269p' src/bidding_qa/web_search.py    # _parse_rows：Tavily JSON → WebRow，五个字段
sed -n '51p'      src/bidding_qa/web_search.py    # ENABLED = bool(settings.tavily_api_key.strip())
```

⇒ 三条结论：① **`ENABLED` 是单引擎常量**（`agent.py` 挂不挂 `search_web`、`rag.py` 提示词
提不提它，全读这一个值）；② **闸门只比 `rows[0]`**，所以分数的**量纲**直接决定整批生死；
③ **`_parse_rows` 是 Tavily 专属**，换引擎要另写，但 `WebRow` 本身是通用的。

### Exa 托管端点的工具返回形状（3.3 那节的依据）

| 工具 | 返回 |
| --- | --- |
| `web_search_exa` | **markdown 文本**（`{content:[{type:"text", text: formatSearchResults(...)}]}`） |
| `web_search_advanced_exa` | **JSON 文本** → `json.loads()` |
| `web_fetch_exa` | 整页正文 |
| `agent_run` | 一次带推理的深搜 |

⚠⚠ 这纠正了我写第一版时的一个**错判**：当时以为「MCP 只回 markdown，所以结构化数据
会被降级成文本」，并据此得出「该用 REST 不该用 MCP」。**`web_search_advanced_exa`
直接回 JSON**，所以那个反对意见不成立 —— 真要说，REST 与 MCP 在这里的差别只是
「多一跳第三方包装」而已。

⚠ MCP 2025-06-18 加了 `Tool.outputSchema` + `CallToolResult.structuredContent`，
**但 Exa 的官方服务器看起来没有用上**（仍走 text content block）。所以别指望
`structuredContent`，直接解析那个 JSON 文本才是能用的路。

### 顺带发现的两件事（只作观察，不在本次动手范围）

1. ⚠ **`CLAUDE.md` 对「技能」整套是零命中的。**
   `grep -i 'skill|技能|load_skill' CLAUDE.md` → **0 匹配**。
   也就是说 `skill_store.py` + `load_skill` 工具 + `src/bidding_qa/skills/`
   **整套没进项目说明**。而且 `tools/rag_tools.py:1,3` 的模块 docstring 还写着
   「四个具体工具」—— 实际是**五个**（第五个是 `LoadSkillTool`）。

   ⚠ 2026-09-30 现状：**两半各变了一半，缺口还在**。
   - 后半句**已修**：`rag_tools.py` 的 docstring 现在写的是「六个工具」并逐名列出（含「加载技能」）。
   - 前半句**只解决了一半**：`CLAUDE.md` 的命中从 0 变成 14，但那 14 处**全是**
     「`load_skill` 是六个工具之一」这一类**顺带提名**（工具表、「工具层」那节、
     新加的「自建 MCP 服务器」一节）。
     `skill_store.py` / `load_skill` / `src/bidding_qa/skills/` **仍然没有专节** ——
     技能怎么被读进上下文、自动扩容的判据、`.claude/skills/` 与 `src/bidding_qa/skills/`
     两份的**手工对拷 + 静默漂移**，一整套仍只写在代码 docstring 里。
     这是**记在案的已知缺口**：`CLAUDE.md` 的「Agent 模式」一节里那句
     「⚠ `load_skill` **本文件没有专节**，细节看 `skill_store.py` 的 docstring 与
     `src/bidding_qa/skills/`（已知缺口，见 `MCP.md` 附录）」指的就是本项，不是漏改。
2. ⚠ **技能文件在本仓库有两份**：`.claude/skills/<名字>/SKILL.md`（9 份，给操作员）
   与 `src/bidding_qa/skills/<名字>/SKILL.md`（2 份，给线上 Agent）。
   同步方式按 `skill_store.py` 自己的 docstring 是**手工 `cp -r` 整目录对拷**，
   且它明确写着「**漂移是静默的**（线上 Agent 会照着旧流程说）」。
   目前两边**只有 2 个同名技能**，`.claude/skills/` 下另外 7 份线上 Agent 读不到。

---

## 资料来源

- [Connect Claude Code to tools via MCP — Claude Code Docs](https://code.claude.com/docs/en/mcp)（作用域、`.mcp.json` 格式、`${VAR}` 展开、批准机制）
- [Tools — FastMCP](https://gofastmcp.com/servers/tools) / [MCP Python SDK: What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/)（`@mcp.tool`、`inputSchema`、v2 协议）
- [Database, Vector, and Analytics MCP Servers — Graphify Guides](https://graphify.net/ai-coding/mcp/database-vector-analytics-mcp-servers/)（Neo4j / PostgreSQL / Qdrant 各方服务器的官方性与成熟度对比）
- **Exa 侧**（3.3 的事实来源）：官方仓库 `github.com/exa-labs/exa-mcp-server`、
  Exa 文档站 `docs.exa.ai`、以及托管端点本身 `https://mcp.exa.ai/mcp`
  —— 三处各有侧重：仓库看**工具名与参数**、文档站看**认证与 `?tools=` 语义**、
  端点看**实际返回的 content block 形状**。
  ⚠ 最后一条**只能实测**：文档不会把 `formatSearchResults()` 的产出写清楚，
  我是按官方仓库里那个函数才确认 `web_search_exa` 回的是 markdown 的。
- [MCP 2025-06-18 变更](https://modelcontextprotocol.io/specification/2025-06-18/changelog)
  （`Tool.outputSchema` + `structuredContent`；⚠ 实操上还不能指望它，见附录）
- ⚠ **Exa 的 `score` 语义**（0.85+ 高置信 / 0.6~0.7 弱信号）来自第三方指南，
  **不是 Exa 官方文档的承诺** —— 正因为口径本身就不硬，3.3 才要求
  **就本项目自己的语料重新量分布**，而不是照抄任何一个数。
- [Best Database MCP Servers in 2026 — MCP Directory](https://mcp.directory/blog/best-database-mcp-servers-2026)

**Exa（3.3 那节）**：

- [exa-labs/exa-mcp-server — GitHub](https://github.com/exa-labs/exa-mcp-server)（官方仓库：远程端点、`?tools=` 语义、认证方式）
- [Exa vs Tavily: Search API Benchmarks — Exa](https://exa.ai/versus/tavily) ⚠ **厂商自评**
- [tavily-search-evals — Tavily](https://github.com/tavily-ai/tavily-search-evals) ⚠ **另一家厂商自评，结论与上一条相反**
- [Exa vs Tavily vs Perplexity — The Neural Base](https://theneuralbase.com/exa-api/learn/beginner/exa-vs-tavily-vs-perplexity/)（第三方：Exa `score` 是语义相似度、0.85+/0.6~0.7 的经验口径、索引每小时更新）
- [Exa vs Tavily vs Serper vs Brave Search for AI Agents — rhumb.dev](https://rhumb.dev/blog/exa-vs-tavily-vs-serper-vs-brave-search)（第三方：唯一站得住的差异是**新鲜度**，质量差异的置信区间重叠）
- [Best AI Search APIs for Agents 2026 — API Scout](https://apiscout.dev/guides/best-ai-search-apis-2026)

⚠⚠ **两家的自评结论互相矛盾**（Exa 的榜 Exa 赢，Tavily 的榜 Tavily 赢），
**都别当中立证据**。第三方那几份的一致说法是「质量差距很小、置信区间重叠」——
所以 3.3 里选第二引擎的理由**建立在「兜底」而不是「Exa 更强」上**，这条要守住。

⚠ 数据库类 MCP 项目迭代很快，**选型当场自己确认一眼**仓库归属、是否只读、
凭据怎么传、有没有 timeout/结果上限。
