# Agent 模式

> `CLAUDE.md` 拆出来的深潜正文（原「Agent 模式」一节）。
> 改 `agent.py` / `rag.py` 的 Agent 专属提示词 / 泄漏恢复之前，先把这里读完。

## Agent 模式（`/api/agent`）

**这是前端唯一使用的链路**（2026-09-20 起，见「项目概览」）：把工具交给模型自主调用。
`/api/chat` 是与之并存的**写死流水线**（检索→塞提示词→作答），两者请求体结构相同；
`lib/api.ts` 原先按 `agent` 字段分派端点，现在**硬编码 `/api/agent`**。

⚠ 由此带来的两个真实差异，改前端时容易踩：① 每轮问答都会多至少一次模型往返，比写死流水线慢；
② Agent 链路会发 `answer_reset`（`/api/chat` 从不发），`useChat` 必须继续处理它。

**六个工具已注册、按条件挂出去**，定义都在 `bidding_qa/tools/rag_tools.py`
（`load_skill` 在 `skill_store.py`，见 `工具层.md`）：

| 工具 | 回答什么 | 主要参数 | 挂出去的条件 |
| --- | --- | --- | --- |
| `search_bidding_knowledge` | 法规条文、政策口径、办事流程 | `query`（可改写重查） | 无条件 |
| `search_knowledge_graph` | **实体之间的关系**：某企业的供应商/采购商、某标的物谁供过、地区分布、交易笔数排行 | `query_type`（枚举）+ `entity` | `graph_store.ENABLED` |
| `search_procurement_records` | **项目级的明细与统计**：按省份/时间/金额/类别/来源筛项目、精确条数与合计、按维度分组 | 结构化过滤器（见 `图谱与采购库.md`） | `pg_store.ENABLED` |
| `load_skill` | 把一份**技能说明书**读进上下文（数据分析流程、回答诊断） | `skill`（必填） | 无条件 |
| `search_web` | **知识库里没有的、有时效性的东西**：最新公告、法规更新 | `web_query`（必填）+ `max_results` | 用户开开关 ∧ `web_search.ENABLED` |
| `search_web_exa` | **换一个引擎再搜一次**（Tavily 结果不理想时的备选） | `exa_query`（必填）+ `exa_max_results` | 上一条 ∧ `web_search_exa.ENABLED` |

⚠ 前四个（含 `load_skill`）进**进程级常量** `TOOL_SCHEMAS`，后两个由 `tools_for(enable_web)`
按请求拼出去（理由见「联网搜索」那张两维表）。⚠ `load_skill` **本文件没有专节**，
细节看 `skill_store.py` 的 docstring 与 `src/bidding_qa/skills/`（已知缺口，见 `MCP.md` 附录）。

⚠⚠ **Exa 那一行是三个条件合取**（用户开关 ∧ 两个 Key 都非空），**不是独立开关** ——
用户没开联网开关就把它挂出去 = 未经同意扣费。详见 `联网搜索与Exa.md`。

⚠⚠ **2026-09-20 改过名**（旧名 → 新名）：`search_knowledge_base` → `search_bidding_knowledge`、
`query_knowledge_graph` → `search_knowledge_graph`、`query_procurement_records` →
`search_procurement_records`，`search_web` 不变。**旧名已彻底注销、不留别名** ——
`probe_recover.py` 用例 0b 逐个断言它们不在注册表里。三个旧名现在是**测试夹具里
「看着像却不是」的幻觉名**（见「工具层 → 探针的假名约定」），别当成漏改。

⚠ `TOOL_SCHEMAS` 是**进程级**常量，而 `search_web` 是**每请求**的，所以它**不进去** ——
由 `rag_tools.tools_for(enable_web)` 拼出来（`probe_pg_store.py` 正钉着 `TOOL_SCHEMAS` 的内容，
往里加会让它失败）。`agent.py` 里**三个** `tools=tools` 的调用点（强制图谱轮、正式轮、
收尾轮）与泄漏恢复的 `schemas=tools` 都得用这一份，漏掉正式轮等于联网工具只在强制轮存在。

分工按**数据形状**而非话题（判断标准写在提示词里）：法规看检索，实体与名单看图谱，
带条件的筛选与精确统计看数据库。⚠ **重叠区**：两边都能答「某单位采购过什么」，
但 `contains` 是跨 标题/项目名称/标的物 的**或**匹配，会比图谱的实体匹配多命中 ——
同一个问题两条路给出「120 笔」与「107 笔」两个数字，模型就会把它们加起来或混着说。
因此提示词里明写「**一个问题不要两个工具都调**」，实体/名单类归图谱（那条路有
`is_enum_question` 强制首轮兜底）。

工具**可同时在场，也可只剩检索那个**：`graph_store.ENABLED` / `pg_store.ENABLED` 各自
为假时，对应的工具既不进 `TOOL_SCHEMAS` 也不进提示词（见 `图谱与采购库.md`）；
联网搜索则看**这次请求**的 `web_search`，且它开着的含义比另外两个多一层——**系统会在模型
第一轮之前就自动搜一次**（见下文 `run_agent_stream()` 的事件序列），工具只是让模型能换个
更精确的词再搜。提示词的这几段是**按片段拼装**的
（`rag._agent_system_prompt()` 等），不是 2×2 写四份 ——
新增一个可选工具时加片段即可，但 `probe_enum_route.py` 的 `probe_tool_matrix()`
要同步扩一格（它逐组合验「提示词提到工具 ⟺ 工具列表里有它」）。

新增工具时要改的地方（漏一个的表现都是「模型调了但界面上一片空白」）。
⚠ Pydantic 化之后**已经是四处，不是当初那六处**：原先第 1~3 条（手写 schema dict、
`_HANDLERS` 加处理器、`_peek_display` 加分支）并成了一处 —— **写一个 `BaseTool` 子类**
（`name` / `description` / `args_model` / `schema_required` / `peek_display()` / `run()`）：
schema 由模型生成、处理器由 `_HANDLERS` 从注册表派生、展示由 `peek_display()` 覆盖，
**三处不再各改一遍**（详见 `工具层.md`）：

1. `tools/rag_tools.py` 写 `BaseTool` 子类并 `register()`；**进程级**的进 `TOOL_SCHEMAS`，
   **每请求可变**的由 `tools_for()` 拼出去，别塞进 `TOOL_SCHEMAS`。
   顺手填 `route_label`（一句话短标签，空手而归时给模型看的），有结构化结果的再覆写
   `empty_reason()` —— 见 `工具层.md`
2. `rag.py` 加提示词片段（只动 Agent 专属后缀）
3. `frontend/components/ToolCallBlock.tsx` 的 `TOOLS` 表加展示名与运行中文案
4. 结构化结果若要在前端独立成块：`ToolOutcome` 加字段 → `tool_result` 载荷加 key →
   `lib/types.ts` → `useChat.ts` 的 `tool_result` 分支（含 `newCount` 守卫）→
   `ChatMessage.tsx` 的 `steps.map` 里加一个块

⚠ 参数名**别撞车**：`_PARAM_ALIASES` 在 `_match_schema` 之前生效，而 `_match_schema` 是
「每个参数给每个工具投一票」的简单多数，**0 票或并列即整条丢弃**。所以别用别的工具已占的
参数名（`query`/`top_k`/`query_type`/`entity`），也别用会被改写成 `entity` 的
`keyword`/`keywords`/`entity_name`。`limit`/`count` 这种泛用词是泄漏标记的「票仓」，
也没用（所以行数上限叫 `max_rows`、联网的查询词叫 `web_query`）。详见 `scripts/probe_recover.py`。

⚠⚠ **`_match_schema()` / `recover_tool_calls()` 的 `schemas` 形参别删**：它们的默认值是
进程级的 `TOOL_SCHEMAS`，而每请求工具（`search_web`）**不在里面** —— `run_agent_stream`
必须把本轮的 `tools` 传下去。少了它，模型把 `search_web` 漏进正文时会被判 0 票丢弃
（表现正是「模型说了要联网，结果什么都没查」），**更糟**的是模型若把参数写成 `keyword`，
`_PARAM_ALIASES` 会把它改写成 `entity` → `search_knowledge_graph` 独得 1 票、
**一次联网调用被当成图谱查询执行**。默认值本身不能省：`probe_recover.py` 有 10 处单 kwarg
调用靠它继续通过。

`agent.py` 的 `run_agent_stream()` 事件序列：

```
start → sources → [(reasoning | delta | tool_call | tool_result | sources | answer_reset)*] → done
```

⚠ **开关打开时的预联网把这段序列改动了一点点**（`enable_web` 且 `web_search.ENABLED`，
2026-09-20 起**不再要求预检索为空** —— 本地答得出也照搜）：第一个 `sources` 之后会
**紧接着再发一个 `sources`**（载荷 `registry.dump()`）。顺序不能反，也不能图省事
「等联网完再发第一个」——首屏来源卡片要秒出。
三步的顺序是有讲究的，写反了都是静默故障：

1. `web_lookup(question, registry=registry)` —— 走的就是工具路径那一份实现，
   **登记进 registry**。⚠ 只推给前端不登记的话，模型下一轮调一次检索工具，
   面板里的网页条目会**静默消失**（`sources` 是整体替换、载荷是 `registry.dump()`），
   而正文还在引 `[1][2]`，点角标滚不到任何地方。
2. **重发 `sources` 必须在 `build_agent_messages(..., registry.dump(), ...)` 之前** ——
   否则提示词里根本没有那几条网页资料。
3. **把 `(TOOL_WEB, question.strip())` 种进 `ctx.seen`**（预联网是绕过 `_run_web` 直接调的）
   —— 不种键，模型看到【资料】里已有网页结果仍可能再调一次，同一句话付两次 Tavily 费用、
   白掉 `agent_max_rounds` 里的一轮（⚠ 别在这儿写死数字，它会变）。
   ⚠⚠ **只在 `web_outcome.ok` 为真时种**。`ok=False` 是传输层/凭据层失败（冷却 / 429 /
   401 / 超时）。种了键的话，模型重搜同一句会撞上 `_run_web` 的去重分支，拿到
   「**该调用已执行过，结果与上次完全相同**」—— 而事实是**什么都没查到**。那句话会把
   `_render_result(kind="web")` 特意保留的「未能完成 ≠ 没有新增资料」整条抹平，
   模型据此告诉用户「联网也没有搜到」。不种键反而更好：模型重搜时命中 30 秒冷却，
   **不发请求**就拿到真实失败原因，既不花钱又能自我纠正。`probe_web_search.py` ⑬ 钉着这条。
   （`ok=True` 含「Tavily 正常应答但 0 条」—— 那种情况重搜同一句确实毫无意义，该种。）

⚠ **额度**：本地预检索 + 网页会在**任何工具轮之前**先占掉最多 `len(本地)+5` 个编号，
而 registry 上限是 `AGENT_MAX_SOURCES`（**20**，2026-09-20 从 12 提上来，见下）。
本地顶格 5 条时是 5+5=10，留给工具轮的是 10 个。`registry.add()` 的 `dropped` **会打一行 WARNING**
（`agent.py` / `api.py` 两处 add 都打），要看有没有被丢直接看日志。

⚠⚠ **这两个数（轮数 / 来源上限）是耦合的，改一个要看另一个**。12 那个旧值在「必定联网」
语义下只剩 2 个额度给工具轮，而轮数提到 5 之后几乎每轮都会溢出 —— 于是检索到的资料
**静默**变成「新增 0 条」。20 仍在 `Markdown.tsx` 的 `/\[(\d{1,2})\](?!\()/g` 两位数字范围内，
上下文增量约为几条问答对。真要再往上加，先看 `data/eval/` 的 token 账。

**三条不能破的不变量**：

1. **工具执行永不抛异常**（`execute_tool()`）。工具报错却漏掉 `role:"tool"` 消息，下一轮请求会
   因为 `tool_call_id` 无人匹配直接 400 —— 所有错误都必须转成文本回传给模型。
2. **循环必定收口**。工具轮数用尽却仍在请求工具时，会再跑一轮 `tool_choice="none"` 的强制收尾。
   少了它，任何多轮问题都会以「（未获得回答）」收场。⚠ 这一轮**至多两跑**（收尾轮自己也把调用
   漏进正文时会先恢复执行、再补一轮），见下面「收尾轮也会恢复泄漏标记」。
3. **引用编号全局唯一**（`SourceRegistry`）。`Retriever._to_source()` 的 `index` 是每次查询都从 1
   重新开始的，直接用会和预检索撞车。登记表续编全局序号，且**已见的 doc_id 复用首次那个 Source
   对象**（否则前端「重排位次升降」会错乱）。上限 `AGENT_MAX_SOURCES`（20），因为
   `Markdown.tsx` 的行内引用正则是 `/\[(\d{1,2})\](?!\()/g`，只认 1~2 位数字。
   **图谱结果不进登记表**：`Source` 是问答对形状，图谱是「实体 + 统计」，塞进去语义不对，
   也不该被正文的 `[n]` 引用。

`answer_reset` 的作用：模型调工具前可能已经吐了一句「我先查一下」。这段文字会被前端当正式回答
追加进 `content`，而 `content` 会 (a) 渲染、(b) 存 localStorage、(c) **作为下一轮历史回传** ——
污染是永久的。后端在该轮以工具调用收尾时发这个事件，前端把 `content` 清空。

**`reasoning_content` 回传**：含 `tool_calls` 的 assistant 消息**无条件回传该字段**
（`_assistant_tool_message()`），**空串也要带上**，判据是「字段在不在」而非「内容空不空」。

这条踩过两次，别再踩第三次：

- 曾经按「这一轮出现过 reasoning 才回传」（`if result.reasoning_seen`）来判，**是个真 bug**：
  强制轮关掉了思考（见下文「列举型问句」），`reasoning_seen=False` → 字段被整个丢掉 →
  下一轮开着思考，服务端直接 400 `The reasoning_content in the thinking mode must be
  passed back to the API`。实测表现就是「强制轮查完图谱，然后整条请求炸成 error 事件」。
- **「不回传就 400」这个说法要看情况**，别一概而论：`scripts/probe_tools.py` 测过，**思考模式
  产出**的消息单轮/多轮、空串/None/完全不传**全部通过**；只有上面那种「历史里的工具调用消息
  来自关思考的轮次、而当前轮开着思考」才会被拒。**回传空串即可让下一轮照常开思考**，
  不必为此把整个请求的思考都关掉。

`_stream_round()` 仍有兜底：真被拒时自动去掉该字段重试一次。**改这段前先跑那个脚本。**

**列举型问句：首轮强制调图谱**（2026-09 修）

用户报的 bug：问「采购企业有哪些？」，回答变成一整段盐化工下游行业举例 ——
**全程没调 `search_knowledge_graph`**。

根因不是检索坏了，是**路由坏了**：预检索**必然**返回几篇字面相近、实为答非所问的资料
（「采购企业」是个泛化词），而 `SYSTEM_PROMPT` 第 1 条是「只依据【参考资料】作答」，
`_AGENT_GRAPH_RULES` 排在最后、措辞只是「你还有第二个工具」—— 压不过第 1 条，模型就顺从地
顺着无关资料编。而 `_AGENT_NO_RESULT_WITH_GRAPH` 那段最强的图谱引导语**永远不会出现**，
因为它是 `if sources:` 的 else 分支，检索却是非空的。

⚠ **检索层没有可用的相关性阈值**，别想着在那儿修：实测盐化工那条得 0.8166，在
`MIN_VECTOR_SCORE=0.72` 之上，重排也没把它排下去。所以「资料是否相关」只能交给模型判断 ——
这正是下面 (2) 那几处提示词存在的理由，也解释了为什么单靠调阈值解决不了这个 bug。

三层修复，缺一层都会退化：

1. **`rag.is_enum_question()`** —— 「有哪些采购企业」这类列举句式的识别。
   两个词表各命中一个（`_ENUM_MARKERS` + `_ENUM_CATEGORIES`）且 `_NOT_ENUM_MARKERS`
   全不命中。**误判代价不对称，所以反例词表写得很舍得**：误判成真会让首轮白调一次图谱
   （白掉 `agent_max_rounds` 里的一轮），误判成假只是退回纯提示词路径。
   最要紧的反例是「**供应商有哪些情形不得参加投标？**」—— 同时含类别词和「有哪些」，
   问的却是法规。用例见 `scripts/probe_enum_route.py`。
2. **提示词**（只动 Agent 专属后缀，`SYSTEM_PROMPT` / `build_messages()` 一个字不改）：
   `_AGENT_RELEVANCE_RULES` **排在检索规则之前**，显式声明「它优先于上面的第 1 条」——
   判断标准是「资料是否**真的回答**了问题」而非「资料里有没有出现问题中的词」；
   `_AGENT_GRAPH_RULES` 加列举路由（并警告**别拿「采购人」这种类别词去 `entity_search`**，
   它按名称模糊匹配必然 0 命中）；`_AGENT_SOURCES_REMINDER` 贴在【参考资料】后面，
   让模型在正要读资料的那一刻就看到它。
   **这些文本现在是按片段拼装的**（2026-09 加第三个工具时改的）：原先每处都是
   `ENABLED` 二选一，三个工具会变成 2×2 = 4 份近乎重复的长文本。改成
   `_agent_system_prompt()` / `_no_result_prompt()` / `_sources_reminder()` 三个拼装函数，
   **现有句子的措辞逐句保留**（那段文本是盐化工那次翻车换来的），只是切成可组合的片段。
   ⚠ 两处细节：`_AGENT_DB_CROSS_TOOL_RULE` 只在**图谱与数据库都启用**时才拼进去
   （否则是在让模型用一个没注册的工具）；`_SOURCES_REMINDER` 里那句「改用某工具」的转向指令
   **必须保持工具中立**——原先它写死「按系统提示词的**图谱**规则改用 `search_knowledge_graph`」，
   带筛选条件的问题会因此被送进没有省份/时间/金额维度的图谱，返回 0 行后模型回头顺着无关资料编，
   **盐化工那个 bug 原地复活**。
3. **确定性兜底**：`agent.py` 用 `_FORCE_GRAPH_CHOICE`（常量在 `tools/rag_tools.py`，
   由 `agent.py` 转发进来）强制首轮调用图谱工具，
   `force_graph = graph_store.ENABLED and is_enum_question(question)`。
   **只强制函数、强制不了参数**（JSON Schema 没有这个能力），`query_type` 仍由模型选，
   兜它选错的是提示词路由 + `graph_store` 里 `entity_search` 的类别词纠正 note。

⚠ **强制那一轮必须同时关掉思考**（`disable_thinking=True`）—— 具名 `tool_choice` 与思考模式
**互斥**：带 reasoning 的请求传具名 `tool_choice` 一律 400 `Thinking mode does not support
this tool_choice`（`"required"` 同样不行，只有 `"none"`/`"auto"` 能过）。
关掉的方式是 `extra_body={"thinking": {"type": "disabled"}}`；`enable_thinking=False` **不生效**。
代价只是那一轮没有 `reasoning_content`（前端「深度思考」那一格是空的），后续轮次照常带思考 ——
但这正是上面 `reasoning_content` 必须**无条件**回传的原因，两处是连着的，改一处要看另一处。

`_stream_round_forced_graph()` 自带降级：被拒且**尚未吐出任何内容**时，以 `tool_choice=None`
重跑这一轮，退回纯提示词路径。少了它，服务端哪天改了规则就会是整条请求变成 `error` 事件
（`_stream_round()` 的内部重试只认带「reasoning」字样的报错）。同 `reranker` / `graph_store.warmup()`
的「降级而非报错」。

新增列举类问题（「有哪些 X」）时要动的地方：`graph_store` 加查询类型 → 提示词路由加一条 →
看要不要给 `_CATEGORY_HINTS` 加类别词别名。三处都验：`scripts/probe_graph_store.py` ③b +
`scripts/probe_enum_route.py`，真机再打一次 `/api/agent`。

**轮数预算：3 → 5（2026-09-20）**

⚠ **升级前那版注释写着「加了第三个工具也不用再动这个值」，已被实测推翻**，别再引用它。
拿一个对比类问题真机打 `/api/agent`（「单一来源采购和竞争性谈判有什么区别？」）：

```
第1轮  search_bidding_knowledge  «单一来源采购与竞争性谈判的区别»    ← 整个问题
第2轮  search_bidding_knowledge  «竞争性谈判采购方式适用情形»         ← 自己收窄到半边
第3轮  search_bidding_knowledge  «竞争性谈判»                         ← 比第 2 轮**更宽**
```

两件事同时成立，都得看清楚：

- ✅ **模型会自己分解、自己改写查询词**。「A 和 B 有什么区别」这类问题它自发发了三次检索、
  逐层收窄 —— 所以**不需要**服务端做问题分解。那会重复劳动或与模型打架，而且「这是不是
  对比题」只能靠关键词启发式（本仓库已经有一个 `is_enum_question`，它的反例词表写得那么
  舍得正说明这类判据多脆）。属「服务端替模型做语义判断」，同 `/api/chat` 那三条已删除的
  自动联网触发。
- ❌ **3 轮装不下**。第 3 轮的查询**比第 2 轮还宽**，是预算见底时的抖动 —— 它自始至终没给
  第二个实体单独发过一次干净的查询。对比类问题至少要「实体A → 实体B → 各自补救」四步。
  ⚠ 轮数用尽**不等于数据丢失**（还有 `tool_choice="none"` 的强制收尾轮兜底），
  丢的是**模型自我纠正的机会** —— 所以「反正有收尾轮」不能当作不加轮数的理由。

⚠ **测这类问题前先看语料覆盖**，否则会把知识缺口误读成检索缺陷。那次两边的覆盖差了 8 倍：
「单一来源」74 条里占 **8** 条，「竞争性谈判」只有 **1** 条（还是发改委 16 号令那条顺带提及）。
模型最后如实答了 A 那半、明说 B 那半查不到、并列出该去查什么 —— **那是对的行为，不是失败**。
再多的轮数也变不出语料里没有的东西。

**`agent_max_rounds` 与 `agent_max_sources` 是耦合的**，见「额度」那一节：加轮数会推高
`SourceRegistry` 撞顶概率，而撞顶曾经被读成「什么都没找到」。两个一起动才安全。

**模型把工具标记写进正文**（2026-09 修，**两条链路各有一套处置**，改一处必须同时看另一处）：

DeepSeek v4 偶发把本该走 `delta.tool_calls` 的**特殊 token 当普通文本吐进 `delta.content`**。
实测那次：问「采购企业有哪些？」，模型回了「再为您补充搜索政府机关、学校等其他类型的采购人」，
后面直接跟着一整段工具调用标记——工具名、参数名、枚举值**全是模型自己编的**
（把两个真实工具名揉成一个不存在的名字，参数写成 keyword，枚举写成 entity_search 的反序）。
因为走的是 content，`delta.tool_calls` 是空的，这一轮被当成「最终回答」收尾 ——
于是用户看到一句「我去补充搜索」外加一堆乱码，而**什么都没查**。

⚠ **这个现象与「我们有没有给它工具」无关**：模型是在**幻想**一个工具。下面第二条实证就是
一次 `/api/chat` 上的泄漏——那条链路**一个工具都没注册**，`.env` 里 `TAVILY_API_KEY` 还是空的，
模型照样把一次 `search_web` 调用当正文吐了出来。所以「只有带工具的链路才会被污染」这个
假设是**错的**，两条链路都得设防。

**A. `clients/base_client.py` 的 `_MarkupSuppressor` —— 两条链路共用同一个**

- `stream_with_tools()`（Agent）与 `stream()`（`/api/chat`）**各 new 一个、共用同一个类**。
  流式分片会把标记拦腰截断，**不能逐片做子串匹配**，所以它扣住 32 字符的尾巴再放行。
  注意有两个正则：`_TOOL_MARKUP_RE` 连尖括号和标签名一起吃掉（检测用），
  `_TOOL_MARKUP_TOKEN_RE` 只剥 token、保住标签结构（`strip_tool_markup()` 解析用）。
  **别合并这两个**——合并后解析时标签全没了，切块全部落空。
- ⚠ `stream()` 的契约是产出 `(kind, text)` 元组，所以它多了一**第三种 kind**：
  `("markup_leak", 标记原文)`，**只在拦到标记时出现**，是**给调用方看的内部控制信号、绝不能
  转发给前端**（`api.py` 拿它判断「这是不是一次联网请求」）。没拦到时一个新分片都不多发，
  冻结路径逐片不变（只因扣尾巴而滞后 32 字符，`flush()` 补齐）。
- ⚠⚠ **`api.py` 里 kind→SSE 的映射有个 `else` 陷阱**：写成
  `{"type": "reasoning" if kind == "reasoning" else "delta", …}` 的话，新增的 `markup_leak`
  会被 `else` 分支当 `delta` **原样发给前端** —— 修 bug 反而亲手把标记送出去。
  所以那段映射**收进了一个嵌套生成器 `_run_pass()`，且只此一处**（调用点别再写一遍）。

**B. `tools/base.py` 的 `recover_tool_calls()`（由 `agent.py` 调用）—— 把拦下来的标记还原成真调用去执行**

- **归属判定：名字精确优先，否则只认结构**（2026-09 改，**原先一律只认结构**）。
  `_schema_by_name(name, pool)` 只在**本轮工具表**里查名（见下 ⚠），命中就先拿那个 schema
  归一化；失败（必填缺失 / 枚举对不上 / 别名落空）**再退回按参数投票**，投票那段逻辑一字未动。
  名字对不上（模型把两个真名揉成一个不存在的名字）时行为与改动前**逐字相同**。
  改这一条的直接动机见下面那条线上 bug：只认参数集合会把一次**联网**判成检索。
- **还要求「至少有一个参数真的落在它身上」**，否则同样退回投票。少了这条会开一个静默错答案
  的口子：`search_procurement_records` **没有必填参数**，模型若把 `query` 这种参数写在它名下，
  归一化会「成功」返回一个**空 args**，然后执行一次没有任何筛选条件的查询（全表）。名字对、
  参数一个都不沾 ⇒ 名字多半是幻觉，别信它。
- **名字限定的别名表** —— 挂在**工具类**上的 `name_scoped_aliases`（`_bind_params()` 经
  `_REGISTRY.get(name)` 取），只在名字精确命中时才叠加上去。**不能并进全局 `_PARAM_ALIASES`**：
  `query` 已被 `search_bidding_knowledge` 占了，全局改写成 `web_query` 会让一次**检索**的泄漏
  从「稳赢」变成打平（0:0）→ 整条丢弃。⚠ 也**别顺手加 `keyword`**：`probe_web_search.py` ⑧
  有断言钉着「`search_web` + `keyword` 必须整条丢弃」。
  ⚠ 它**长在工具类上而不是 `base.py` 里的一张模块级 dict**（2026-09-30 改）：原先用裸工具名
  字面量做键，改名时那张表会**静默失效**（别名落空 → 一次联网泄漏被投票判给检索工具），
  而「改名顺手改这里」没有任何提示。挂到类上就跟着名字一起走。
  ⚠⚠ **现在两张表各有两条、且互为镜像**（2026-09-30）：
  `WebSearchTool = {"query": "web_query", "exa_query": "web_query"}`、
  `ExaSearchTool = {"query": "exa_query", "web_query": "exa_query"}`。
  后一条是**跨引擎串台**的解药：两个引擎的参数名各成一派，模型写对工具名、却写另一个引擎的
  参数名时，名字那条路会**因为必需参数缺失而归一化失败、退回投票**，而投票只看参数名 ⇒
  **调用被拐到另一个引擎上执行**（实测：`search_web_exa` + `web_query` → 跑的是 Tavily）。
  `probe_web_search.py` ⑧ 双向各钉一条 —— 那边特意验过「去掉这两条就当场变红」。
- 参数名走一张很短的别名表，枚举值先精确匹配再按「下划线切词后排序」比对
  （`entity_search` ↔ 反序写法），**对不上就整条丢弃，绝不猜**。
  恢复失败必须退化成「没有工具调用」，由原有收尾逻辑兜底。
- ⚠⚠ **`schemas` 形参别删**（默认值是进程级的 `TOOL_SCHEMAS`，不含按请求挂的 `search_web`）：
  **生产路径上现在只有 `run_agent_stream` 一处**要传（原先还有 `api._recover_web_query`，
  2026-09-20 随 `/api/chat` 的兜底触发一起删了）。见「联网搜索」。

沿用 `answer_reset` 把模型那句「我先补充搜索」作废——恢复出调用后走的还是正常的工具轮，
前导正文照样被清掉，最终回答是模型拿到图谱结果后重新生成的。

`agent_max_rounds` 为这条路径留了余量：恢复出来的调用**照常占用轮数**，不额外放宽。

**C. `/api/chat` 上这套机制已经删掉了（2026-09-20）**

`/api/chat` 是写死的流水线，**没有工具循环可恢复**。它原先另有一套「从标记里把查询词捞回来、
真去 Tavily 搜一次、再重答一遍」的专用恢复路径（`api._recover_web_query()`）。**联网语义从
「答不出才搜」改成「开关打开必定搜」之后，它整块不可达了**（联网发生在任何正文流出之前），
已删除。`api.py` 里那段 tombstone 注释记着删了什么、以及为什么。

那条路径留下三条仍然成立的结论，抄在这里免得日后有人想「补回来」：

1. **判据必须只针对联网那一条 schema**（`tools_for(True)` 里筛出 `TOOL_WEB` 再传给
   `recover_tool_calls`）。泄漏的是图谱/数据库形态的标记时**不该联网**——`/api/chat` 压根
   没注册那些工具，模型是糊涂了，不该顺势花一次 Tavily。
2. **编号必须续接**：本地**有**结果时，直接沿用不传 registry 的那条路（`to_sources()` 从 1
   开始编号）会与本地条目撞号。必须建 `SourceRegistry(cap=settings.agent_max_sources)`，
   先 `add(本地 sources)` 占住 1..K，再 `web_lookup(..., registry=reg)`。
   ⚠ 这条现在是 `/api/chat` 的**唯一**写法——`registry=None` 分支已经到不了。
3. **`sources` 重发必须带完整 `latency_ms`**（既有约定：`sources` 是整体替换）。

⚠ **残留边界（已知，未修）**：`/api/chat` 上抑制器拦到标记时**只记一行 WARNING、不再重答**
（`_run_pass()` 里那个 `continue`）。`_MarkupSuppressor` 的语义是「一旦命中，本轮后续
content 全部丢弃」，所以模型若先吐一句「让我联网搜索」再吐标记，用户看到的正文**就到此为止**。
改动前这条链路上有触发 ② 兜底重答，现在没有了。
判断是**可以接受**的：开关开着时网页结果**已经在生成之前的【资料】里**，模型没有理由再发起
一次搜索；而开关关着时**改动前本来就是这个行为**（触发 ② 也要求 `payload.web_search`），
所以这不是新引入的形态，只是从「开着才有」扩到了全部。真要在意，最省的做法是
`markup_leak` 时发一次 `answer_reset` + 重跑一遍 `_run_pass()`（**不额外联网**）——
但那会让 `answer_reset` 重新出现在 `/api/chat` 上，与 `types.ts` 的「仅 /api/agent 产生」
以及删掉它时写的那些注释都冲突。先按现状记着。

**改这段前先跑** `scripts/probe_markup.py`（抑制器，含分片切断与「不能吞正文」的负例；
⑩ 起还覆盖 `stream()` 的接线——含「无标记时逐片相同」与「标记只在 reasoning 里不触发」）
与 `scripts/probe_recover.py`（恢复逻辑，第一个用例就是 2026-09 那次真实泄漏的原文，
其余全是「必须丢弃」的负例）；⚠ 这个脚本**头两组是用例 0 / 0b**：0 钉 schema 形状
（每个整数字段清洗后顶层必须有 `type:"integer"`、枚举字段必须有 `enum`、全表无 `title`、
`required` 逐字段不变），0b 钉「投票路径的夹具确实没注册」——**改 `base.py` 的清洗层
或给任何字段加 `Optional` 之前先看它们**（漏了是静默降级，见「工具层」那条）；`scripts/probe_web_search.py` ⑧ 钉着联网那几条归属断言、
⑫ 端到端跑一遍 `/api/chat` 的**完整 SSE 事件序列**（全部替身，不需要任何服务）；
Agent 侧端到端是 `scripts/probe_recover_e2e.py`：**用例 A** 复刻线上那次普通轮泄漏（需 Neo4j），
**用例 B** 复刻收尾轮泄漏 + 恢复（**不需要任何服务**）。⚠ 它消费事件时**模拟了 `useChat.ts`
的 delta 追加 / `answer_reset` 清空语义**——直接拼接 delta 会把已被作废的前导正文也算进来，
那条断言就永远过不了（测的也就不是用户真正看到的东西）。

**收尾轮也会恢复泄漏标记**（2026-09-30 修，`agent.py` 的收口块）

⚠⚠ 修之前收尾轮**从不看 `final.leaked_markup`**，而 `_MarkupSuppressor` 一旦命中就把本轮
content 全部丢掉（只留标记**之前**那一截）⇒ 模型在收尾轮又把调用漏进正文时 `final.content`
是空的，直接掉进 `fallback_answer()`：用户拿到的是一份**来自知识库、却回答着另一个问题**的
原文，读起来完全像一次正常回答。真机 2026-09-30 就是这么发生的（日志里「工具轮数用尽仍无回答」
之后紧跟着「模型把工具调用标记写进了正文…共 0 个」），**那个问题的答案一个字都没答上来**。

处置与普通轮**同构**，但有界：`for attempt in (1, 2)` —— 第 1 轮收尾检出 `leaked_markup` 就走
`recover_tool_calls(..., prefix="final", schemas=tools)` 把调用捞回来执行（含 `answer_reset`
作废那句「我再查一下」），然后至多再跑一轮收尾；第 2 轮无论拿到什么都不再恢复。

- ⚠⚠ **绝不能改成 `while`**：收尾轮是本链路唯一的收口保证 —— `agent_max_rounds` 用尽后必须
  **必定**落到一句回答或降级文案。恢复不出东西（0 票/并列）时直接收口，照 `recover_tool_calls`
  那条「对不上就整条丢弃，绝不猜」的纪律，不在这里补第二套判据。
- ⚠ **两轮的 `call_id_prefix` 必须不同**（`final1` / `final2`）：`_stream_round` 撞上 reasoning 400
  时用**同一个 prefix** 重试，而 `eval/agent_eval.py` 的 `_tool_calls_of()` 是按 prefix 去重的
  （保留最后一次）——两轮共用会让恢复出来的那次与第二轮的记录互相覆盖。
- ⚠ **恢复之后必须追加 assistant 消息**（`_assistant_tool_message`，无条件带 reasoning_content）：
  那些 tool_calls 会被**真的执行**，不追加的话对应的 `role:"tool"` 消息无人认领，下一轮请求直接
  400（不变量 ①）。首轮收尾**故意不追加**的老规矩没变（那是为了「模型仍返回 tool_calls 时没有
  悬空的 tool_call_id」），所以那句注释要按「首轮」读，别当成整块。
- ⚠ **工具执行体抽成了 `_emit_calls()`**（一个嵌套异步生成器），普通工具轮与收尾轮的恢复共用
  同一份。两处各写一份的话，`assess_outcome` 的追加语义 / `latency["tool_ms"]` 的记账 /
  pydantic 结果的 `.model_dump()` 会慢慢漂移，而漂移的表现是「某个块渲染成空白」——
  看不出少的是哪一步。

⚠ **用例 B 有两种形状，都必须钉**：模型在漏标记之前**可能已经吐过一句**「我再查一下」，也可能
一个字都没吐。修好之前它们落进**不同的**坏分支 —— 有前导正文时那句声明被当成正式回答端出去；
没有时掉进 `fallback_answer()`。所以 `case_b` 由 `announce` 参数跑两遍。⚠ 那条假 LLM 的断言
写在**生产路径上**（`_stream_round` 直接调它），比事后从事件序列反推更贴近「下一轮模型到底
看到了什么」。

