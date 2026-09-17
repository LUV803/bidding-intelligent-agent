---
name: probe-regression
description: 改完某块代码该跑哪个探针 —— 17 个 probe_*.py 加 2 个评估脚本的「改了什么 → 跑哪个」对照表，标注 11 条全部离线、哪些要 Neo4j/PostgreSQL/API Key，以及两个容易接反的前提和一批「红了但不是回归」的已知项。Use when 用户改完代码问「要跑什么验证」「跑一下探针」「怎么验这次改动」，或你自己要确认改动没破坏契约。
---

# 探针回归：改了什么 → 跑哪个

⚠ **探针验的是契约，不是语义** —— 全绿只说明「没破坏既有约定」，不说明「功能对」。
反过来，**改了 `src/` 之外的**（`docs/`、`.claude/`、`README`）**一个都不用跑**。
前端改动跑 `npm run build`（仓库**唯一**的类型检查入口）。

## 两条纪律，先看

### 1. 断言**行为**，别断言源码文本

⚠ `grep` 当判据会**静默为真** —— 仓库踩过：用 `grep '"reasoning"'` 判断某修复部署没部署，
三行命中全是 `LLMEvent` 的事件类型字符串与 docstring，**两版代码都命中，什么都没验到**。
正确形状是导入那个函数、喂一个 `SimpleNamespace` 进去测输出（`probe_vision.py` 就是这么做的）。

### 2. 探针基本**不用停后端**

它们都用替身 monkeypatch 掉 `api.get_retriever`（实测 `probe_llm_provider.py:290`、
`probe_rate_limit.py:203`、`probe_web_search.py:844` 都是 `api.get_retriever = lambda: …`），
不碰真 Qdrant。

⚠ **例外两条**（要真 retriever，撞本地 Qdrant 排他锁，**必须先停后端**）：
`scripts/eval_rag.py --retrieval live` 与 `eval/agent_eval.py`。
（`eval/dashboard.py` 不用停 —— 它只读 PostgreSQL，不碰 Qdrant。）

## 对照表

| 改了什么 | 跑哪个 |
| --- | --- |
| `config.py` 的 `Settings` 字段 / `_OPTIONAL_CAPS` / 那个 validator | `probe_config.py` |
| `clients/llm_factory.py` / `ChatRequest`·`HealthResponse` / `api.py` 两处 `get_llm` | `probe_llm_provider.py` |
| `clients/base_client.py`（抑制器 / `stream()` 契约） | `probe_markup.py` |
| `clients/vision_client.py` / `rag.with_image_context` | `probe_vision.py` |
| `clients/local_client.py`（本地供应商 / `supports_tools`） | `probe_tools.py` + `probe_llm_provider.py` |
| `tools/base.py`（清洗层 / 恢复 / 别名 / 投票） | `probe_recover.py` —— ⚠ **先看用例 0 / 0b**，漏了是**静默降级**；改了泄漏恢复再补 `probe_recover_e2e.py`（端到端，需 Neo4j） |
| `tools/rag_tools.py`（工具定义 / 参数名 / 枚举） | `probe_recover.py` + `probe_quality.py` + `probe_enum_route.py` |
| `agent.py` 的结果质量 / 轮数 / 来源上限 | `probe_quality.py` |
| `agent.py` 的 `run_agent_stream` **签名** | `eval/agent_eval.py --selftest`（③ 钉 `stream_with_tools` 的调用形状） |
| `graph_store.py` 或任何 Cypher | `probe_graph_store.py` |
| `rag.is_enum_question` / 提示词路由 | `probe_enum_route.py` |
| `pg_store.py` 或任何 SQL | `probe_pg_store.py` |
| `web_search.py` / `rag_tools.tools_for` / rag 网页片段 | `probe_web_search.py` |
| `conv_store.py` 或任何 SQL | `probe_conv_store.py` + `probe_feedback.py` + `eval/dashboard.py --selftest` |
| `conv_store.py` 的 feedback 三函数 / `api.py` 三条反馈路由 / `useChat.setFeedback` | `probe_feedback.py` |
| `api.py` 的 Limiter 三行 / `rate_limit_per_minute` | `probe_rate_limit.py` |
| `retriever.py` / `hybrid.py` / 闸门 / 重排 / 提示词 | `eval_rag.py --selftest` → 再 `--no-llm` |
| `conv_store.usage_stats()` | `eval/dashboard.py --selftest` + `probe_conv_store.py` ⑫ |
| 前端（`useChat` / 组件 / 类型） | `npm run build`（⚠ 与 `npm run dev` 互斥，共用 `.next/`） |

⚠ **真机探针**（要服务 + API Key，不是改完就必然要跑）：`probe_graph_tool.py`（Neo4j）、
`probe_pg_tool.py`（PostgreSQL）。它们验的是「**模型会在对的时机选对工具**」——
那是提示词层的回归，改 `rag.py` 的路由段时才值得跑。

## 11 条**全离线**的（几秒出结果，随时可跑，不需要任何服务/Key/网络）

```bash
uv run python scripts/probe_config.py
uv run python scripts/probe_llm_provider.py
uv run python scripts/probe_rate_limit.py
uv run python scripts/probe_markup.py
uv run python scripts/probe_recover.py
uv run python scripts/probe_enum_route.py
uv run python scripts/probe_quality.py
uv run python scripts/eval_rag.py --selftest
uv run python scripts/make_eval_questions.py --selftest
PYTHONIOENCODING=utf-8 uv run python eval/agent_eval.py --selftest
uv run python eval/dashboard.py --selftest
```

⚠ 最后那条要 `PYTHONIOENCODING=utf-8`，否则 Git Bash 里是一屏乱码（同 `curl -d` / `psql -c`
那个 GBK 老病）。

另有**四条是「探针内部含离线段」**、不是整条离线 —— 数据库连不上时它们不会跳过硬跑，
而是**那些段恰恰只在连不上时才需要成立**：`probe_vision.py` ①、
`probe_conv_store.py` ①②⑨⑪、`probe_feedback.py` ①②③④、`probe_web_search.py`（除 ⑪）。

## 两个容易接反的前提

- ⚠⚠ **`probe_rate_limit.py` 必须在 `import bidding_qa.api` 之前改
  `settings.rate_limit_per_minute`** —— 限额是 `api.py` 的**模块级** `_limits`，import 期算一次
  就定死了。⑤ 验「0 = 关闭」靠 `importlib.reload(api)` 重算。
- ⚠⚠ **`probe_conv_store.py` / `probe_feedback.py` 的离线断言跑在 `warmup()` 之前**
  —— 这正是它们的 `main()` 与 `probe_pg_store.py` **不同构**的原因：后者一旦连不上就 `return 1`，
  那几条在那里**永远跑不到**。

## 红了但不是回归（已知项，别去修）

| 现象 | 真相 |
| --- | --- |
| `probe_tools.py` ⑤ / ⑤b 打 `[SKIP]` | **不是 FAIL**。那两条是 DeepSeek 服务端特有的约束，判据按**实际供应商**分叉（`type(llm).__name__`，不是 `settings.llm_provider`） |
| `probe_graph_tool.py` 用例 ①「中国科学技术大学采购过哪些项目？」偶发不过 | **重叠区**（图谱与采购库都能答），实测 5 次 **3:2**。CLAUDE.md 明写「别为了让它变绿去改提示词路由」。看**长期**是否明显偏离 50/50，不看单次 |
| `agent_eval.py` 的 ag-08 单题翻转 | 联网题，判分取决于模型这次调没调工具，而预联网是白送的。**单题分数一律不作数** |

⚠ 单题噪声底实测 **±0.28**、均值 **±0.005**（`eval_rag` 连跑两次的实测）。
看到单题分数变了先怀疑噪声，别当回归 —— 那是本仓库最容易误判的一件事。
