# 自建 MCP 服务器

> `CLAUDE.md` 拆出来的深潜正文（原「自建 MCP 服务器」一节）。
> 改 `mcp_server.py` / `mcp/mcp_bridge.py` 之前，先把这里读完。

## 自建 MCP 服务器（`mcp_server.py` + `mcp/mcp_bridge.py`，2026-09-30 加）

**它是什么**：把三个工具**搬进一个独立进程**，后端通过 MCP 协议（streamable HTTP）当
**调用方**去调它们。

```
src/bidding_qa/mcp_server.py      服务端半 —— python -m bidding_qa.mcp_server
src/bidding_qa/mcp/mcp_bridge.py  客户端半 —— 三个 run() 经它把调用发过去
```

⚠⚠ **与 Exa 那个 MCP 是两回事，只是共用 SDK**。Exa 是我们当**客户端**连别人的托管端点
（`mcp_client.py`）；这一节说的是**我们自己的服务器**，而且后端是它的**第一个调用方**。
`mcp_client.py` 的「本项目只有一个 MCP 服务」那句已改成「只管 Exa 那个托管端点」，
**代码零改动**。

### 为什么是这三个工具

| 工具 | 进服务器？ | 为什么 |
| --- | --- | --- |
| `search_bidding_knowledge` | ❌ | 持有 retriever（Qdrant 排他锁），且与 `SourceRegistry` 全局编号长在一起 |
| `search_web` / `search_web_exa` | ❌ | **每请求**挂的（跟着用户开关走），进程级常量装不下 |
| `search_knowledge_graph` / `search_procurement_records` / `load_skill` | ✅ | 三个都不碰 Qdrant ⇒ **能与后端并存**，同一台服务器还能被 Claude Desktop / Cursor 直接连 |

⚠ `MCP.md` §3 论证过「把现有五个工具换成 MCP 是纯成本」—— **那条仍然成立**，它说的是
检索工具（与全局编号、`recover_tool_calls()` 泄漏恢复、按工具表拼装的提示词长在一起）。
本次动的是它点名的**例外**，不是推翻它。

### 开关与降级：留空是**默认且健康**的状态

`settings.mcp_server_url` 为空 ⇒ 三个工具**在本进程内执行**（改动前的老行为，逐字节相同）；
非空 ⇒ 走 MCP。⚠ 本条决定了它**不能**进 `_OPTIONAL_CAPS`：那张表的语义是「字段非空即为开」，
加进去会让**每台正常机器**都吐一行「未启用的可选能力」，而体检的规矩是配置齐备时一行不输出。

⚠ 与图谱/采购库的「连不上只影响那个工具」同一条纪律：**服务器没起时后端照常问答**，
只是那三个工具如实报失败（文案带端点串）。

### 路由分支在三个 `run()` 里，**不在 `execute_tool` 上**

⚠⚠ 别把分支提到 `execute_tool`（看着更省事、一处顶三处）。`execute_tool` 在
`tool.run()` **之前**就 `parse_args` 并往 `ctx.seen` 写键，提前拦截会让：
模型把枚举写错（`query_type="entity"`）拿到报错、改成正确值重试时撞上 `duplicate_query`
（「该调用已执行过，结果与上次完全相同」），而**正确行为**是拿到
`错误：没有名为 X 的查询类型。可用类型：…` —— `skill_store.py` 的 docstring 称后者是
「最重要的信息」。所以分支在**校验 + 去重之后**、本地调用之前，顺序由代码结构保证：

```python
    # 校验与去重那几行一个字不改……
    if mcp_bridge.is_enabled():
        try:
            payload = await mcp_bridge.call(self.name, {...})
        except mcp_bridge.BridgeError as exc:
            return ToolOutcome(f"图谱查询失败（{exc}）。…", ok=False, error=str(exc))
        return outcome_from_payload(payload)

    started = time.perf_counter()   # ↓ 以下原样，一个字不改
```

四条配套约定：

1. ⚠⚠ **失败文案与进程内那条逐字相同** —— 模型看到的失败形态不该随部署形态变，它的正确反应
   （如实说查不到，别编）两种情况下也一样。
2. ⚠ **传过去的是本地已归一化的那份参数**（图谱 `{query_type, entity}`、采购库是
   `max_rows` **夹紧之后**的 `args`、技能是 `{"skill": name}`）。服务器会再跑一遍同样的
   `parse_args` + `run()`，两次夹紧幂等；**传原始 args 会让两条路径对同一个请求给出不同的行数**。
3. ⚠ **`elapsed_ms` 取服务器侧那份**（DB 自己的时钟），传输耗时只记 `logger.debug` ——
   折进去会让同一个查询在两条路径上报出不同的数，而报告里那两行是要能互相印证的。
   实测：同一次图谱调用，本地包装 311.73ms / 服务器 306.33ms，**两个数分得开**。
4. ⚠ `ctx.seen` **仍由客户端持有**（服务器侧 ctx 每次调用新建，天然不去重）⇒ 重复调用照样
   判「已执行过」，第二次不打远端。

### 服务器侧四条硬约束（改 `mcp_server.py` 前先读）

1. ⚠⚠ **白名单查在 `execute_tool` 之前**。服务器一 import `rag_tools`，`_REGISTRY` 里就躺着
   **六个**工具 —— 客户端传 `search_bidding_knowledge`，`execute_tool` 会高高兴兴执行它，
   而 `ctx.retriever=None` ⇒ `AttributeError`（或更糟：静默空结果）。不在白名单就直接回
   `错误：本服务器没有名为 X 的工具。可用工具：…`，并置 `ctx.tool_names = _EXPOSED`，
   让「可用工具」列出**正确的三个**。
2. **`ctx.retriever = None`**：`ToolContext.retriever` 是必填字段（只有检索工具读它）。
   白名单保证它从没被读过 —— 别以为能 `ToolContext()` 空构造。
3. **handler 回 2-tuple `(中文正文, payload_dict)`**：SDK 对长度 2 的 tuple 走
   `CombinationContent` 分支 ⇒ `content=[正文]`、`structuredContent=载荷`。只回 dict 的话
   SDK 会把载荷**美化打印成 JSON 塞进 content**，第三方客户端看到一屏 JSON。
4. **启动时 `warmup()` 一次 Neo4j / PostgreSQL**（各自吞异常，同 `api._bootstrap` 那三个内层
   try），打印监听地址；`--host` 不是回环时**多打一行 WARNING**（本服务**无鉴权**，
   `API_HOST=0.0.0.0` 那个既有教训不许在这条新端点上重演）。

⚠⚠ **两条 SDK 细节，改了都是静默故障**：

- **`@server.call_tool(validate_input=False)`** —— 默认 `True` 会把中文报错换成
  `Input validation error: 'e_related' is not one of [...]`，模型的自我纠正线索没了。
- **不设 `outputSchema`** —— SDK 在「声明了却没返回」时**抛错**，而我们没有 `ToolOutcome`
  的 schema 可填。
- ⚠ **用 lowlevel `Server`，不用 `FastMCP`**：只有前者能逐字交出我们自己的 `inputSchema`；
  FastMCP 会从 Pydantic 走 `model_json_schema(by_alias=True)` 重新推导，**毁掉**
  `Any` + `json_schema_extra` 那套（容错夹紧 + 中文枚举报错）。
- ⚠ 也**不传 `security_settings`**：不传时 DNS-rebinding 防护默认**关闭**。别「顺手加固」成
  `allowed_hosts=["127.0.0.1:*"]` —— Host 是**精确匹配**，客户端连 `localhost:8765` 时
  Host 头就是 `localhost:8765`，一次不匹配就 421，且只在真机打得出来。

### `mcp_bridge.py` 与 `mcp_client.py` 的分工（**别合并**）

`mcp_client` 的 docstring 明写「刻意做小」并**点名拒绝**了工具发现（`list_tools`）与
`structuredContent` —— 那两样恰好是本模块要的（我们自己的端点，契约由我们定）。
硬塞进去会让那边那段**推理**（不只是那句清单）变成假话。代价是约 25 行连接样板重复，
换 Exa 那条**本机无法端到端验证**的路径零风险 —— 划算。

四条设计决定（改之前先读模块 docstring）：

1. ⚠⚠ **失败一律抛 `BridgeError`，不返回结果对象。** 与 `mcp_client` 的「永不抛、回
   `McpResult`」**有意相反**：三个 `run()` **本来就有** `except Exception` 兜底并产出各自那句
   已标定的中文失败文案，抛进去是**零新增错误处理代码**；回结果对象意味着三个 `run()` 各多一个
   `if` 分支 —— 那才是新增的、会漏改的东西。`CancelledError` 原样穿透（用户点停止那条路）。
2. ⚠⚠ **冷却只由传输层失败触发，业务失败绝不冷却。** 判据取**有没有拿到 `structuredContent`**
   这个结构，不取 `ok` 的值、也不解析错误文案（话术判据）。若把 `ok=False` 也算失败，
   模型连查两次空结果就把工具**锁死 30 秒**，而界面上完全看不出来。
3. **`BridgeError` 的文案必须带端点串**。它是「工具突然全挂」时**唯一**的分诊线索，而这句话会
   原样进 `role:"tool"` 消息、进模型上下文、进前端的工具调用块 —— 三处都看得到。
   ⚠ `_one_line()` 必须**钻进 `ExceptionGroup` 取真正那条**：anyio 的 `str()` 是
   「unhandled errors in a TaskGroup (1 sub-exception)」，**指向不到任何东西**。
   （⚠ 已知局限：`_leaf()` 只走 `BaseExceptionGroup.exceptions`、不跟 `__cause__`，
   所以那条退化成外层文案的情况仍可能出现。）
4. **两次调用现连现断**，不维护长会话。streamable HTTP 是**有状态**的
   （`Mcp-Session-Id`），长会话要么接进 lifespan、要么写一个「断了就重连」的状态机，
   而那个状态机的失败形态是**整条问答挂住**；现连现断的代价只是每轮多一次握手。

⚠⚠ **`_http_client_factory` 里的 `trust_env=False` 不能删**：这台机器的 Windows 系统代理
（`http://127.0.0.1:57777`）会把**连 127.0.0.1 的请求也绕去代理**，死端口因此回 **502**
（一个正常 HTTP 响应），看起来像「服务器起来了但握手不对」，指向不到真正的原因。
⚠ 那是本仓库**唯一**一条「HTTP 打本机」的路径（Neo4j 走 bolt、PG 走 psycopg，都不理会代理）。
⚠ **别把这条搬到 `mcp_client.py`** —— 那条路连 `mcp.exa.ai`，在这台机器上正是靠这个代理出网的。

### ⚠⚠ 不要 import `..tools`（成环）

`tools/__init__.py` 会把 `rag_tools` 拉进来，而 `rag_tools` 要 import `mcp_bridge` ——
反过来立刻成环，故障形态是**入口相关**的静默故障（服务端起来没事、某个脚本一跑就炸）。
所以桥接层只回**裸 dict**，`outcome_from_payload()` 那一步在 `tools/rag_tools.py` 里做
（codec 在 `tools/base.py`，与 `ToolOutcome` 定义挨着）。
⚠ `outcome_to_payload()` 的字段集必须与 `ToolOutcome` **逐字段相等** ——
`probe_mcp_server.py` ② 有一条 `set(payload) == {f.name for f in fields(ToolOutcome)}` 的
**字段集断言**：日后往 `ToolOutcome` 加字段而忘了 codec，红在这里，
而不是红在「前端图谱块渲染成空白」。
⚠ `new_sources` / `new_indices` / `known_indices` 对这三个工具**恒为空**，但照常进 payload
（这样字段集断言能写成 `==`）。⚠ 将来若有经 MCP 路由的工具真填了这三个字段，
**不能**并进本地的 `SourceRegistry` —— 来源编号是**客户端本地**的。

### 端到端实测（2026-09-30，后端 8123 + 服务器 8765）

| 步骤 | 结果 |
| --- | --- |
| 图谱题（「有哪些采购企业？」⇒ 强制首轮图谱） | `tool_result ok=true`、`graph` 载荷真实（total=500 撞顶、20 行、`某中心小学 71 笔`），服务器侧 `elapsed_ms=306.33` |
| 采购库题（「按采购类别分组统计」） | `ok=true`、`pg` 载荷真实（15 个类别、8751 条、金额合计 218.57 亿） |
| **杀掉服务器**再问同一句 | 第 1 轮 `ok=false`、`error` 带端点串；第 2 轮撞 **30 秒冷却**（`elapsed_ms=0.0`，**一次连接都没发**）；模型如实答「知识图谱和采购记录数据库本次均无法连接」、**没有编** |
| 重启服务器 | 冷却到期后**自动恢复**，`ok=true` 照常 |
| 后端日志 | 整场只打了**一行** `MCP 调用 … 失败`（冷却那发刻意不打日志） |

⚠ 那条反证是**必需的**：没有它，「经 MCP 执行」与「悄悄走回进程内」在报告上**完全一样**。

### 探针

```bash
uv run python scripts/probe_mcp_server.py --offline   # ①②③④⑥关，不需要任何服务/Key
uv run python scripts/probe_mcp_server.py             # 再加 ⑤ 与 ⑥开：起一个服务器子进程
uv run python -m bidding_qa.mcp_server                # 手动起服务器（默认 127.0.0.1:8765/mcp）
MCP_SERVER_URL=http://127.0.0.1:8765/mcp uv run python scripts/probe_skills.py   # 应仍 92/92
```

⚠⚠ ⑥ 组（路由）是本次新增的、**最值钱**的一组：它用替身证明「URL 留空时本地存储层被调到
**恰好一次**、桥零次」与「URL 非空时本地存储层**一次都没被调到**」。之所以用替身而不是断言
数据，是因为**「路由坏了却悄悄走回进程内」的结果与正常完全一样** —— 只有「谁被调用了」能把
两者分开。⚠ 验过它是**承重**的：把三处 `if mcp_bridge.is_enabled():` 换成 `if False:`，
**恰好且只有** ⑥ 开那三条变红（110 通过 / 3 失败）。

⚠ `probe_skills.py` 的 `main()` **开场就把 `settings.mcp_server_url` 强制置空**（`finally`
还原）：它的 docstring 承诺「全离线」，而 ④ 组会 `execute_tool(..., name=TOOL_SKILL)` ——
那是三个带路由分支的工具之一。不置空的话，环境里配了 `MCP_SERVER_URL` 这句承诺就是假的，
而**输出上完全看不出来**（连不上就如实报失败，看着还挺对）。
⚠ 其余真打 Neo4j/PG 的探针（`probe_graph_tool.py` / `probe_pg_tool.py` /
`probe_recover_e2e.py` / `eval/agent_eval.py`）**不改**：`MCP_SERVER_URL` 非空时它们确实
**该**走 MCP（那就是当时的运行配置），服务器没起时如实报「不可达」是**对的**行为。

### 明确不做（scope 边界）

- **`agent.py` / `api.py` / `rag.py` / `frontend/` 零改动** —— 工具名与 SSE 契约不变，
  `ToolCallBlock.TOOLS` 早就覆盖这三个名字。
- **不加 `/api/health` 字段**（同 Exa 那条「有意留白」）：health 的字段要有**前端消费者**，
  而前端不需要知道工具跑在哪。
- **不做 `--stdio`**：本机没有 Claude Desktop，那条路**无法验证** ——
  不重复 Exa「端到端一次都没打过」那个缺口。`build_server()` 已经把它变成十行的事。
- ⚠ **服务器与后端必须用同一份 `.env`**（`env_file` 是绝对路径，所以与 CWD 无关）。
  两边配得不一样时**工具表会对不上**，而表现是「某个工具在一边『不存在』」——
  ③ 的 `inputSchema` 漂移比对能抓到 schema 变，**抓不到函数体变**（已知局限）。

