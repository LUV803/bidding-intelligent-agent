---
name: diagnose-capability
description: 「服务在跑，但这个能力不工作」的排查手册 —— /api/health 每个字段到底证明了什么、哪些字段刻意不触发连接所以会骗人、30 秒冷却与自愈路径、哪些能力刻意不受 _require_ready 挡、启动配置体检在哪看、以及服务侧零依赖的排查命令。Use when 用户说「图谱没反应」「health 说不可用但 PG 明明起来了」「联网开关点了没用」「历史没同步」「工具一个都没调」「明明配了却不生效」。
---

# 「服务在跑但这个能力不工作」排查

⚠ 起停服务本身看 `start-services` skill。**本文只管一件事：怎么从信号里读出真原因。**

---

## 先分清：三个信号源各能回答什么

| 信号源 | 回答什么 | 不能回答什么 |
| --- | --- | --- |
| `/api/health` | 服务进程的状态 + 各可选能力的**配置**与**最近一次已知结果** | 「这次用哪家供应商」「用户开没开联网」「能力现在真能用吗」（见下） |
| **启动配置体检**（stderr 最前面几行） | 哪些可选能力没开 + **为什么** + `.env` 里有哪些键被静默丢弃 | 运行期变化 |
| `/api/config`（`api.py:254-266`） | 模型名 / 嵌入模型 / top_k / 重排配置 / Qdrant 模式 | 同样没有请求上下文 |

⚠ `/api/config` **不调 `_require_ready()`**，所以服务初始化期间也回 200 —— 它比 health 更适合
在 `_bootstrap` 还没跑完时确认「后端进程活着、配置读到了」。

---

## ⚠⚠ `/api/health` 每个字段到底证明了什么

`HealthResponse` 共 29 个字段（`schemas.py:363-411`，构造在 `api.py:221-251`）。
**可选能力那几组都是三件套**：`X_configured` / `X_available` / `X_detail`。读法：

| 组 | `configured` 为假 | `available` 为假但 `configured` 为真 |
| --- | --- | --- |
| `graph_*` | `NEO4J_URI` 空（**或没装 neo4j 驱动**） | 见下「它没试过」 |
| `pg_*` / `history_*` | `POSTGRES_HOST` 空（**两个能力共用这一个开关**） | 同上 |
| `vision_*` | `ZHIPU_API_KEY` 空（**或没装 SDK**） | 见下「不是网络探测」 |
| `web_*` | `TAVILY_API_KEY` 空 | 同上 |

### ⚠⚠ 坑一：`available: false` 很可能只是「**还没试过**」

`graph_status()`（`graph_store.py:777-793`）与 `pg_status()`（`pg_store.py:785-805`）的
docstring 里都写着一句话：「**不抛异常、不触发连接**」。两个 store 都是**懒连接**
（`_conn is None` / `_driver is None`），所以**服务刚起来、还没人问过任何问题时**，
`detail` 就是：

```
尚未建立连接
```

⚠ **这不是「连不上」，是「还没连过」。** 拿它当预检会得出假警报 ——
`eval/agent_eval.py` 为此专门先真 `warmup()` 一次再报状态。
同理，`detail` 里出现 **`环境错误 / 认证失败 / connection refused` 之类的原文**才是真的失败。

### ⚠⚠ 坑二：自愈发生在**下一次真实查询**，不在重启

`_get_conn()` 成功连上的分支（`pg_store.py:372-373`）会把 `_failed_at` 与 `_last_error`
**清空**。所以：

> **数据库后来起来了 ⇒ 不用重启后端。** 下一次真去打一个采购库问题，它就自己好了，
> `pg_available` 当场翻真。

⚠ 本会话端到端验过：PG 从 STOPPED 起来之后，第一发真实查询（`ag-07`）直接成功，
陈旧的 `available: false` 自行纠正。**先别急着重启后端** —— 重启会白白丢掉索引/模型的加载。

### ⚠⚠ 坑三：失败要等 **30 秒冷却**，且这 30 秒里它**每次都秒失败**

`_FAIL_COOLDOWN = 30.0`，两个 store **各自独立定义**（`pg_store.py:90` / `graph_store.py:235`），
不是共享常量。冷却期间 `detail` 会带上 `，Ns 后重试` 后缀。

⚠ 这是刻意的：既不能永久记住失败（PG 可能比服务晚起），也不能每次重试（URI 不可达时每次都要
耗掉一个 `connection_timeout`，模型连调两次工具就是用户干等两分钟）。
**所以「刚修好数据库就立刻提问仍失败」是正常的，等 30 秒或者换个问题再打一发。**

### ⚠⚠ 坑四：`vision_available` / `web_available` **不代表做过网络探测**

健康检查每 30 秒轮询一次，不可能每次真调一次视觉 API / 真搜一次 Tavily（既慢又花钱）。
它们读的是「配了 Key」+「**最近一次真实调用**没失败」—— 刚起服务时**偏乐观**。

⇒ **别按它给按钮置灰**（前端就是这么做的：附件按钮不置灰，让用户点了拿到真实中文报错）。
⇒ 反过来，`available: true` 之后**第一次**调用仍可能失败，那才是真结果。

### ⚠⚠ 坑五：`llm_providers` 是**配置**，不是心跳

`local_client._LocalLLM.ready`（`:48-57`）末行是 `return bool(self.base_url.strip())` ——
**纯读配置、不发请求**。

⇒ 胶囊里出现 vLLM **只说明 `.env` 填了地址**，不说明 vLLM 进程活着。服务没起来的症状是
**提问时报连接错误**，不是 health 变红。要判进程死活就真打端点
（`curl -s 127.0.0.1:8001/v1/models`）。

⚠ `available_providers()` 只列 `ready` 的，且**读活的 `settings`**（不是 import 期快照）。

### ⚠⚠ 坑六：`llm_provider` / `llm_model` / `llm_ready` 描述的是**服务端默认**那家

健康检查**没有请求上下文**，而供应商是每请求可变的（前端那个胶囊）。同
`web_configured` vs `ChatRequest.web_search` 是同一条限制（`api.py` 里有先例注释）。
**用户选了 GLM 而 GLM 有问题时，这三个字段一点忙都帮不上。**

---

## 哪些能力**刻意**不受 `_require_ready()` 挡

`_require_ready()`（`api.py:190-195`）只被**三个**端点调用（`:272` / `:557` / `:727`，
即 `/api/search`、`/api/chat`、`/api/agent`）。**其余全部不受它挡**，且都是**故意的**：

| 端点 | 为什么不受挡 |
| --- | --- |
| `/api/health` | 它本身就是报状态的那个 |
| `/api/config` | 同上，初始化期间也要能读配置 |
| `/api/vision` | 图片解析不碰 Qdrant/BM25，挂在索引加载上等于让独立能力被核心链路拖死 |
| `/api/conversations*`（6 个） | 索引 + 向量模型加载要几十秒，跟着 503 的话**首屏历史会消失几十秒**，而 localStorage 版本是秒出的 —— 那是明确的退步 |
| `/api/feedback*`（3 个） | 同上，反馈不碰 Qdrant |

⚠⚠ **由此引出一条最容易误判的排查线索**：重启后端后头几十秒里，`/api/health` 是 **503**，
而**历史其实是好的**。所以：

> **前端的「未同步」提示读的是 `listConversations()` 那一次调用的成败，
> 不是 `health.history_available`。** 按 health 判断会让侧栏在那几十秒里谎报「未同步」。

⚠ 同理，健康检查的 `status` 字段是 `"ok" if state.ready else (state.error or state.stage)` ——
**它不是布尔**。判「后端可用」要判 `=== "ok"`，**不是 HTTP 200**（`_bootstrap` 期间 health
回 503，那时提问会失败）。

---

## 三态纪律：数据还没测出来时，一个字都不该多说

前端几处开关都用**三态**（`null` = 还没结论）。**别把它简化成布尔** —— 那会让界面在真相未明
时说一句错话：

| 字段 | `null` 的含义 | 读错的表现 |
| --- | --- | --- |
| `page.tsx` 的 `webConfigured` | health 还没回来 | 首屏谎报「未配置 TAVILY_API_KEY」 |
| `useChat` 的 `historySynced` | 还不知道能不能同步 | 侧栏谎报「未同步到数据库」 |
| `llm_providers` | 还没取到 | 「未配置」的提示先于配置结果出现 |

⚠ `historySynced` 从 `useChat` 经 `page.tsx` 传给 **`Sidebar`**（不是 `ChatInput`，别照着开关那
条路径抄）。

---

## ⚠⚠ 症状 1：「模型一个工具都不调」先查 `supports_tools`

```python
# agent.py:253
tools = tools_for(enable_web) if getattr(llm, "supports_tools", True) else []
```

为假时**整张工具表被剥空**，退化成「预检索 + 直接作答」—— 界面上**完全看不出**这是配置还是
模型行为（`agent.py:372` 那句 `bool(tools)` 就是为这一格加的）。

```bash
uv run python -c "from bidding_qa.config import settings; print(settings.ollama_supports_tools)"
```

⚠ 消费方一律 `getattr(llm, "supports_tools", True)` —— 探针里的假 LLM 是鸭子类型。

---

## ⚠⚠ 症状 2：「`.env` 里明明写了，代码就是读不到」

**本仓库踩过四次的坑**（Neo4j / Postgres / Zhipu / Tavily）：
`Settings` 是 pydantic-settings 且 `extra="ignore"` ⇒ **没声明的键被静默丢弃**。

### 2026-09-21 起有自动防线了：**启动配置体检**

`config.py` 末尾的模块级 `settings = get_settings()` 会触发 `_report_startup_config`
（`config.py:456-480`），它把 `_config_findings()`（`:89-119`）的每一行**以 WARNING 念出来**。

**它长什么样、在哪看**（这条最实用）：

- ⚠ 跑在**任何 `logging.basicConfig` 之前**（`config.py` 是 import 期，`api.py:49` 的
  basicConfig 还没执行），那时只有 WARNING 能经 `logging.lastResort` 落到 stderr。
- ⇒ 这几行**没有时间戳、格式光秃秃的**，且**排在所有带格式的日志之前**。这是**故意的**：
  早于一切、绝不漏掉。⚠ 所以别去日志**尾部**找它，去看**最开头**。
- ✅ **配置齐备的机器上它一行都不输出**（本机当前 `.env` 就是 0 行）。刷一屏「一切正常」的日志
  等于把体检变成噪声。

它报三件事：

1. **未启用的可选能力**（`_OPTIONAL_CAPS`，`config.py:32-38`）—— 逐项点名 + 缺哪个 Key + 会失去什么：
   `知识图谱` / `采购记录库 / 对话历史` / `联网搜索` / `图片解析`。
2. **没有任何文本大模型可用** —— 判据是 `_TEXT_LLM_SOURCES` **四个字段全空**
   （`DEEPSEEK_API_KEY` / `ZHIPU_API_KEY` / `VLLM_BASE_URL` / `OLLAMA_BASE_URL`）。
   ⇒ 会退化成 `fallback_answer`（**把知识库原文端出来，不是模型答案**）；
   而 `eval_rag` 那类脚本拿它去算 faithfulness 会得 1.0 —— 一整轮数字全废。
3. ⚠⚠ **`.env` 里未声明的键**（`_undeclared_env_keys()`，`:61-86`）—— **这是最值钱的部分**。
   逐个点名，处理方式是「要么补字段、要么删键」。

⚠⚠ **它只念不拦，绝不 `raise`** —— 这是设计，不是偷懒：四个可选能力全靠「没配就降级」活着，
`.env.example` 交上去时那几个 Key 本来就是空的，`raise` 等于「谁都没配 Key 就起不来服务」。
**别顺手把它改成校验。**
⚠ 也别指望它在运行期重跑：`mode="after"` 只在构造时执行一次（`settings.X = …` 这类属性赋值
**不触发**它）—— 探针们正是靠这一点改运行期行为。

⚠ 但体检**只是兜底提醒，不是「可以不用改三处了」**：声明缺失的键仍要改 `config.py`。

---

## 症状 → 先查哪里

| 症状 | 先查 | 常见真原因 |
| --- | --- | --- |
| 图谱/采购库「不可用」 | `detail` 原文 | 「尚未建立连接」= 还没试过（不是故障）；有错误原文才是真失败 |
| 刚修好数据库但仍失败 | 冷却后缀 `/30s` | 等冷却过，或直接打一发真实查询触发自愈 |
| 联网开关点了没用 | `web_configured` | 服务端没配 `TAVILY_API_KEY` —— 此时**不发 error、按钮不置灰、界面毫无变化**，只得到一次没有网页条目的普通回答 |
| 「没搜到」还是「压根没搜」 | 后端日志 `agent.web_lookup()` 那行 | `ok=` / `原始=N` / `闸门=` / `新增=` 四段分得开四种失败（0 条 / 闸门整批丢 / 全判成已知 / 重试没救回） |
| 工具一个都没调 | `ollama_supports_tools` | `supports_tools=False` 剥空整表 |
| `.env` 改了不生效 | 后端**重启**了吗 | `ENABLED` 那批常量是 **import 期**算死的，热改不生效 |
| 历史没同步 | `listConversations()` 的成败 | ⚠ **不是** health 的 `history_available`（见上） |
| 用户说某个模型答得不对 | ⚠ health 帮不上 | 那是每请求的，health 只报默认那家 |
| 后端起来但首页示例卡片是灰的 | health 的 `graph_*` / `pg_*` | 前端拿它给卡片置灰 |

---

## 服务侧零依赖排查命令

⚠ 容器里**没有 `ss`**（用 bash 内建 `/dev/tcp`）。命令与 `start-services` 同一套：

```bash
# 后端活着吗、什么状态（⚠ 判 status == "ok"，不是 HTTP 200）
curl -s localhost:8000/api/health | python3 -m json.tool

# 只挑可选能力那几组看（避免 29 个字段刷屏）
curl -s localhost:8000/api/config

# 启动配置体检 —— 在日志【最前面】，没有时间戳的那几行
head -40 /root/autodl-tmp/backend.log

# 联网的四段真相（排查「开了联网却没有网页内容」的唯一线索）
grep "联网搜索 ok=" /root/autodl-tmp/backend.log | tail -20
```

⚠ 打中文请求体的坑（Git Bash 按 GBK 发出去 → 400）见 `curl -d` 那条 ——
中文一律写成 UTF-8 文件后用 `--data-binary @file.json`。

---

## 改完必跑

```bash
uv run python scripts/probe_config.py       # 28 条，**全离线**（改 config.py 的 Settings/_OPTIONAL_CAPS/校验器之后）
uv run python scripts/probe_llm_provider.py # 64 条，**全离线**（改 health 的供应商字段之后）
uv run eval/dashboard.py --selftest         # 28 条，**全离线**（不用停后端；读线上总账）
```

⚠ 三个都是**全离线**的，随手就能跑。真要动 health 的字段，记得 `probe_rate_limit.py` 里还有
一条断言钉着「`/api/health` 整个豁免限流」。

## 两条一直成立的硬约束（别在这条链路上踩）

- **后端与 `ingest` / `eval_rag --retrieval live` / `eval/agent_eval.py` 互斥** —— 本地 Qdrant
  是嵌入式文件模式、同目录独占锁。⚠ 但 `eval/dashboard.py` **不用停后端**（它只读 PostgreSQL）。
- ⚠ **`API_HOST` 默认 `0.0.0.0` = 绑定所有网卡，而本服务没有任何认证** —— 局域网里谁都能问，
  而 `web_search` 是**请求参数**，别人能白白烧掉 Tavily 额度。CORS 拦不住（`curl` 不理会），
  限流也只是缓解。**只在本机用就把它改成 `127.0.0.1`** —— 这是比限流正确的修法，也是一行。
  ⚠ 这条**改动需要用户拍板**，别自作主张。
