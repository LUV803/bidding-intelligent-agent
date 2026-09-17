---
name: add-provider
description: 接一家新的文本大模型供应商（OpenAI 兼容端点或本地部署）—— 三步接线、_NOT_CONFIGURED 与 _KEY_FIELD 两处必改、supports_tools 为假会把整张工具表剥空、本地慢供应商的 60 秒超时、以及按请求切换的缓存与胶囊顺序。Use when 用户说「接个新模型」「换一家供应商」「把本地部署的模型接进来」「加个 vLLM/Ollama 之外的」。
---

# 接一家新的文本大模型供应商

## 先搞清形状：一个共享实现 + N 份配置映射

⚠ 两家客户端**不是两套 HTTP 实现**。DeepSeek 走 `openai` SDK，**GLM 也是 OpenAI 协议兼容**，
所以真实形状是：

```
clients/base_client.py      BaseLLMClient（只声明契约的 ABC）
                            OpenAICompatLLM（≈200 行流式拼装，两家共用）
                            _MarkupSuppressor / strip_tool_markup
clients/deepseek_client.py  DeepSeekLLM  ← 只做一件事：从 settings 读自己那份配置
clients/zai_client.py       ZaiLLM
clients/local_client.py     _LocalLLM → VllmLLM / OllamaLLM（本地两家）
clients/llm_factory.py      按名字取客户端，**每家一个缓存实例**
```

**新供应商只要是个 OpenAI 兼容端点，就继承 `OpenAICompatLLM` 把配置映射成 property 即可**
（`clients/` 里那 200 行一行不用动）。契约（`stream` / `stream_with_tools` / `complete`）不变，
`_MarkupSuppressor` 也在基类里共用。

⚠ **`BaseLLMClient` 是只做契约声明的 ABC，别给它加运行期关卡**。它唯一的消费方是**类型标注**
—— 探针里的假 LLM 全是鸭子类型、不继承它，加 `isinstance` 校验会把那些替身全挡在门外。
同理 `vision_client` **不继承它**（同步、`analyze() → VisionResult`、走 `zai-sdk`，
与 `stream` 契约没有共同接口）。

---

## 要改的地方

| # | 位置 | 改什么 | 漏了的表现 |
| --- | --- | --- | --- |
| 1 | `clients/xxx_client.py` | 写 `OpenAICompatLLM` 子类（或本地：`_LocalLLM` 子类） | — |
| 2 | `llm_factory.py:38-45` | `_PROVIDERS` 加一条 | — |
| 3 | `llm_factory.py:50-55` | `_LABELS` 加一条 | 胶囊显示成键名（`zai` 而不是 `GLM`） |
| 4 | 子类里写**自己的** `_NOT_CONFIGURED` | 点名缺哪个键 | 降级提示退成中性的「未配置 API Key，」 |
| 5 | `probe_llm_provider.py:152` | `_KEY_FIELD` 加一行（**仅云服务**） | 那条「抹掉 Key 它必须消失」的断言**静默跳过** |
| 6 | `config.py` / `.env` / `.env.example` | 三处一起加配置项 | 本仓库记过四次的静默覆盖 |

⚠⚠ **第 2 步一律追加在 `_PROVIDERS` 末尾**：`available_providers()` 按 dict 声明序输出，
而那个顺序**就是前端胶囊的循环顺序**（`ChatInput.cycleLlm()` 对它取模）。中间插队会改变用户
已经习惯的点击次序。

⚠ `_DEFAULT_FALLBACK`（`:58`）= 名字认不出来时回退到哪家。**遇未知值降级 + 记 WARNING，不抛**
（同 `QDRANT_MODE=auto` 的惯例）—— `LLM_PROVIDER=zaip` 这种拼错若抛异常，服务直接起不来。

---

## 子类必须实现什么

`OpenAICompatLLM`（`base_client.py:305`）上一共有 4 个**无默认实现的抽象 property**
（`:331-345`）：`api_key` / `base_url` / `default_temperature` / `default_max_tokens`。

⚠ 漏了任何一个，**实例化那一刻就会 `TypeError: Can't instantiate abstract class`** ——
这是**响的**失败（`available_providers()` 会遍历所有供应商建实例，所以第一次 `/api/health`
轮询就炸），不用担心静默。

⚠ 但这条规则**来自一个真实的静默 bug**，写新供应商时要知道：改造前 `temperature` / `max_tokens`
的默认值是**直接读 `settings.deepseek_*`** 的，于是新供应商漏填就是**悄悄拿到 DeepSeek 的
预算**——在 GLM 上不会报错，只是行为不对。ABC 现在把这类漏填挡在门外了，
**但别在这里加回任何「兜底默认值」**，那等于把那个 bug 请回来。

⚠ `BaseLLMClient`（`:244`）另有 `model`（`:253`）/ `ready`（`:258`）/ `stream`（`:276`）/
`stream_with_tools`（`:286`）/ `complete`（`:300`）五个 abstract，**继承 `OpenAICompatLLM`
的话它们都已经有实现了**，只需再填上面那四个 property。

### `_NOT_CONFIGURED`：必须写自己那份

`base_client.py:320-321` 的默认值是中性话「未配置 API Key，请在 .env 中填写后重试。」，
`rag._no_key_hint()`（`rag.py:561-579`，调用点 `:608` / `:618`）是**从 `llm._NOT_CONFIGURED`
里切出键名**的：

```python
return f"{str(getattr(llm, '_NOT_CONFIGURED', '')).split('，', 1)[0].strip()}，" if (
    getattr(llm, "_NOT_CONFIGURED", "")
) else "未配置 API Key，"
```

⚠ 所以文案的**形状必须**是「`未配置 XXX_API_KEY，` 后面接别的」——它在**第一个中文逗号**处切。
写漏了不指错，但也不具体（退化成中性话）。

⚠⚠ 这条是**真机撞出来的**：原先两处写死「未配置 `DEEPSEEK_API_KEY`」，只有一家供应商时是对的；
加了每请求供应商之后，选了 GLM 而智谱 Key 没配的用户，会被提示去查一个**本来就配好了**的键。
**写新供应商时别重蹈**：`ZaiLLM` 那句（`zai_client.py`）就是范本。

---

## 本地的两家（vLLM / Ollama）：三处与云服务不同

参考 `local_client.py`（`_LocalLLM` 在 `:30`）。三家共用一个中间基类，差异只在：

| | 云服务（DeepSeek / GLM） | 本地（vLLM / Ollama） |
| --- | --- | --- |
| `ready` 判据 | `bool(api_key.strip())`（基类默认） | **`bool(base_url.strip())`**（`_LocalLLM:48-57` 覆写） |
| `api_key` | 真 Key | 静态占位符 `"EMPTY"`（`:38-46`）—— 本地不校验，但 `AsyncOpenAI` 拒空串 |
| `_KEY_FIELD`（探针） | **要加一行** | **绝对不能加**，见下 |
| `supports_tools` | 默认 `True` | Ollama 覆写成 property 读 `settings.ollama_supports_tools` |

⚠⚠ **`_KEY_FIELD`（`probe_llm_provider.py:152`）的语义是「抹掉 Key 它就消失」**，
而本地的开关是**地址**不是 Key。硬塞进去会让 `victim` 从 deepseek 飘到别家，
那条「抹 Key 必消失」的断言就变成在测另一件事了。`probe_llm_provider.py:169-175` 有原文注释。
⚠ 反过来，**云服务漏加那一行 = 那条断言静默跳过**（`victim` 选不中就打 `[SKIP]`），
而那正是「只列 ready 的」唯一的真凭证。

⚠ `_LocalLLM` **刻意没有自己的 `_NOT_CONFIGURED`** —— 两家子类各自写
（`local_client.py:89` / `:153`），父类那句只是给「真取到抽象中间类」兜底。

### ⚠⚠ `supports_tools = False` 会把**整张工具表**剥空

```python
# agent.py:253
tools = tools_for(enable_web) if getattr(llm, "supports_tools", True) else []
```

`agent.py:372` 那句 `bool(tools)` 就是为这一格加的。提示词侧同理：`rag.build_agent_messages`
的 `tools_ok` 也读它 —— **「提示词提到工具 ⟺ 工具列表里有它」这条不变量由构造成立**，
不靠两处记得同步。

⚠ 唯一的 `False` 是 Ollama 的官方 `deepseek-r1:8b`：它的模板里没有 tools 段，
带 tools 请求会被服务端**直接拒掉**（不是答得差，是一个字都出不来）。
⚠ 新供应商若也接不了工具，**覆写成读活 `settings` 的 property**，别写成类常量 ——
类属性在 import 期定死，改了 `.env` 不重启不生效（`local_client.py:171-179` 的注释记着这条）。
⚠ 消费方一律写 `getattr(llm, "supports_tools", True)`：探针里的假 LLM 是鸭子类型，
硬取属性会把它们全挡在门外。

---

## ⚠⚠ 60 秒超时：本地慢供应商的真实形状

`OpenAICompatLLM.timeout: float = 60.0`（`base_client.py:317-318`）**是每次读取的超时，
不是总超时**；配 `client` property 里那个 `max_retries=1`（`:371`，**字面量，不是配置项**）
= 最坏 120 秒。

- **持续流式输出会重置读取时钟** ⇒ 长回答（实测 500 秒）能活着。
- **冷启动一个字节都不吐** ⇒ 两次 60 秒静默 ⇒ `Request timed out.`

⚠ 实测（2026-09-28，本机 Ollama + qwen3.5:9b 钉 CPU）：**首次**调用
`120642.39 ms` 失败、error 事件是「生成回答失败: Request timed out.」；
**预热后**同一题成功、`501640.41 ms`。

⇒ **跑本地供应商之前先空跑一句热身**（vLLM 首次还可能触发编译）。这是
A/B 流程的前置检查之一，见 `ab-compare` skill。
> 这条**不要顺手去改**：用户已判定为硬件问题（24 GB 卡装不下 9B）。要改也是改
> `timeout` / `max_retries` 两个旋钮，且得先问。

---

## 两条 DeepSeek **服务端特有**的约束（新供应商多半没有）

| 约束 | DeepSeek | GLM-4.7-FlashX |
| --- | --- | --- |
| 具名 `tool_choice` + 思考 → 400 | ✅ 有 | ❌ 没有 |
| 带 `tool_calls` 的 assistant 消息缺 `reasoning_content` + 下一轮开思考 → 400 | ✅ 有 | ❌ 没有 |

⇒ `probe_tools.py` 的 ⑤ / ⑤b **按实际供应商分叉**：判据是 `type(llm).__name__`
（**不是** `settings.llm_provider` —— 工厂拼错时会静默回退，以实际用的为准）。
非 DeepSeek 时打 `[SKIP]` 而**不是 FAIL** —— 不分叉的话「换个 `LLM_PROVIDER` 再跑一遍探针」
这条验证步骤会稳定给出两条假 FAIL，把真问题淹掉。

⚠ 但 `_assistant_tool_message()` **仍然无条件回传 `reasoning_content`**（空串也带）。
那是为**跨供应商统一**：多回传一个服务端不认的字段不会被拒，少回传则换个供应商就 400。
**别因为新供应商不要求就删。**

⚠ 「强制那一轮必须关掉思考」那件事（`extra_body={"thinking": {"type": "disabled"}}`，
`enable_thinking=False` **不生效**）也是 DeepSeek 侧的约束。它正是上面第二条的成因：
关思考的轮次没有 `reasoning_content`，若按「这轮出现过 reasoning 才回传」来判，字段被整个丢掉，
下一轮开着思考就 400。**这条踩过两次，别再踩第三次。**

---

## 按请求切换（第二个「每请求可变」的维度）

| | 进程级 | 请求级 |
| --- | --- | --- |
| 文本供应商 | `settings.llm_provider`（`.env` 的 `LLM_PROVIDER`）—— 现在只是**默认** | `ChatRequest.llm_provider`（前端那个胶囊） |
| 联网搜索 | `web_search.ENABLED`（有没有 Key） | `ChatRequest.web_search` |

⚠ **与联网那对的形状不同，别照抄**：联网是两个维度**取交集**（没 Key 时开关打开也不搜）；
供应商是**一维覆盖** —— 名字认得出来就照办，**没配 Key 就如实报没配，绝不悄悄换成另一家**。
静默换模型 = 用户拿到一个他没要的模型的答案，比报错糟得多。

三条实现纪律：

- **按名缓存**（`_llms`，`:62`），不是每次 `cls()` 新建 —— 缓存住实例也就缓存住了它懒建的
  `_client`（`base_client.client` property），**连接池不丢**。
- ⚠ **归一化只在 `resolve_provider`（`:65-86`）里做一处**。`api.py` 的两处调用点是
  `get_llm(payload.llm_provider or None)` —— **空串 → `None` → 默认**，这个名字**照传不归一化**
  （大小写/空格在 `get_llm` 里处理）。两处都归一化就会有两套口径。
- ⚠ 未知名字也**照传**，降级发生在 `resolve_provider` 里 —— handler 不预先过滤，
  否则「未知值降级」那条就没有真机路径了。

⚠ `available_providers()`（`:109-124`）**只列真配了 Key 的**（`ready` 为真）：名字合法但没配 Key
的点了必然失败，摆出来是骗人。前端因此**只在 ≥2 家时才渲染**那个胶囊。
⚠ 这条是**读活的 `settings`**，不是 import 期快照。

---

## 改完必跑

```bash
uv run python scripts/probe_llm_provider.py   # 64 条，**全离线**（不需要任何 Key / 服务 / 网络）
uv run python scripts/probe_tools.py          # 真机：tool_calls 拼装 + reasoning_content 回传（需 Key）
uv run python scripts/probe_quality.py        # ⚠ 真机烤一遍 agent（需服务）—— 见下
```

⚠ **`probe_llm_provider.py` 是唯一不要 Key 的**，先跑它。切换着测不用改文件：

```bash
LLM_PROVIDER=zai uv run python scripts/probe_tools.py
LLM_PROVIDER=deepseek uv run python scripts/probe_tools.py   # 对照
```

⚠ 两条**已知局限**（在 `probe_llm_provider.py` 的注释里，**别当成 bug**）：
① `/api/health` 只能报**默认**那家 —— 它没有请求上下文（同 `web_configured` vs
`ChatRequest.web_search` 那条限制）；
② 切换只影响**之后的提问**，已有消息仍是旧模型产出的，界面上不标注是哪家答的。
历史只回传 `{role, content}`（不带供应商痕迹），所以**一个对话里混用两家是安全的** ——
带 `tool_calls` 的 assistant 消息只存在于**单次请求内部**的工具循环里，从不落库、从不回传。

⚠ **改 `ChatRequest` / `HealthResponse` 时注意 `llm_provider` 必须带默认值**：
`probe_web_search.py` 直接构造 `ChatRequest(question=…, …)` 而**不传**这个字段，
漏了默认值那条探针会直接红。
