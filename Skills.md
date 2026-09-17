# 这个项目适合增加哪些 Skills

> **结论**：6 个候选，按价值排序。前三个值得优先做。
> ⚠ 全部**不改变产品运行时行为** —— skill 是「写给我看的说明书」，不是给系统装的插件。
> 它省的是**每轮重新交代背景**的通信成本，加不了任何产品功能。

---

## 0. 判据：什么才配做成 skill

三条同时成立才做：

1. **重复发生** —— 一年会做十次以上
2. **有静默失败模式** —— 做错了不报错，只是结果不对（本仓库最贵的一类）
3. **知识不在代码里**，或散在 2898 行 / 241 KB 的 `CLAUDE.md` 里翻不到

反面（都不该做）：一次性的、做错会当场炸的、代码本身就写得很清楚的。

⚠ 还有一个前提：**`CLAUDE.md` 每个 session 都进上下文，skill 的正文只在被调用时才读**。
所以 skill 的真正价值是「**按触发场景重新打包**」，不是装新知识 —— 绝大部分内容
`CLAUDE.md` 里已经有了，问题是没人能在需要的那一刻从 2898 行里翻出来。

---

## 1. 已有的四个（别重复造）

| skill | 覆盖 | 边界 |
| --- | --- | --- |
| `start-services` | 起停服务：顺序、等待判据、停服、4 个静默坑 | 命令留在 `docs/`，它只写「按什么顺序、怎么算成功」 |
| `deploy-sync` | 本机 → 服务器：打包 / scp / 解包 / 重启 / 验证 | 含两个静默风险（`.env` 覆盖、`.env.bak` 不挡） |
| `probe-regression` | **改了什么 → 跑哪个探针**（19 条对照表） | 验契约不验语义；前端改动它只指 `npm run build` |
| `ab-compare` | vLLM vs Ollama 的工具调用 A/B（3 句固定小样） | 目前只覆盖这两家、这一件事 |

---

## 2. 推荐增加

### ① `new-tool` —— 新增 / 修改一个 Agent 工具 ★★★

**触发**：「加个工具」「新工具」「改一下工具的参数」「模型不调我新加的工具」

**为什么值得**：本仓库最典型的**多处联动 + 静默失败**改动。CLAUDE.md 只写了「四处要改」，
但没写**改错的表现**，而这些表现全是静默的 —— 模型调了、界面上什么都不显示；
或者参数被判 0 票整条丢弃。

**内容骨架**：

- **四处改动**（Pydantic 化之后是四处，不是当初那六处）：写 `BaseTool` 子类并 `register()`
  → 决定进 `TOOL_SCHEMAS`（进程级）还是由 `tools_for()` 拼（每请求）→ `rag.py` 提示词片段
  → `frontend/components/ToolCallBlock.tsx` 的 `TOOLS` 表
- **有结构化结果才要**的第五处，是最容易漏的一条五连：
  `ToolOutcome` 加字段 → `tool_result` 载荷 → `lib/types.ts` → `useChat.ts`（含 `newCount` 守卫）
  → `ChatMessage.tsx` 的 `steps.map`。⚠ 漏了的表现是「模型调了但界面上一片空白」
- ⚠⚠ **参数名雷区**（最值钱的一节）：别用别的工具已占的 `query` / `top_k` / `query_type` / `entity`；
  别用会被 `_PARAM_ALIASES` 改写成 `entity` 的 `keyword` / `keywords` / `entity_name`；
  `limit` / `count` 这种泛用词是泄漏标记的「票仓」，也没用（所以叫 `max_rows` / `web_query`）。
  判据是 `_match_schema` 的**简单多数投票，0 票或并列即整条丢弃**
- ⚠⚠ **类型写法**：`str | None` 正常走 Pydantic；但 `top_k` / `max_rows` / 枚举一律
  `Any` + `json_schema_extra`。用 `int` + `ge/le` 会把「夹紧到上限」变成 `ValidationError`；
  用 `Literal[...]` 会把 `graph_store` 那句**带可用值列表的中文报错**换成 Pydantic 英文 422。
  不用 `int | None` 是因为 `model_json_schema()` 产出 `anyOf`（无顶层 `type`）会让清洗层
  **把恢复路径来的字符串原样收下** —— 不报错、不崩溃，只是类型变了
- ⚠ 进程级 / 每请求**不能塞错**：往 `TOOL_SCHEMAS` 里加每请求的东西，`probe_pg_store.py` 会红
- ⚠ 改完要同步扩 `probe_enum_route.probe_tool_matrix()` 一格（它逐组合验「提示词提到工具 ⟺ 工具在场」）
- ⚠ 改名：旧名**彻底注销不留别名**；探针夹具用的是「看着像却不是」的幻觉旧名，别顺手改成新名
- **跑**：`probe_recover.py`（先看用例 0 / 0b）+ `probe_quality.py` + `probe_enum_route.py`

---

### ② `add-provider` —— 加一家文本大模型供应商 ★★★

**触发**：「接个新模型」「换一家」「加个 OpenAI 兼容的端点」「本地部署的模型接进来」

**为什么值得**：CLAUDE.md 已经列了「加第三家要做的三步 + 漏一条的表现」，
但**本次实测又发现两条它没写的**，而且两条都是静默的。

**内容骨架**：

- **三步**：`clients/` 写 `BaseLLMClient` 子类 → `_PROVIDERS` 加一条 → `_LABELS` 加一条
  （漏了胶囊显示成键名）
- ⚠ 必须写自己的 `_NOT_CONFIGURED` —— `rag._no_key_hint()` **从它里面切键名**，
  漏了只会退成中性的「未配置 API Key，」（不指错，但也不具体）
- ⚠ 必须实现 `default_temperature` / `default_max_tokens` —— 漏了**静默拿 DeepSeek 的默认值**
  （在该供应商上不报错，只是行为不对）
- ⚠ `probe_llm_provider.py` ③ 的 `_KEY_FIELD` 映射加一行 —— 漏了那条「抹掉 Key 后它必须
  从列表消失」的断言会**静默跳过**，而它是「只列 ready 的」唯一的真凭证
- ⚠⚠ **`supports_tools=False` 会把整张工具表剥空**（`agent.py:253` 的
  `tools = tools_for(...) if getattr(llm, "supports_tools", True) else []`），
  **从输出上完全看不出来** —— 本地小模型常不支持 tools，这是本地供应商的默认坑
- ⚠⚠ **本地 / 慢供应商要额外想 timeout**（本次 2026-09-28 实测，CLAUDE.md 里还没有）：
  `base_client.py:318` 的 `timeout=60.0` 是给云端 API 定的，`max_retries=1` 让它变成 120 秒。
  Ollama 冷启动实测 **501.6 秒**且**全程零输出** ⇒ 两次 60 秒静默直接撞穿，
  用户看到「生成回答失败: Request timed out.」。
  ⚠ 注意 `timeout` 判的是**每次读取**的间隔（持续吐字就一直续命），所以只有**冷启动**必炸
- ⚠ `.env` / `.env.example` / `config.py` **三处一起改**（本仓库记过四次）
- **跑**：`probe_llm_provider.py`（64 条全离线）+ `probe_tools.py`

---

### ③ `diagnose-capability` —— 「服务在跑，但这个能力不工作」★★★

**触发**：「图谱怎么没反应」「health 说不可用但 PG 明明起来了」「联网开关点了没用」
「历史没同步」「附件按钮报错」

**为什么值得**：这是本仓库**最容易读错**的一类信号。本次就撞上一次 ——
PG 是我手动 `service postgresql start` 起来的，`/api/health` 里 `pg_available` **仍然是 false**，
而实际上**下一次真查询就自愈了，根本不用重启后端**。读错一次就会白白重启一遍服务。

**内容骨架**：

- ⚠⚠ **`llm_providers` 不是心跳** —— `local_client.py:49-57` 的 `ready` 是
  `bool(self.base_url.strip())`，**纯读配置、不发网络请求**。胶囊里有某家 ≠ 该进程活着
- ⚠⚠ **`pg_status()` 不触发连接**（`:785-805` 明写「不抛异常、不触发连接」）：
  `_failed_at is not None` 时永远回 `available: false`，而 `_get_conn()` **成功时才清它**
  （`:372-373`）。冷却 `_FAIL_COOLDOWN = 30s` ⇒ **自愈路径是「下一次真查询」，不是重启**
- ⚠⚠ **`_bootstrap` 期间 health 回 503，但那不代表所有能力不可用** ——
  历史 / 反馈 / 图片解析 / 视觉**刻意都不调 `_require_ready()`**（挂了索引加载只会让它们
  在重启后消失几十秒）。用 `/api/health` 判断它们是好是坏会**谎报**
- ⚠ **三态字段**：`web_configured` / `historySynced` / `vision_available` ——
  数据还没出来时**一个字都不该多说**（`0 (—)` 不是 `0 (0%)`）
- ⚠ **启动配置体检那一屏**在哪看、报的三种东西是什么（未启用的能力 / 两个文本 Key 都空 /
  **`.env` 里未声明的键** ← 这条最值钱，`extra="ignore"` 会静默丢弃）
- ⚠ **`ENABLED` 那批常量全在 import 期算死** ⇒ 改 `.env` 必须**重启后端**，热改不生效
- **服务侧怎么查**：`/dev/tcp` 探端口（⚠ 那台容器**没有 `ss`**）、`ps aux` 找 PID、
  `nvidia-smi` 确认 VRAM 真掉下来、`pg_lsclusters` 看 `online`（没有 systemd）
- **跑**：`probe_config.py`（先读它报缺什么）

---

### ④ `frontend-switch` —— 加一个前端开关 / 走通一条 prop 链路 ★★

**触发**：「加个开关」「这个开关首屏没生效」「胶囊点了没反应」「开关点了但请求没变」

**为什么值得**：同一条链路要改**两处**（首屏 `WelcomeScreen` 那条 + 多轮 `ChatInput` 那条），
而漏一处的表现是「首屏没有、多轮有」这种**半生效**。且有几处 TS 检查**帮不上忙**——
CLAUDE.md 里那条「`onSelect={(id, jump) => …}` 少写 `jump` 时 `tsc` 一片绿」就是实例。

**内容骨架**：

- ⚠⚠ **`page.tsx` 有两条路径**，所有底部控件的 prop 都要一路传到底：首屏那条进
  `WelcomeScreen` → 转给 `ChatInput`；多轮那条直接进 `ChatInput`
- ⚠ `llmProvider` 那组是**四个 prop**（`llmProvider` / `llmDefaultProvider` /
  `llmProviders` / `onSelectLlmProvider`），其中**只有 `onSelectLlmProvider` 是函数**
  —— 漏它的表现是胶囊点下去 **`cycleLlm()` 调 undefined、React 抛错、整页白屏**
- ⚠ `run()` 的依赖数组：漏一个 = 「界面说自己切了、实际没切」（**最不直观的一处**）
- ⚠ localStorage **hydrate / persist 是两处**，漏任一处 = 刷新回默认值。
  而 `webSearch` / `agent` 那两键**刻意不持久化**，别再读回来
- ⚠ `npm run build` 与 `npm run dev` **互斥**（共用 `.next/`）：撞上了是「页面出来了但没样式」，
  很容易误判成自己改坏了组件
- ⚠ **阴影 / ring 互斥**：项目的阴影是任意值 `shadow-[…]`，与 Tailwind 的 ring 合成变量
  互斥 —— 同时写 `focus-within:ring-*` 会**静默不显示**
- **跑**：`npm run build`（仓库**唯一**的类型检查入口）

---

### ⑤ `eval-protocol` —— 跑评估三件套 ★★

**触发**：「跑一下评估」「这次改动能打分吗」「数字怎么变了」

**为什么值得**：三个脚本分工不同、前提不同、**成本差两个数量级**，而选错一次就是
几十分钟白跑或几十次 API 调用打水漂。本次就踩过一次：
`agent_eval` **同名覆盖且没有 `--resume`**，回头核对「上次那题多少分」时手上已经什么都没有了。

**内容骨架**：

- **分工表**：`eval_rag`（检索/生成质量，四指标）/ `agent_eval`（**工具路由对不对**，
  四个工具全挂）/ `dashboard`（线上总账，**不碰 Qdrant**）
- ⚠ **只有 `eval_rag --retrieval live` 与 `agent_eval` 要停后端**（本地 Qdrant 排他锁）；
  `dashboard` 随时可跑
- **成本先估**：`eval_rag` 全量 74 题 = 458 次调用 / 6.5 分钟；`agent_eval` 10 题几分钟；
  Tavily 每个 `web: true` 的题 1 credit
- ⚠⚠ **续跑语义两边相反**：`eval_rag` 同配置重跑**接着检查点续跑**（故意的），
  `agent_eval` **同名覆盖、没有 `--resume`** —— 想留两次对比必须 `--out-name`
- ⚠⚠ **噪声底**：均值 ±0.005、**单题 ±0.28**。「优化后某题从 0.5 涨到 0.75」没有意义。
  要看**题面不变时 n（陈述条数）有没有变** —— n 变了才是机制性变化
- ⚠ 两个**结构性判据**别误读：空检索单独成桶（否则拒答会拿 faithfulness 满分）、
  `rag: false` 的题不进均值（否则寒暄题恒 0 分，系统性压分）
- ⚠ **空过会抬高工具准确率**（本次实测 10 题里 5 题没调任何工具）—— 报告每次都点名，读数字前先看它
- **省钱的顺序**：先 `eval_rag --no-llm`（零成本、秒级）→ 再 `agent_eval --check-tools`
  → 最后才决定要不要花 API

---

### ⑥ `recalibrate` —— 换语料 / 换配置后重标定阈值 ★（可选）

**触发**：「换了知识库」「重新 ingest 了」「阈值要不要改」

**为什么值得**：CLAUDE.md 反复写「**针对当前语料实测标定**，换库必须重新标定」，
涉及 `MIN_VECTOR_SCORE` / `MIN_BM25_SCORE` / `MIN_RERANK_SCORE` / `WEB_SEARCH_MIN_SCORE`
四组，外加 `GROUP_COLS`（数据画像决定候选）—— 但**标定过程本身没有脚本、也没有写下来**。

⚠ **但这条有个前提缺口**：真要做这个 skill，最好**先补一个 `scripts/calibrate.py`**
（跑一批相关问题 + 一批无关问题、打印两簇的分布与空带、给出建议值）。
否则 skill 里只能写「怎么取点」，而取点那步全靠手搓 —— 形状参考 `eval_rerank.py`。
`probe_web_search.py` ④ / `probe_pg_store.py` 里的**画像断言**是现成的守卫：
标定变了它们会红。

---

## 3. 不建议做成 skill 的

| 不做 | 为什么 |
| --- | --- |
| **「怎么写提示词」** | 那是产品代码（改 `rag.py`），且已有 `probe_enum_route.py` 兜着。做成 skill 会诱导跳过探针 |
| **「怎么提交 / 怎么开分支」** | 仓库只有 1 个 commit、**没有 remote**，提交与否是用户的决定；写死了反而催我自作主张 |
| **把 `CLAUDE.md` 拆成 skill** | 它每个 session 都要在。拆了就是「每次启动都缺一截」，而且下面那句「哪些键曾经被静默丢弃」类的跨模块事实没有天然归属 |
| **「前端配色 / 样式怎么改」** | 那套 CSS 变量写在 CLAUDE.md 里够了，且改错了**看得见**（不静默）—— 不满足判据 2 |
| **「重建 Neo4j 存储」** | 一次性操作（2026-09-17 已做完），不满足判据 1 |
| **「怎么加一个可选能力」** | 与 ①②③ 大面积重叠，拆开只会三份都写不全 |

---

## 4. 顺带：已有 skill 该补的两处

1. **`ab-compare` 补「60 秒超时」这条实测结论。** 用户已决定不修（判定为硬件/预期），
   但**不记下来下次会重新发现一遍** —— 本次为它花了 8 分钟，还差点把「冷启动超时」
   误读成「Ollama 工具调用不行」。结论三句就够：`base_client.py:318` 是共享的 60s、
   `max_retries=1` 让它变 120s、只有**冷启动**（零输出）会撞穿、**预热一句即可绕过**。

2. **`ab-compare` 的适用面可以泛化。** 现在写死「vLLM vs Ollama × 三句固定用例」，
   而本次证明它的骨架（预检 → 对齐预算 → 固定用例 → 六格记录 → 三条读数纪律）
   **对任何「换一家供应商跑同一批题」都适用**。六格里唯一 provider 相关的只有耗时。

---

## 5. 做的时候的格式契约（本次已用实物核实）

```yaml
---
name: new-tool                  # 必需；逐字 = 目录名；ASCII kebab-case；≤64 字符
description: …                  # 必需；≤1024 字符
---
```

- ⚠ **只写这两个字段**。`allowed-tools` 的确切语义本次**没能核实**（`.venv` 里两个官方
  skill 都没写它）—— 拿不准就不写，省略是合法的。
- ⚠⚠ **只有 frontmatter 常驻上下文** ⇒ `description` 是**唯一每个会话都花钱**的部分，
  要短而准，且把**用户真会打的词**写进去（中英都给）。正文可以放开写。
- ⚠ **`description` 写歪了是静默失效** —— 任务来了我不会去读它，而这件事**不报错**。
  这是唯一需要真机验证的一步：在 `/` 列表里点一次、看它有没有读到该读的东西。
- ⚠ 别在 skill 目录里再套一个带 `SKILL.md` 的子目录（会被当成另一个 skill）。

---

## 附：本次 A/B 的实测结论（已经产出，供 `ab-compare` 补录）

6 格全绿，**路由逐字一致**：

| 用例 | vLLM | Ollama |
| --- | --- | --- |
| ag-05（图谱） | `search_knowledge_graph` · 1 轮 · `ok=True` · 22.6 s | 逐字相同 · 501.6 s（首发 120s 超时） |
| ag-07（采购库） | `search_procurement_records` · 1 轮 · `ok=True` · 29.4 s | 逐字相同 · 456.3 s |
| ag-09（检索） | `search_bidding_knowledge` · 1 轮 · `ok=True` · 31.1 s | 逐字相同 · 333.9 s |

- **零标记泄漏**（`grep 漏进正文 backend.log` 空，判据字符串已在 `tools/base.py` 四处核对）
- **`answer_reset` 在 Ollama 侧同样生效**（ag-09 事件序列里有）
- 唯一系统性差异是**速度 11–22 倍**（CPU vs GPU 的硬件差，不是模型差）
- **工具调用这一层，CPU 上的 9B 与 vLLM 等价**
