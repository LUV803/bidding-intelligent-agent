# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

这份文件是常驻说明书：地图、契约、常用命令，以及每条子系统的「开关 / 最危险的那条不变量 / 该跑哪个探针」。各子系统的**深潜正文在 `docs/内部/` 下，不进上下文** ——
⚠ 改任何子系统之前，先读「改动索引」里指定的那一份**全文**，哪怕只改一行。那不是补充材料，是这份文件被拆开的下半部分。

## 项目概览

招投标领域 RAG 问答系统：Qdrant 向量检索 + BM25 关键词检索双路召回，RRF 融合（可选的 CrossEncoder 重排）后交 DeepSeek 生成带来源引用的回答。两个独立进程：

- **后端** `src/bidding_qa/`（Python 3.12 + FastAPI，端口 8000）
- **前端** `frontend/`（Next.js 14 App Router + Tailwind，端口 3000）

后端有**两条并存的链路**，但**前端只走其中一条**：`/api/agent` 现在是唯一入口 ——
2026-09-20 按要求删掉了「直接 RAG / Agent 模式」切换按钮，**Agent 模式成了默认且唯一的选择**
（端点硬编码在 `lib/api.ts` 的 `streamChat()`；`agent` 状态、`SendOverrides.agent`、
localStorage 键三样一起删了）。它把**检索**、**知识图谱查询**、**采购记录库查询**、
**联网搜索**挂成工具供模型自主调用（见 `docs/内部/Agent模式.md`；工具定义现在独立成
`src/bidding_qa/tools/` 包，见 `docs/内部/工具层.md`）。

⚠⚠ **`/api/chat` 没有删**（写死的流水线：无条件检索→交给模型作答）。后端两个端点都在，
探针也仍钉着它的 SSE 契约，只是 `frontend/` 里再也打不到它 —— **改 Agent 代码时不要顺手删
`api.py` 里那条链路**。旧文档写过「答辩主演示走 `/api/chat`」，那句**已经过期**，
别再按它理解代码或安排演示。

知识图谱、采购记录库、联网搜索、Exa 备用引擎都是**可选能力**：连不上/没配 Key 时
`search_bidding_knowledge` 与整条问答链路完全不受影响，只是对应的工具如实报告失败
（见 `docs/内部/图谱与采购库.md`、`docs/内部/联网搜索与Exa.md`）。它们各算各的。
⚠ 但它们的开关**不在同一个维度上**：图谱与采购库是**进程级**常量（`ENABLED`，导入期按
`.env` 算死），联网搜索是**每个请求**由 `ChatRequest.web_search` 决定 —— 后者是第一个
「每请求可变」的可选能力，两维不可合并，详见 `docs/内部/联网搜索与Exa.md`。⚠ Exa 两者都沾一点：
它既要用户这次开着联网开关、又要 `TAVILY_API_KEY` 与 `EXA_API_KEY` 都非空
（**三个条件同时为真**），是唯一一个「挂不挂出去」由合取决定的工具。

⚠ **用哪家文本大模型是第二个「每请求可变」的维度**（2026-09-21 加）：前端工具条上那个循环
胶囊 → `ChatRequest.llm_provider`，不传/空串就跟 `settings.llm_provider` 这个**默认值**
（`.env` 的 `LLM_PROVIDER` 从「唯一开关」降级成了默认）。⚠ 与联网那对**形状不同**：
联网是两维**取交集**（没配 Key 时开关打开也不搜），供应商是**一维覆盖** —— 名字认得出来就照办，
**没配 Key 就如实报没配、绝不悄悄换成另一家**。⚠ **加第三家供应商要动三处**（它自己的
`_NOT_CONFIGURED`、`_PROVIDERS`/`_LABELS`、`probe_llm_provider.py` 的 `_KEY_FIELD` 映射），
漏一条全是静默。详见 `docs/内部/客户端层与图片解析.md`。

⚠ **联网搜索的语义是「开关打开 ⇒ 每次提问都联网」，不是「本地答不出才联网」**
（2026-09-20 改）。两条链路都一样：先本地检索、再拿**原问题**搜一次 Tavily，网页结果
带同一套 `[n]` 编号并入「参考来源」，两份一起交给模型作答。代价是每次提问 1 个 credit、
首个字晚 1~3 秒；开关默认关着。Agent 模式下还会额外挂出 `search_web` 工具，让模型能换个
更精确的词再搜（自动那次用原问题，往往不够精确）。⚠ 判据是**四条合取**（用户开关 ∧
`use_llm` ∧ `llm.ready` ∧ `web_search.ENABLED`），**别提前算** —— 后两条是「搜回来也没人读」的守卫。

⚠ **Exa 是 Tavily 的备选引擎**（2026-09-30 加）：`search_web` 返回 0 条或明显答非所问时，
模型可以换个引擎、用**同一个查询词**再搜一次，结果是另一批网页，同样带 `[n]` 并进同一个
来源面板。它不是独立的开关 —— 跟着用户那个「联网搜索」开关走，且两个 Key 都得配。
详见 `docs/内部/联网搜索与Exa.md`。

提问窗口还支持**上传图片**：后端调智谱 `glm-4.6v-flashx` 把图解析成结构化字段，结果既展示、
也自动带进后续提问（见 `docs/内部/客户端层与图片解析.md`）。⚠ **它不是一个 Agent 工具**，
解析发生在上传那一刻、早于提问。⚠⚠ **唯一的硬不变量：图片内容只进提示词、绝不进检索** ——
拼进 `question` 会让检索拿整份公告去查、向量被稀释，**极可能直接跌破闸门返回 0 条**。
同样是**可选能力**：没配 `ZHIPU_API_KEY` 时只是附件按钮如实报错，问答照常。

⚠ 还有**第三个维度，但它不是「可选能力」而是「部署形态」**（2026-09-30 加）：
图谱 / 采购记录 / 技能三个工具**可以搬进一个独立进程**（`python -m bidding_qa.mcp_server`），
后端经 MCP 协议当调用方去调它们。开关是 `MCP_SERVER_URL`，**留空 = 跑在进程内
（默认且健康，与加这个功能之前逐字节相同）**。工具名、schema、SSE 契约、前端一个字节不变，
所以它与上面那四个能力**各算各的**，见 `docs/内部/MCP服务器.md`。

## 改动索引

「改哪块 → 先读哪份 → 跑什么」。⚠ **先读的那份不是补充材料**，是本文档被拆开的下半部分；只读核心就动手，丢掉的是那一节全部的踩坑记录。

| 你要改的东西 | 先读 | 跑什么 |
| --- | --- | --- |
| `graph_store.py` / 任何 Cypher / 查询类型 | `docs/内部/图谱与采购库.md` | `probe_graph_store.py` |
| `pg_store.py` / 任何 SQL | `docs/内部/图谱与采购库.md` | `probe_pg_store.py` |
| `src/database/to_postgresql.py` | `docs/内部/图谱与采购库.md` | `--dry-run` |
| `rag.is_enum_question()` / `rag.py` 的 Agent 提示词片段 | `docs/内部/Agent模式.md` | `probe_enum_route.py` |
| `agent.py` 的 `run_agent_stream` | `docs/内部/Agent模式.md` | `eval/agent_eval.py --selftest` |
| `clients/base_client.py` 的抑制器 / `stream()` 接线 | `docs/内部/Agent模式.md` | `probe_markup.py` |
| `tools/`（新增工具 / schema / 清洗层 / 泄漏恢复） | `docs/内部/工具层.md` | `probe_recover.py` + `probe_quality.py` |
| `clients/llm_factory.py` / `ChatRequest` / `HealthResponse` | `docs/内部/客户端层与图片解析.md` | `probe_llm_provider.py` |
| `clients/vision_client.py` / `rag.with_image_context` | `docs/内部/客户端层与图片解析.md` | `probe_vision.py` |
| `web_search.py` / `rag_tools.tools_for` / `rag` 的网页片段 | `docs/内部/联网搜索与Exa.md` | `probe_web_search.py` |
| `mcp/mcp_client.py` / `mcp/web_search_exa.py` | `docs/内部/联网搜索与Exa.md` | `probe_web_search.py` |
| `mcp_server.py` / `mcp/mcp_bridge.py` | `docs/内部/MCP服务器.md` | `probe_mcp_server.py` |
| `conv_store.py` / 任何 SQL | `docs/内部/对话历史与反馈.md` | `probe_conv_store.py` |
| `conv_store` 的 feedback 三函数 / 三条反馈路由 | `docs/内部/对话历史与反馈.md` | `probe_feedback.py` |
| `conv_store.usage_stats()` | `docs/内部/对话历史与反馈.md` + `docs/内部/评估体系.md` | `probe_conv_store.py` + `eval/dashboard.py --selftest` |
| `api.py` 的 Limiter 三行 / `rate_limit_per_minute` | `docs/内部/限流与配置体检.md` | `probe_rate_limit.py` |
| `config.py` 的 Settings 字段 / `_OPTIONAL_CAPS` / 那个 `model_validator` | `docs/内部/限流与配置体检.md` | `probe_config.py` |
| `scripts/eval_rag.py` / 检索 / 闸门 / 提示词 | `docs/内部/评估体系.md` | `eval_rag.py --selftest` |
| `eval/agent_eval.py` / `eval/agent_cases.jsonl` | `docs/内部/评估体系.md` | `agent_eval.py --selftest` |
| `frontend/` 任一文件 | `docs/内部/前端.md` | `npm run build`（⚠ 先停 dev server） |
| `.env` / 新增配置项 | 核心「需要知道的环境细节」 | `probe_config.py`，⚠ 三处一起改 |

## 常用命令

```bash
# 后端
uv sync                                          # 安装依赖（首次拉 torch，约 200MB）
uv run python -m bidding_qa.ingest --recreate    # 数据入库（首次下载向量模型 ~95MB）
uv run python -m bidding_qa                      # 启动 API :8000，接口文档 /docs
uv add <pkg>                                     # 加依赖（pyproject 走清华镜像）

# 重排调参（重排模型 ~1.1GB，首次启用时会自动下载）
python scripts/eval_rerank.py                    # 对比开/关重排的排序变化与耗时
python scripts/bench_rerank.py                   # 在目标机器上实测单条候选耗时

# 工具层 / Agent 模式：验证工具调用链路（改 tools/ / clients/ 前后都该跑一次）
# ⚠ 两条 DeepSeek 专属断言在别的供应商下打 [SKIP] 而非 FAIL，见 docs/内部/客户端层与图片解析.md
python scripts/probe_tools.py                    # 真机验证 tool_calls 拼装与 reasoning_content 回传规则
python scripts/probe_graph_tool.py               # 真机验证模型会在对的时机选对工具（需 Neo4j + API Key）
python scripts/probe_markup.py                   # 模型把工具标记漏进正文时的抑制器（不需要任何服务）
python scripts/probe_recover.py                  # 从漏出的标记里恢复工具调用 + schema 形状组（不需要任何服务）
python scripts/probe_recover_e2e.py              # 端到端串联：A 普通轮泄漏（需 Neo4j）+ B 收尾轮泄漏恢复（离线）
uv run python scripts/probe_quality.py           # 50 条：空结果时的「结果质量」提示 + 撞顶不算空手（不需要任何服务）

# 知识图谱层（改 graph_store.py 或任何 Cypher 前后都该跑一次；不需要大模型）
uv run python scripts/probe_graph_store.py       # 9 个查询类型 + 金额回归断言 + 只读防线 + 降级
uv run python scripts/probe_enum_route.py        # 列举句识别 + 提示词接线 + ③c 联网两维矩阵（不需要 Neo4j/大模型）
uv run python scripts/probe_pg_store.py          # 采购库层：画像对账 + 跨栈对账 + 只读行为 + 注入 + 降级
uv run python scripts/probe_pg_tool.py           # 真机验证模型会在对的时机选对工具（需 PostgreSQL + API Key）

# 图片解析（改 clients/vision_client.py / rag.with_image_context 前后都该跑一次）
uv run python scripts/probe_vision.py            # 离线接线断言 + 真机试调视觉模型（需 ZHIPU_API_KEY）

# 联网搜索（改 web_search.py / rag_tools.tools_for / rag 的网页片段前后都该跑一次；离线段不需要 Key）
# ⚠ ⑧ 现在覆盖**两个引擎**的泄漏归属（含跨引擎串台），⑨ 起的 Exa 断言在 `EXA_API_KEY` 为空时靠临时翻 ENABLED 跑，别把那段 try 收掉
uv run python scripts/probe_web_search.py        # 158 条断言：接线 + 冻结路径 + 工具矩阵 + 空结果重试 + 泄漏归属（含 Exa 双向串台）+ /api/chat 必定联网 + Agent 预联网种键 + 真机

# 文本大模型供应商（改 clients/llm_factory.py / ChatRequest·HealthResponse / api.py 那两处 get_llm 后都该跑一次）
uv run python scripts/probe_llm_provider.py      # 64 条：归一化 + 按名缓存 + 可用列表 + 请求级路由 + 降级提示点名（**全离线**，不需要任何 Key/服务/网络）

# 对话历史（改 conv_store.py / useChat 的同步逻辑前后都该跑一次；①②⑨ 离线段不需要数据库）
uv run python scripts/probe_conv_store.py        # 152 条：接线 + DDL + 降级 + 路由映射 + 往返 + 大 payload + 替换 + 级联 + 搜索转义/命中定位 + 时间戳 + pg_store 只读回归 + **整库清空（回滚事务）**

# 回答反馈（改 conv_store.py 的 feedback 三函数 / api.py 那三条路由 / useChat.setFeedback 之后都该跑一次）
uv run python scripts/probe_feedback.py          # 98 条：接线 + **无外键** + 降级文案 + 路由映射 + 往返 + upsert + 重存/删对话后反馈仍在 + 过滤夹紧

# 限流（改 api.py 那三行 Limiter 接线 / config.py 的 rate_limit_per_minute 之后都该跑一次）
uv run python scripts/probe_rate_limit.py        # 25 条：接线 + 真触发 + 中文文案 + Retry-After + health 豁免 + **每端点分桶** + SSE 穿越 + 0=关闭（**全离线**）

# 启动配置体检（改 config.py 的 Settings 字段 / _OPTIONAL_CAPS / 那个 model_validator 之后都该跑一次）
uv run python scripts/probe_config.py            # 33 条：只念不拦 + 逐能力点名（正/反向，标签整体匹配）+ .env 未声明键 + 只在构造时跑一次（**全离线**）

# 自建 MCP 服务器（改 mcp_server.py / mcp_bridge.py / 三个 run() 的 MCP 分支 / codec 之后都该跑一次）
uv run python scripts/probe_mcp_server.py --offline   # 70 条：内存内服务器面 + codec 字段集 + 冷却 + 断流过滤 + 路由(关)，**全离线**
uv run python scripts/probe_mcp_server.py             # 113 条：再加 ⑤ 真机（自己起子进程，需 Neo4j + PostgreSQL）+ ⑥ 路由(开)
uv run python -m bidding_qa.mcp_server                # 手动起服务器（默认 127.0.0.1:8765/mcp；地址由 MCP_SERVER_URL 反解）
# ⚠ MCP_SERVER_URL 留空 = 三个工具跑在进程内（默认且健康）；要验「经 MCP」得显式给它
MCP_SERVER_URL=http://127.0.0.1:8765/mcp uv run python scripts/probe_skills.py   # 应仍 92/92（main() 会临时置空）

# RAG 质量评估（LLM 当裁判，复刻 RAGAS 四指标；改检索/提示词/闸门前后都该跑一次）
uv run python scripts/eval_rag.py --selftest     # 71 条离线断言：打分数学 + 宽容解析 + 检查点命名 + 融合参数进 run-id（不需要任何服务）
uv run python scripts/eval_rag.py --no-llm       # 只跑检索层：零 API 成本、秒级，先看召回有没有坏
uv run python scripts/eval_rag.py --limit 5      # 小样（~32 次调用）；去掉 --limit 是全量 74 题（实测 458 次 / 6.5 分钟）
uv run python scripts/make_eval_questions.py     # 生成留出集（74 次调用）→ data/processed/eval_questions.jsonl
uv run python scripts/make_eval_questions.py --selftest   # 25 条离线断言，不需要 Key

# Agent 综合评估（四个工具全挂 —— 补的正是上面 --pipeline agent 摘掉的那一层；改 agent.py 签名前后都该跑）
PYTHONIOENCODING=utf-8 uv run python eval/agent_eval.py --selftest   # 48 条，**全离线**（不需要任何服务）
uv run python eval/agent_eval.py --check-tools    # 只报四个工具的可用性（会真 warmup 一次）
uv run python eval/agent_eval.py --limit 3        # 小样（⚠ 先停后端：本地 Qdrant 排他锁）
uv run python eval/agent_eval.py --out data/eval  # 全量 10 题 → data/eval/
uv run python eval/agent_eval.py --faith-context sources   # 换成与 eval_rag 可比的口径

# Agent 运行看板（评估体系的另一半：读线上总账。⚠ **不碰 Qdrant，不用停后端**）
uv run python eval/dashboard.py            # 总对话数 / 用户反馈 / 检索缓存
uv run python eval/dashboard.py --selftest # 28 条，**全离线**（不需要任何服务/Key/数据库）

# PostgreSQL（独立脚本，不进问答链路；改 to_postgresql.py 前后都该跑一次）
uv run python src/database/to_postgresql.py --dry-run   # 只解析并打印建表 SQL，不连库
uv run python src/database/to_postgresql.py             # 弹窗选 xlsx，整表替换灌进 chatbot 库

# 前端
cd frontend && npm install
npm run dev                                      # :3000
npm run build                                    # 唯一的类型检查入口（tsc + 构建）
```

⚠️ **`npm run build` 和 `npm run dev` 不能同时跑**：两者共用 `frontend/.next/`，build 会把它
改写成生产产物（`build-manifest.json` / `server/` / `static/` 全被覆盖）。dev server 进程不会
察觉，仍在监听 :3000 并**继续返回 200 的 HTML**，但页面引用的 `/_next/static/**` 全部 404 ——
表现是「页面出来了但没样式、不 hydrate」，很容易误判成自己改坏了组件。撞上了就
`rm -rf frontend/.next` 后重启 dev server。要跑 build 就先停 dev。

**本项目没有测试套件**，也没有配置 linter。改动后的验证方式是：`npm run build`（前端类型检查）+ 直接打后端接口：

```bash
# 纯检索（不经大模型，最适合快速验证召回改动）
curl -s -X POST localhost:8000/api/search -H "Content-Type: application/json" \
  -d '{"query":"履约保证金最高限额","mode":"hybrid","top_k":5}'

# 流式问答
curl -N -X POST localhost:8000/api/chat -H "Content-Type: application/json" \
  -d '{"question":"单一来源采购公示被质疑怎么办？"}'

# Agent 模式（模型可自行改写查询词追加检索）
curl -N -X POST localhost:8000/api/agent -H "Content-Type: application/json" \
  -d '{"question":"评标委员会的专家人数和构成有什么要求？","rerank":false}'

# 联网搜索（开关一开**必定**联网：先本地检索、再查一次 Tavily，两份一起作答；
# 本地答得出也照搜。`/api/agent` 也认这个字段——那边还会多注册一个 search_web 工具）
curl -N -X POST localhost:8000/api/chat -H "Content-Type: application/json" \
  -d '{"question":"2026年最新的投标保证金比例规定是什么？","web_search":true}'
```

> Windows 的 bash 会把 `-d` 里的中文按 GBK 发出去，服务端按 UTF-8 解析会回 400
> `There was an error parsing the body`。中文请求体请写成 UTF-8 文件后用 `--data-binary @file.json`。

## 架构与数据流

### 入库（一次性）

`dataset.load_qa_docs()` 解析 Excel → 去重 → `ingest.run()` 做两件事：向量化写入 Qdrant（`data/qdrant_local/` 或云），同时把 jieba 分词后的语料存成 BM25 索引（`data/index/bm25_index.json`）。**两处索引必须同时重建**，否则向量库与 BM25 的 `doc_id` 会对不上。

### 检索（每次请求）

`retriever.search()` 是唯一入口，串起三个模块：

```
vector_store.search()  ─┐
                        ├─→ hybrid.reciprocal_rank_fusion() → _passes_gate()
bm25_index.search()    ─┘                                          ↓
                                              _apply_rerank() → _gate_by_rerank()
                                                             ↓
                                                        _to_source()
```

- `hybrid.py` 负责 RRF 融合、片段抽取（滑窗取 jieba 词重合度最高的句子）、关键词高亮
- **关键不变量**：引用序号在「闸门过滤之后」才分配，保证正文里的 `[1][2][3]` 连续。改动排序/过滤逻辑时不要破坏这一点
- 三种模式语义不同：`vector` 模式下 `score` 就是余弦相似度；`bm25` 模式下 `score` 被归一化成「top=1.0」；`hybrid` 下是 RRF 归一化分
- **重排是可选后置环节，不是必需路径**：`reranker.py` 懒加载模型，`_apply_rerank()` 用 try/except 兜住所有异常并回退到融合顺序。改动时保持这个「降级而非报错」的性质——模型没下载好不应该让整个问答不可用。`warmup()` 同样吞异常，且**即使 `RERANK_ENABLED=false` 也预加载**（前端开关可随时打开，不能等到那时才花十几秒加载）
- 重排后**不要再回填未打分的尾部候选**：若 `_gate_by_rerank()` 把 head 筛空了，尾部只会更不相关，必须保持为空才能触发「无匹配」

### 相关性闸门（易踩坑）

RRF 只看排名，**没有「答非所问」的概念** —— 只要参与排名就一定拿到约 0.5 的分。所以 `_passes_gate()` 用原始分数兜底，向量或 BM25 **任一达标即保留**。

`MIN_VECTOR_SCORE=0.72` / `MIN_BM25_SCORE=8.0` 是针对**当前这份语料实测**标定的（相关问题 ≥0.84/≥20，无关问题 ≤0.67/≤3.8）。**更换知识库后必须重新标定**，否则会大面积误杀或形同虚设。⚠⚠ **但字面相近而答非所问的不在 ≤0.67 这一簇里**（盐化工那次实测 **0.8166**，在 0.72 之上）—— **检索层没有可用的相关性阈值，别在这儿修「答非所问」**，那要交给模型判断。

### 重排序闸门（比上面那个更准）

向量/BM25 只能判断「像不像」，CrossEncoder 判断的是「能不能回答」，因此开启重排时
`_gate_by_rerank()` 会按 `MIN_RERANK_SCORE=0.55` 再筛一轮。实测能回答的候选 ≈0.73，
知识库里根本没有答案的问题则全部落在 ≈0.50（模型对谁都「无感」）——这是召回层做不到的判据。
同样是**针对当前语料标定**的，换库需重测。⚠ **但重排分只在重排开启时存在**，而重排是用户随时可关的前端开关 —— **别拿它当通用判据**（「结果质量检查」不用它，就是这条）。

### 客户端层（`clients/`）

文本大模型（DeepSeek / 智谱 GLM）与视觉模型的收口处。工厂是 `clients/llm_factory.py` 的 `get_llm()`，按名缓存，每请求可由 `ChatRequest.llm_provider` 覆盖。

- ⚠ **加新供应商时必须实现 `default_temperature` / `default_max_tokens`**，漏了就是静默拿 DeepSeek 的默认值（在 GLM 上不会报错，只是行为不对）。
- ⚠ **按请求切换是「一维覆盖」** —— 名字认得出来就照办，**没配 Key 就如实报没配，绝不悄悄换成另一家**。
- ⚠ **加第三家供应商要动三处**：`_PROVIDERS` + `_LABELS`、它自己的 `_NOT_CONFIGURED`（`rag._no_key_hint()` 从这里切键名）、`probe_llm_provider.py` 的 `_KEY_FIELD` 映射 —— 漏一条全是静默。
- ⚠ **`BaseLLMClient` 是只做契约声明的抽象基类，别加运行期关卡** —— 它的唯一消费方是类型标注，加 `isinstance` 会把探针里的鸭子类型替身集体挡在门外。

跑 `uv run python scripts/probe_llm_provider.py`（全离线）。

→ 全文（9.8k，含「图片解析」一节）见 `docs/内部/客户端层与图片解析.md`。

### 工具层（`bidding_qa/tools/`）

六个工具的定义层：`base.py` 是引擎（`BaseTool` / 注册表 / `execute_tool` / `recover_tool_calls`），`rag_tools.py` 是具体工具与 `tools_for()`。定义改成了 **Pydantic 模型驱动**。

- ⚠ **依赖方向是唯一的硬约束**：`tools/*` 可以 import `..config` / `..schemas` / `..clients` / `..graph_store` / `..pg_store` / `..web_search` / `..rag` / `..mcp.mcp_bridge`，但**绝不 import `..agent`**（反过来立刻成环）。
- ⚠ **注册 ≠ 挂出去**：六个工具 import 期一律 `register()`，`TOOL_SCHEMAS` 才是**挂出去**的那份 —— 被禁用的工具若真被调用，拿到的是**该工具自己如实的失败**，而不是「没有这个工具」。
- ⚠ **清洗层**：`model_json_schema()` 对 `int | None` 的产出是 `anyOf: […]`、**没有顶层 `type`**，`.get("type")` 返回 `None` 会让恢复路径把字符串 `"5"` 原样交给 handler（**不报错、只是类型变了**）。⚠ 多分支的 `anyOf` 要原样保留，别一律展平。
- ⚠ **整数 / 枚举一律走 `Any` + `json_schema_extra`**（用 `int` + `ge/le` 会把夹紧变成 `ValidationError`；用 `Literal[...]` 会把中文枚举报错变成英文）。
- ⚠ **加新工具是四处**：`BaseTool` 子类 → `rag.py` 提示词片段 → `ToolCallBlock.tsx` 的 `TOOLS` 表 → 结构化结果再走 types / useChat / ChatMessage。

跑 `uv run python scripts/probe_recover.py` 与 `probe_quality.py`（都不需要任何服务）。

→ 全文（9.5k）见 `docs/内部/工具层.md`。

### Agent 模式（`/api/agent`）

**前端唯一使用的链路**（2026-09-20 起，端点硬编码在 `lib/api.ts` 的 `streamChat()`）：把工具交给模型自主调用。⚠ `/api/chat` 没有删 —— **改 Agent 代码时不要顺手删 `api.py` 里那条链路**。

- ⚠ **工具执行永不抛异常**（`execute_tool()`）。工具报错却漏掉 `role:"tool"` 消息，下一轮请求会因为 `tool_call_id` 无人匹配直接 400 —— 所有错误都必须转成文本回传给模型。
- ⚠ **循环必定收口**。工具轮数用尽却仍在请求工具时，会再跑一轮 `tool_choice="none"` 的强制收尾。少了它，任何多轮问题都会以「（未获得回答）」收场。
- ⚠⚠ **引用编号全局唯一**（`SourceRegistry`）—— 且**限额是个三角，三者耦合**：工具轮数
  `agent_max_rounds` / 来源上限 `AGENT_MAX_SOURCES` / `Markdown.tsx` 的行内引用正则
  `/\[(\d{1,2})\]/`（**只认 1~2 位数字，超 99 会让正文 `[n]` 从此不认**）。加轮数或加工具
  都会推高撞顶概率，动一个要看另两个。⚠ 撞顶（`fresh`/`known` 双双为空）与「一条没命中」
  **形状完全一样**，会被 `empty_reason()` 读成空手而归 —— 见 `docs/内部/工具层.md` 的结果质量检查。
- ⚠ **`reasoning_content` 回传**：含 `tool_calls` 的 assistant 消息**无条件回传该字段**，判据是「字段在不在」而非「内容空不空」。

改 `agent.py` / Agent 专属提示词之后跑 `probe_enum_route.py`、`probe_recover.py`、`probe_markup.py`（都不需要任何服务）。

→ 全文（18.6k）见 `docs/内部/Agent模式.md`。

### 知识图谱（`graph_store.py`）

Neo4j 里是 `batch/知识图谱_导入Neo4j.py` 建的中文图模型（以「交易」为中心，8751 笔）。**`graph_store.ENABLED` 是唯一开关**（= `NEO4J_URI` 非空），工具挂不挂、提示词提不提都读它。

- ⚠ **只用中文那套** —— `batch/to_neo4j.py` 建的英文标签那套按供应商聚合金额会笛卡尔积（单个供应商虚高 24 倍）。⚠ **重跑 `to_neo4j.py` 会把英文标签再建出来**，要跑就 `--database` 指到别的库。
- ⚠ **三条不变量**：① **绝不接受模型给的 Cypher**（`query_type` 走白名单分派，参数全走 `$param`）；② **只读**（session 级 `READ_ACCESS`）；③ **降级而非报错**（`query()` 无论如何都返回一个 `GraphResult`）。
- ⚠ **`sum(DISTINCT t.交易额)` 会静默算错钱**（两笔金额相同时 DISTINCT 吃掉一笔，激光打印机少算 5.3%），这些查询无 fan-out 顾虑，一律用 `sum()`。
- ⚠ **空 `entity` 必须挡住**：`CONTAINS ""` 恒为真，会静默返回「交易最多」那个实体的数据 —— 看起来完全合理的错误答案。
- ⚠ **源数据里有一笔 66 亿的异常交易**（`某县残疾人联合会`），是次高金额的 2.8 倍，看着像录入错误 —— 看到离谱数字先想到这里。

跑 `uv run python scripts/probe_graph_store.py`（不需要大模型）。

→ 全文（9.1k，含另两节）见 `docs/内部/图谱与采购库.md`。

### SSE 契约（前后端耦合点）

`api.py` 推送的事件类型与 `frontend/lib/types.ts` 的 `StreamEvent` 一一对应，改一边必须同步另一边：

| 事件 | 说明 |
| --- | --- |
| `start` | 请求开始 |
| `sources` | 检索结果 + 耗时，**先于**任何文本推送。**整体替换**，重发时必须带上完整 `latency_ms` |
| `sources`（第二次） | **两条链路同一判据**：开关打开、服务端配了 Key，且这次联网**真拿到了新来源**（`outcome.new_sources` 非空）。`/api/chat` 与 `/api/agent` 都在**生成本身开始之前**发（Agent 那条还要更早，在预检索之后、模型第一轮之前），且**必须排在第一个 `delta` 前面**。本地 0 召回时第二次的载荷就是纯网页条目。⚠ 联网**没拿到新结果时不发**——不能图省事发个和第一次相同载荷的 `sources`，那会白白触发一次面板重渲染。**不新增事件类型**，前端的整体替换天然接住 |
| `reasoning` | 思维链增量 → 前端「深度思考」折叠区 |
| `delta` | 正式回答增量 |
| `done` / `error` | 结束或错误 |
| `answer_reset` | **仅 `/api/agent`**：作废本轮已流出的前导正文（模型调工具前常先吐一句「我先查一下」）。⚠ 2026-09-20 之前 `/api/chat` 也发过它——那时联网是「答不出才搜」的兜底，需要先作废「让我联网搜索」那句再重答；现在联网发生在**任何正文流出之前**，那条链路上没有可作废的东西，事件彻底消失 |
| `tool_call` / `tool_result` | 仅 Agent：一次工具调用的开始与结果 → 前端「工具调用」折叠区 |
| `tool_result.graph` | 仅 `search_knowledge_graph` 带此字段（其余工具为 `null`）→ 前端「知识图谱查询」折叠区 |
| `tool_result.pg` | 仅 `search_procurement_records` 带此字段（其余工具为 `null`）→ 前端「采购数据库查询」折叠区 |

`tool_result` 的载荷里 `graph` / `pg` 是**已经 `.model_dump()` 过的字典**。`GraphResult` /
`PgResult` 是 pydantic 模型（`schemas.py`，与 `Source` 一样），直接塞进事件字典会在
`api.py` 的 `json.dumps` 处炸成 `TypeError`。`api.py` 对事件载荷是原样 `{**data}` 透传，加字段不用动它。

⚠⚠ **canonical：凡进 `Message` / localStorage 的字段必须小 —— 写失败是静默的**（约 5MB 配额）。
下面两条是它的实例（`Message.image` 不存 base64、网页正文要截断，都在这一条名下）。

⚠ `PgResult` 里**不能留 `Decimal` 与 `date`**：`numeric(18,2)` 回来是 `Decimal`、
日期列回来是 `datetime.date`，两者都不是 JSON 可序列化的，同样在那句 `json.dumps` 处炸。
**在构造 `PgResult` 时就转掉**（金额 → `float`，日期 → ISO 字符串），`filters` / `extra`
也要保证全是 `str` / `float`。⚠ `VisionResult.fields` 同一条：**值必须全是 `str`**
（模型会把金额返回成数字、关键条款返回成数组，`vision._as_text()` 统一转）。

**请求体侧**：`/api/chat` 与 `/api/agent` 的 `ChatRequest` 多了 `image_context: str = ""`
（图片解析结果的**裸文本**，模板由后端 `rag.with_image_context()` 拼，⚠ **前端 `lib/types.ts` 里
有第二份，改一边要改另一边**）。为空串时全链路行为
与不传这个字段**逐字节相同**——`probe_vision.py` ① 与 `/api/chat` 的三种请求体对比都钉着这条。
`/api/vision` 是**独立的普通 JSON 接口，不走 SSE**，所以上表不用改。

`ChatRequest` 还有 `web_search: bool = False`（联网搜索，见 `docs/内部/联网搜索与Exa.md`）。
⚠ 判据是**四条合取**（开关 ∧ `use_llm` ∧ `llm.ready` ∧ `web_search.ENABLED`），**传下去的是用户原始开关值、别提前算**。

`ChatRequest` 还有 `llm_provider: str = ""`（文本大模型供应商，2026-09-21 加，
见 `docs/内部/客户端层与图片解析.md`）。⚠ **空串 = 跟服务端默认**（`settings.llm_provider`），
此时行为与不传这个字段**逐字节相同**；认不出的名字降级到默认 + WARNING，**不报错**。
⚠ **它必须带默认值**：`probe_web_search.py` 直接构造 `ChatRequest(question=…, …)` 而**不传**
这个字段，漏了默认值那条探针会直接红。

⚠⚠ **`/api/health` 多了两个字段，但它们只能报「默认」那家**（没有请求上下文）：

```
llm_provider:  str                      # 归一化后的默认供应商名：deepseek / zai
llm_providers: list[dict[str, str]]     # [{name, label, model}]，**只列真配了 Key 的**
```

前端那个循环胶囊全靠 `llm_providers`。⚠ 与 `web_configured` vs `ChatRequest.web_search`
是**同一条限制**（`api.py` 里有先例注释）—— 所以 health 的 `llm_model` / `llm_ready`
描述的是**服务端默认**，不是「这次用的」。⚠ **`llm_providers` 是读活的 `settings`**、
不是 import 期快照：真机验过，把一个进程的 `ZHIPU_API_KEY` 抹空，它当场只剩 DeepSeek 一家。

⚠⚠ **`Source` 多了三个恒定字段，原先那条「逐字节相同」的不变量要按新口径读**：

```
url: str | None = None            # 知识库条目恒为 null
origin: Literal["kb","web"] = "kb"
published_date: str = ""          # ⚠ 必须是 str，别当 date 处理
```

所以 `sources` 事件的 JSON **永远**比加联网之前多这三个键（知识库条目是
`null`/`"kb"`/`""`），**不变量重述为**：「**除这三个恒定新增字段外**，index / 顺序 / 分数
逐字节相同」。`probe_web_search.py` ⑥ 按这个口径钉着 `build_context` / `build_messages` /
`build_agent_messages` / `fallback_answer` / `_render_result` 的字面量。

⚠ **`web_search` 开关本身的「逐字节相同」现在只对「不传 / `=false` / 服务端没配 Key」三种
情况成立**（`probe_web_search.py` ⑫ 的负例段钉着：`web_calls == []` + 单个 `sources`）——
2026-09-20 之前还有第四种「`=true` 但本地答得出」，那条**已经不成立了**：开关打开
必定联网，本地答得出也照发第二次 `sources`。这正是本次语义变更的核心。

这三个字段也是 §「`tool_result` 不加 `web` 载荷」那条的理由之一：网页结果已经作为
`Source` 并入了来源面板，再给 `tool_result` 加一份结构化载荷就是同一份数据画两遍。

### 限流（slowapi，2026-09-21 加）

`api.py` 里三行：`Limiter` + `RateLimitExceeded` 处理器 + `SlowAPIMiddleware`。开关是 `settings.rate_limit_per_minute`（**0 = 关掉**，此时连中间件都不挂）。

- ⚠⚠ **桶是「每个端点」的，不是全站合计** —— 这条**最容易记错**。slowapi 的桶键是 `(key_func 结果, 端点函数名)`，所以 `/api/chat` 与 `/api/agent` 各有一份 120/分钟。
- ⚠⚠ **429 处理器必须写成同步的 `def`** —— `SlowAPIMiddleware` 对**协程**处理器会**静默**退回它自带的英文处理器（不报错、不抛异常，只是用户读到一句英文）。
- ⚠⚠ **别换成 `SlowAPIASGIMiddleware`** —— 它的 `send_wrapper` 每收到一个 body 就重发一次 `http.response.start`，而本服务三条问答链路全是 SSE，那是 **ASGI 协议违例**。
- ⚠ **`/api/health` 整个豁免** —— 健康检查一旦被 429，前端会把轮询**加快到每 4 秒一次**，正好去撞更紧的限流，是个**正反馈**。⚠ 豁免靠**端点函数名**匹配，改名会静默失去豁免。
- ⚠⚠ **限流挡不住真正的暴露面** —— `API_HOST` 默认 `0.0.0.0` 而本服务**没有任何认证**，`web_search` 是请求参数，邻居可以白白烧掉 Tavily 额度。只在本机用就把 `.env` 的 `API_HOST` 改成 `127.0.0.1`。

跑 `uv run python scripts/probe_rate_limit.py`（全离线）。

→ 全文（6.1k，含「启动配置体检」一节）见 `docs/内部/限流与配置体检.md`。

### 启动配置体检（`config.py`，2026-09-21 加）

`Settings` 上挂了一个 `model_validator(mode="after")`（`_report_startup_config`），在**构造时**把配置一次说清 —— 也就是 `config.py` 末尾那句模块级 `settings = get_settings()`，**import 期**就跑完。

- ⚠⚠ **只念不拦：绝不 `raise`** —— 五个可选能力全靠「没配就降级」活着，而 **`.env.example` 交上去时这几个 Key 本来就是空的**，在那里 `raise` 等于把「可选」变「必需」，服务直接起不来。
- ⚠ **级别必须是 WARNING，别降成 INFO**。它跑在**任何 `logging.basicConfig` 之前**，那时只有 WARNING 能落到 stderr，INFO 会被直接丢弃 —— 体检就白做了。
- ⚠ **加一个可选能力 = 在 `_OPTIONAL_CAPS` 里加一行**（前提是它的开关就是某个字段非空）；⚠ 表里的字段名必须都在 `Settings.model_fields` 里，否则 `getattr(s, field, "")` 会静默拿到哨兵 `""`，于是一个**开着**的能力被报成「未启用」。
- ⚠ 它报三件事：未启用的可选能力（逐项点名 + 缺哪个 Key）/ 两个文本 Key 都空 / **`.env` 里未声明的键**（`extra="ignore"` 会静默丢弃，本仓库为此栽过四次）。

跑 `uv run python scripts/probe_config.py`（全离线）。

→ 全文（6.1k，含「限流」一节）见 `docs/内部/限流与配置体检.md`。

### PostgreSQL 导入（`src/database/to_postgresql.py`）

独立脚本，**不在问答链路里**：弹窗选一个 xlsx，整表灌进本机 PostgreSQL 的 `bid_records` 表。凭据读 `.env` 的 `POSTGRES_*`（它不 import `bidding_qa.config`，自己解析）。

- ⚠ **`PROJECT_ROOT` 是 `parents[2]`，不是 `parents[1]`** —— 本文件在 `src/database/` 下，比 `batch/` 深一层。写错**不报错**，只是 `.env` 和默认 xlsx 全都找不到、路径全空。
- ⚠ **numpy 标量不能直接喂 psycopg**（`cannot adapt type`），且空值有 `None` / `NaN` / `NaT` 三副面孔，而 **`float('nan')` 在 Python 里是真值** —— 逐格走 `CONVERTERS` 显式转换。
- ⚠ **改列定义要重建表**：`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，不补列也不改列；`check_schema()` 会在列对不上时拒绝写入（COPY 按列名错位是**静默**的）。
- ⚠ **Windows Git Bash 里 `psql -c "…中文…"` 会失败**（bash 按 GBK 发参数，同 `curl -d` 那个病）—— 中文 SQL 写成 UTF-8 文件后用 `psql -f`。

先跑 `uv run python src/database/to_postgresql.py --dry-run`（只解析并打印建表 SQL，不连库）。

→ 全文（9.1k，含另两节）见 `docs/内部/图谱与采购库.md`。

### 采购记录库（`pg_store.py`）

同一个 `bid_records` 表（与图谱**同源**），包成 `search_procurement_records` 工具。查询方式是**结构化过滤器 + 代码拼 SQL**：**模型给的字符串永远不进入 SQL 文本**，列名走白名单 dict 查找、值全走 `%s`。

- ⚠ **只读** —— 但 **`conn.read_only = True` 在 `autocommit=True` 下是个假防线**（实测它读完返回 `True`，`DELETE` 照样放行）—— 采用连接参数 `options="-c default_transaction_read_only=on …"` + 建连后自检。⚠ **任何「设置了就算数」的安全断言都要先验一把**。
- ⚠ **NULL 组会霸榜**（`代理机构` 7698/8751 为空，排出来第一名是「（空）7698 条」）：聚合模式一律 `WHERE 列 IS NOT NULL`，且 `group_by` 的候选里根本不放 `agency`。⚠ 可空排序键一律加 `NULLS LAST`。
- ⚠ **`ILIKE` 通配符要转义**：裸的 `'%%%'` 命中 8751 行（全表）—— 与图谱「空 entity」是同一类静默错答案。⚠ **空筛选必须挡住**（只有 `sort_by` 也算说清了要什么）。
- ⚠ **66 亿那笔错账在这张表里同样存在**：`sort_by` 默认 `published`，用户明确要金额排行时照排，但渲染时附「疑似录入错误」的提醒。

跑 `uv run python scripts/probe_pg_store.py`（不需要大模型）。

→ 全文（9.1k，含另两节）见 `docs/内部/图谱与采购库.md`。

### 对话历史持久化（`conv_store.py`）

服务端为主、localStorage 降为降级回退。与 `pg_store.py` 共用 `POSTGRES_HOST` 开关（同一条 PG），但**可写**：刻意没有 `default_transaction_read_only`，失败**抛** `ConvStoreUnavailable` → 路由 503。

- ⚠ **表结构：`payload jsonb` + 两个生成列**，**生成列只能引用同表已声明过的列**，`payload` 挪到后面会让建表直接报错，而那是**静默降级**（前端整个历史功能变成「未同步」）。没有迁移框架，改列要手工 DROP + CREATE。
- ⚠ **`Conversation.remote` —— 一个标志，三个守卫**（PUT / localStorage / `send()`），缺一个都是静默的数据销毁。
- ⚠ **同步边界是「一轮回答结束」**：靠 `streaming` 守卫、**零防抖、零定时器** —— `patchMessage` 每个 SSE 事件调一次，一轮问答就是几百上千次。写入器单飞是**正确性**要求（两个并发 PUT 没有先后保证）。
- ⚠ **`/api/conversations/search` 必须声明在 `/api/conversations/{conv_id}` 之前** —— FastAPI 按声明顺序匹配，反了的话搜一个词会被当成搜 `id="search"` 的对话，表现是「搜索永远没有结果」——完全静默。

跑 `uv run python scripts/probe_conv_store.py`。

→ 全文（23.3k）见 `docs/内部/对话历史与反馈.md`。

### 用户反馈（`feedback` 表，2026-09-21 加）

每条回答可以点 👍/👎，存进 `feedback` 表。**只做「收集 + 可查」**，反馈**不回流**到检索或生成（语料只有 74 条，按反馈调权重必然过拟合）。

- ⚠⚠ **表上一处外键都没有** —— 这是整个设计唯一的关键决定。`save_conversation()` 每次保存都整批重插消息，加外键的话级联会在**下一次自动保存时把这条对话上所有的 👍/👎 静默删光**。⚠ 不加外键还顺带对了第二件事：对话被删后反馈仍留着（分析数据不该跟着会话走）。⚠ 由行为断言钉着（`probe_feedback.py` ⑦⑧），**谁日后「顺手把外键补上」，那两条会当场变红**。
- ⚠ **同一个事实存两处、互不替代**：哪个拇指亮着走 `Message.feedback`（随 `payload jsonb` 走，后端零改动）；这次评价是什么走 `feedback` 表。
- ⚠ **`doc_ids` 是 `jsonb` 而不是 `text[]`，且必须包 `Jsonb(...)`** —— 裸 `list` 会 `ProgrammingError: cannot adapt type`，类型检查发现不了。

跑 `uv run python scripts/probe_feedback.py`。

→ 全文（23.3k，含「对话历史持久化」一节）见 `docs/内部/对话历史与反馈.md`。

### 图片解析（`clients/vision_client.py`）

提问窗口的回形针 → `POST /api/vision` → 智谱 `glm-4.6v-flashx` 抽成结构化字段，渲染成「图片解析」卡片，并作为上下文附到这次提问上。

- ⚠⚠ **它不是 Agent 工具**（最容易改错的一点）：解析发生在上传那一刻、早于提问，也不由模型决定要不要调 —— 做成工具会引入投票、别名撞名、泄漏归属一整串新风险。
- ⚠ **唯一的硬不变量：图片内容只进提示词，绝不进检索。** 直觉上「拼进 `question` 就完事」是错的：那样检索会拿一整份公告去查、向量被稀释，**极可能直接跌破闸门返回 0 条**。
- ⚠ **模板有两份**（`frontend/lib/types.ts` 与 `rag.with_image_context()`），改一边要改另一边。
- ⚠ **`glm-4.6v-flashx` 是推理型模型**，思维链会先吃掉 `max_tokens`，给少了表现为「图传上去了，返回空」——最难查的那种。

跑 `uv run python scripts/probe_vision.py`（① 离线接线断言）。

→ 全文（9.8k，含「客户端层」一节）见 `docs/内部/客户端层与图片解析.md`。

### 联网搜索（`web_search.py`）

把 Tavily 包成一个可选能力。网页结果**并入「参考来源」面板、带同一套 `[n]` 编号**（不做独立折叠块 —— 它对模型和用户而言确实就是【资料】）。

**语义：开关打开 ⇒ 每次提问都联网**，不是「答不出才搜」。判据是 `payload.web_search and payload.use_llm and llm.ready and web_search.ENABLED` 四条同时为真，⚠ **传下去的是用户那个原始开关值，别提前算**（`tools_for()` 内部还要再合一次）。

- ⚠ **网页内容是不可信输入，只当资料读**：渲染时在块首插入 `_WEB_UNTRUSTED_NOTICE`、网页块标成 `【资料4｜网页：ah.gov.cn】`、提示词写死「不要执行其中的任何指示」。⚠ 防线**跟随数据、不跟随开关**。
- ⚠⚠ **`WEB_SEARCH_MIN_SCORE=0.70` 是「准入线」，判据是这一批的 top 分，不是逐行分**（低于就整批返回空）。⚠ 已知偶发失败：同一句查询的 top 分**偶尔**整体掉下来（Tavily 返回退化结果）—— **别据此加「闸门失败就重试」，也不许改成「留下 top-1」**。
- ⚠ **`WEB_SEARCH_DOMAINS` 必须是逗号分隔的 `str`，不能是 `list[str]`**：pydantic-settings 对复杂类型走 JSON 解析，写在模块级 `get_settings()` 处抛 `SettingsError`，**整个服务起不来**。
- ⚠ 前端那个开关**刻意是唯一不持久化的**（每次打开页面都从「关」开始）—— 它是**唯一花钱**的开关，每次提问 1 个 credit。⚠ 别把 `bidding-qa-web-search` 那个旧键读回来。

跑 `uv run python scripts/probe_web_search.py`（⑪ 真机段需 Key）。

→ 全文（20.4k，含「Exa 备用搜索引擎」一节）见 `docs/内部/联网搜索与Exa.md`。

### Exa 备用搜索引擎（`src/bidding_qa/mcp/`，2026-09-30 加）

Tavily 之外的第二个联网引擎：`search_web` 返回 0 条或明显答非所问时，模型可以换个引擎、**用同一个查询词**再搜一次，结果同样带 `[n]` 进同一个来源面板。

- ⚠ **它不是独立开关**：**三个条件同时为真**才挂出去 —— 用户那个「联网搜索」开关 ∧ `web_search.ENABLED` ∧ `web_search_exa.ENABLED`。理由是 **Exa 每次调用都花钱**，没开联网就挂出去 = 未经同意扣费。
- ⚠⚠ **绝不能叫 `src/mcp/`**：那个名字会**遮蔽官方 `mcp` SDK 包**（`mcp_client.py` 里正是 `from mcp import ClientSession`），失败形态是 `ImportError: cannot import name 'ClientSession'`，**指向不到真正的原因**。
- ⚠⚠ **认证走 `Authorization: Bearer` 头，绝不进 URL** —— 托管端点也认 `?exaApiKey=<key>`，**那是不能用的那一种**（密钥会进请求日志、异常信息、任何一层代理的访问日志）。
- ⚠⚠ **`exa_min_score` 是个死旋钮，而它故意留着**：结果里根本没有 `score` 字段，`to_sources()` 整批没分数时**跳过闸门**并记 WARNING。⚠ **也不许按 score 排序**，顺序原样保留 Exa 给的那一份。

跑 `uv run python scripts/probe_web_search.py` 与 `probe_enum_route.py`。

→ 全文（20.4k，含「联网搜索」一节）见 `docs/内部/联网搜索与Exa.md`。

### 自建 MCP 服务器（`mcp_server.py` + `mcp/mcp_bridge.py`，2026-09-30 加）

把图谱 / 采购记录 / 技能**三个工具搬进一个独立进程**，后端经 MCP 协议当调用方。开关是 `MCP_SERVER_URL`，**留空 = 跑在进程内（默认且健康，与加这个功能之前逐字节相同）**。

- ⚠⚠ **路由分支在三个 `run()` 里，不在 `execute_tool` 上** —— 它的位置是**校验与去重之后**、本地调用之前。提到 `execute_tool` 上会把 `ctx.seen` 的键**提前**写进去，模型第一次参数写错就再也改不对了。
- ⚠⚠ **白名单查在 `execute_tool` 之前**：服务器一 import `rag_tools`，`_REGISTRY` 里就躺着**六个**工具，不在白名单就直接回「本服务器没有名为 X 的工具」。
- ⚠⚠ **两条 SDK 细节改了都是静默故障**：`@server.call_tool(validate_input=False)`（默认会把中文报错换成英文 input validation error）；**不设 `outputSchema`**（声明了却没返回会抛错）。
- ⚠⚠ **不要 import `..tools`（成环）** —— 桥接层只回裸 dict；⚠ `outcome_to_payload()` 的字段集必须与 `ToolOutcome` **逐字段相等**，忘了 codec 会红在字段集断言上，而不是红在「前端图谱块渲染成空白」。
- ⚠ **它不能进 `_OPTIONAL_CAPS`**（那张表的语义是「字段非空即为开」）。

跑 `uv run python scripts/probe_mcp_server.py --offline`（全离线）。

→ 全文（8.5k）见 `docs/内部/MCP服务器.md`。

### RAG 质量评估（`scripts/eval_rag.py`）

RAGAS 装不进去，所以用「LLM 当裁判」按其**公开定义**在本地复刻四个指标 —— 保留的是方法论，不是它的代码，**数字也不与 RAGAS 可比**。旋钮全走 CLI，`config.py` 一个字段都不加。

- ⚠⚠ **空检索必须单独成桶，绝不能混进均值**：闸门把问题**整体筛空**时，模型回的那句拒答是唯一一条陈述、又恰好被**空上下文**支持 ⇒ **faithfulness = 1.0，满分**。判据用结构（`len(sources) == 0`）而非话术。
- ⚠⚠ **两份数据集都在天花板上**（问题原文就在索引里，检索必然命中）—— 有信号的只有**相对变化**与最差题清单。
- ⚠⚠ **先量噪声底，再看数字**：均值层面噪声底 ≈ **±0.005**（动了 0.01 以上才值得当信号），**单题噪声 ±0.28，绝不能用单题下结论**。
- ⚠⚠ **检查点短名必须两两不同**：**不能拿 `name[:3]` 图省事**（`context_recall` 与 `context_precision` 前三个字母都是 `con`），写死一张 `_METRIC_SHORT` 表。
- ⚠ **跑评估之前通常要停掉后端**（Qdrant 排他锁）；`--no-llm` 是零 API 成本的检索层，几秒出结果，先跑它。

跑 `uv run python scripts/eval_rag.py --selftest`（71 条离线）。

→ 全文（含另两节）见 `docs/内部/评估体系.md`。

### Agent 综合评估（`eval/agent_eval.py`，2026-09-21 加）

补的正是 `eval_rag.py --pipeline agent` **主动摘掉**的那一层：四个工具全挂，同时报「工具选得对不对」与「答得好不好」。**Agent评分 = 0.6 × 综合评分 + 0.4 × 工具选择准确率**（综合评分 = 三个 RAG 指标的等权均值）。

- ⚠⚠ **绝不要在这里调 `_isolate_agent_tools()`** —— 那是本脚本与那个脚本唯一的分野。
- ⚠⚠ **`RecordingLLM` 的快照必须 `deepcopy`**：`messages` 是**同一个 list 对象**，存引用的话五轮记下来的是同一份终态，前几轮的工具证据全丢，而且丢得**看不出来**（证据反而更全、报告一切正常）。
- ⚠⚠ **faithfulness 的判据**：图谱与采购库的结果**不是 `Source`**、从不进 `sources` —— 只拿 `sources` 当判据的话，那类答案 faithfulness **恒为 0**，是**系统性低估且报告上看不出异常**。默认 `--faith-context evidence`。
- ⚠⚠ **工具选择准确率：`hit ⟺ (require ⊆ called) and (called ⊆ allow)`** —— 预检索与预联网是无条件的、且**不是工具调用**，所以「模型直接作答」与「调了工具」都算路由正确。⚠ **空过会抬高这个数**。
- ⚠ **必须先停掉后端**（Qdrant 排他锁），Neo4j / PostgreSQL 得在跑 —— **「配了但连不上」与「没配置」的处置不同，别混**。

跑 `PYTHONIOENCODING=utf-8 uv run python eval/agent_eval.py --selftest`（全离线）。

→ 全文（含另两节）见 `docs/内部/评估体系.md`。

### Agent 运行看板（`eval/dashboard.py`，2026-09-21 加）

与 `agent_eval.py` 是评估体系**互补的两半**：它答「**线上跑成什么样**」（攒了多少对话、用户满不满意），数据源是 PostgreSQL，**不碰 Qdrant，不用停后端**。

- ⚠⚠ **计数走真 `count(*)`，绝不用 `len(list_*)`** —— `conv_store` 的两个列表函数**都封顶 500**，拿 `len()` 当总数，数据长起来那天会显示一个**看起来完全合理的错误数字**（界面写「总对话数 500」，实际 1200）。
- ⚠⚠ **零反馈打 `0 (—)`，不打 `0 (0%)`** —— `0%` 的意思是「测过了，全是差评」；`—` 的意思是「还没有人评过」。
- ⚠⚠ **`检索缓存` 是一根待接的钩子，不是硬编码的一句假话** —— 本仓库没有任何检索缓存子系统，这一行**只能**打 `(暂无数据)`，**别拿别的指标凑数**。

跑 `uv run python eval/dashboard.py --selftest`（全离线）。

→ 全文（含另两节）见 `docs/内部/评估体系.md`。

## 需要知道的环境细节

- **`config.py` 在导入时设置 HuggingFace 环境变量**（`_apply_hf_env`）。因此任何用到 `sentence_transformers` 的模块都必须先 `from .config import settings`，否则镜像配置与 `HF_HUB_DISABLE_XET` 不生效。
- **`HF_HUB_DISABLE_XET=1` 不能删**：`huggingface_hub>=1.0` 默认走 Xet 协议，hf-mirror 不支持，会导致模型下载**静默卡死**（无报错、无超时）。
- **DeepSeek v4 会先输出思维链**（`delta.reasoning_content`）再输出 `delta.content`。只读 `content` 会让用户面对近 30 秒空白。两者在 `llm.stream()` 中以 `(kind, text)` 元组透传。
- **本地 Qdrant 模式两个限制**：① 同一目录独占锁 —— 起 API 后无法再跑 ingest，必须先停服务；② `delete_collection` 是空操作，`--recreate` 依赖 `ensure_collection()` 里额外的 `clear()` 兜底清空。
- **`.env` 含真实密钥且已被 gitignore**，模板在 `.env.example`。`QDRANT_MODE=auto` 会在云服务连不上时静默降级到本地模式（当前即为此状态，日志有 WARNING）。
- **`Settings` 是 pydantic-settings 且 `extra="ignore"`**：`.env` 里写了、`config.py` 没声明的键会被**静默丢弃**（`NEO4J_*` 那 7 个键就这么白写了很久）。加配置项时先确认 `config.py` 里有同名字段。⚠ 这条已踩**四次**（Neo4j / Postgres / Zhipu / **Tavily**），四段配置的注释里都是同一个提醒。
  ⚠ **2026-09-21 起这条有自动防线了**：`Settings` 上的 `model_validator` 会在启动时把
  未声明的 `.env` 键逐个点名（WARNING，早于一切日志）。见 `docs/内部/限流与配置体检.md`。
  ⚠ 但那只是**兜底提醒**，不是「可以不用改三处了」——声明缺失的键仍然要改 `config.py`。
  ⚠ 而且复杂类型（`list[str]`）会让 pydantic-settings 走 JSON 解析，`.env` 里写逗号分隔的列表会在**模块级** `get_settings()` 处抛 `SettingsError` → 整个服务起不来。列表型配置一律声明成 `str` + 逗号 split（`cors_origins` / `web_search_domains` 都是这个形状）。
- **联网搜索（Tavily）需要自备 Key**：`.env` 里的 `TAVILY_API_KEY` **已配置**（2026-09-20 起），所以 `search_web` 工具会挂出去、`/api/chat` 与 `/api/agent` 的必定联网都会执行、`/api/health` 报「可用」。申请地址 https://tavily.com（免费档 1000 credits/月，`search_depth=basic` 每次 1 credit）。Key 为空时全部如实降级（工具不挂出去、提示词不提、两条链路都不联网）。
  ⚠ **免费档 1000 credits/月 ÷ 每次提问 1 credit = 约 1000 次提问**。开关默认关着，且 tooltip 写明了消耗，但**演示/答辩前记得看一眼用量**——把它拨开一整天就能烧掉大半。
  ⚠ `WEB_SEARCH_MIN_SCORE` 已实测标定（**0.70**，见 `docs/内部/联网搜索与Exa.md`）；**但它与 `.env` 是两处**——`.env` / `.env.example` 里也各写了一份，会**静默覆盖** `config.py` 的默认值（标定当天就踩过一次，复标时又踩了一次）。**改默认值必须三处一起改**。
- ⚠⚠ **`API_HOST` 默认 `0.0.0.0` = 绑定所有网卡，而本服务没有任何认证**（`.env` 里当前就是
  `0.0.0.0`）。`netstat` 上显示 `0.0.0.0:8000 LISTENING`，局域网内任何机器都能直接问 ——
  而 `web_search` 是**请求参数**，所以别人可以白白烧掉 Tavily 额度。
  **CORS 拦不住**（浏览器规则，`curl` 不理会）；**限流也只是缓解**（每端点 120/分钟，
  两个问答端点合起来 240/分钟，照样能烧）。只在本机用就把 `.env` 的 `API_HOST` 改成
  `127.0.0.1`。要拿手机/另一台电脑演示才需要留 `0.0.0.0`，那种场合才该考虑限流。
- **Neo4j 是本机实例**（`bolt://localhost:7687`，库名 `neo4j`），`.env` 里的 `NEO4J_PASSWORD` 同样是真实凭据，别内联到命令行。连不上只影响图谱工具，问答链路照常。
- **PostgreSQL 也是本机实例**（`localhost:5432`，装在 `D:\postgresql`），`bid_records` 表（8751 行）由**两个**模块读：
  `pg_store.py`（采购记录查询工具，**只读**）与 `conv_store.py`（对话历史，**可写**）。
  `src/database/to_postgresql.py` 是灌数据的独立脚本，不在问答链路里，见 `docs/内部/图谱与采购库.md`。
  ⚠ **下面这句曾经是错的，2026-09-20 订正**：原文写着「`POSTGRES_*` 那几个键没有加进
  `config.py`……独立脚本自己解析 `.env`」—— 与代码矛盾。`config.py:126-135` 里
  `postgres_host` / `port` / `user` / `password` / `database` / `connect_timeout` /
  `query_timeout` 全都在，`pg_store.py` 一直在读。**只有 `to_postgresql.py` 自己解析 `.env`**
  （它是独立脚本，不 import `bidding_qa.config`）。
  ⚠ 对话历史与采购记录库**共用同一个开关** `POSTGRES_HOST`：置空则两个能力一起关掉。
    这个耦合是有意的（同一个库），不为历史另加开关。
- **本机 torch 是 CPU 版且 8 线程已跑满**（`torch.get_num_threads()==8`），重排推理没有余量可挖；`torch.quantization.quantize_dynamic` 在 torch 2.14 + XLM-R 组合下会抛 `AttributeError`，别再试。有 GPU 的机器设 `EMBEDDING_DEVICE=cuda` 即可。
- ⚠ **Git Bash 会把 `/x` 形式的参数当路径转换掉**：`robocopy … /E` 报 `无效参数 #3: "E:/"`、`reg query … /v` 报找不到项 —— 都被换成了 `E:/`、`v` 这样的路径。解法是**在命令前加 `MSYS_NO_PATHCONV=1`**，同时把 `//c` 写回 `/c`（`MSYS_NO_PATHCONV=1 cmd /c robocopy … /E`）。这与下面 `curl -d` / `psql -c` 的中文编码问题是**同一个 shell 的两副面孔**，中文参数 + 西文斜杠参数撞上了就一起犯。
- **想在 dev server 跑着的时候验 `npm run build`**（两者共用 `.next`，见上文警告）：别去关 dev，把前端复制一份到临时目录跑即可 —— `cp -r app components hooks lib *.json *.mjs *.ts *.d.ts <tmp>/`，再用 `MSYS_NO_PATHCONV=1 cmd /c robocopy … /E` 复制 `node_modules`（**别用 `mklink /J` 挂 junction**：junction 会被解析成真实路径，webpack 报 `Can't resolve './D:/…/next/dist/client/next.js'`）。跑完删临时目录，真实 `frontend/.next` 与 dev server 全程不受影响。

## 前端要点

配色走 `globals.css` 里的 CSS 变量，Tailwind 在 `tailwind.config.ts` 里映射成语义色名。所有会话状态集中在 `hooks/useChat.ts`，`page.tsx` 只负责布局与主题。

- ⚠ **`useChat.ts` 的 `run()` 依赖数组**别漏掉开关：`[hybrid, rerank, webSearch, llmProvider, patchMessage]`。漏了会拿到闭包里的旧值，表现为「开关点了但没生效」。
- ⚠ **旧 localStorage 键一律不读**：`bidding-qa-agent` 读回来会把老用户**永久钉在写死流水线上**（开关已经没了，他再也拨不回来）；`bidding-qa-web-search` 是同一条处置的镜像（残留的 `true` 会把联网永久粘在开启态）。
- ⚠ **控件尺寸与阴影有统一约定**：五个控件一律 `h-8`（改高度要五个一起改）；⚠ **别加 `focus-within:ring-*`** —— 容器的阴影是**任意值** `shadow-[…]`，与 Tailwind 的 ring 合成变量互斥，写了会**静默不显示**。
- ⚠ **`Message.image` 存 `VisionPayload`（解析结果），绝不存原图 base64** —— `useChat` 会把整个 conversations 写进 localStorage（约 5MB 配额），塞进去是**静默**失败。
- ⚠ **底部控件的 prop 要一路传到底**：`page.tsx` 有**两条**路径（首屏那条和多轮那条），漏一边的表现是「首屏与多轮行为不一致」。

跑 `npm run build`（⚠ 与 `npm run dev` **不能同时跑** —— 两者共用 `frontend/.next/`）。

→ 全文（8.1k）见 `docs/内部/前端.md`。

## 数据

`data/processed/问题答案样例数据.xlsx`（`问`/`答` 两列）实际有 81 行数据行：1 行问答不完整被跳过，6 组为仅标点不同的重复问题，默认 `DEDUPE=true` 后以 **74 条**入库。被丢弃的条目逐条打日志。样例数据里混有化工行情、设备采购等非招投标法规范畴的内容。
