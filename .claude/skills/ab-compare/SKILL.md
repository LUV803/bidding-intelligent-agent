---
name: ab-compare
description: 对比 vLLM(GPU) 与 Ollama(CPU) 上同一个模型的**工具调用行为** —— 3 句固定小样、六格记录表、三条读数纪律，全部钉在实测过的 SSE 字段名上。Use when 用户说「A/B 对比」「比一下 vLLM 和 Ollama」「哪个的工具调用更准」「两个供应商对比」，或改完客户端/提示词后要验证两侧行为一致。
---

# A/B 对比：同一个模型的工具调用行为（vLLM vs Ollama）

**比的是「路由对不对 / 收不收口 / 漏不漏标记」，不是速度**（那边的差异是硬件，见读数纪律 3）。

⚠ **这套协议目前不在任何文档里**，只散在会话记录里 —— 所以它整份写在这里，不外指。

## 1. 前置检查（缺一项结论就不成立）

```bash
curl -s localhost:8000/api/health | python -m json.tool
```

- **`llm_providers` 里 vllm / ollama 都在**。顺序 = `_PROVIDERS` 声明序
  （deepseek → zai → vllm → ollama）。⚠ 只列真配了地址的，少一家先回去看
  `start-services` 那节的 `.env` 导入期问题。
- ⚠⚠ **`OLLAMA_SUPPORTS_TOOLS=true`** —— 为假时 `agent.py:253` **把整张工具表剥空**，
  那时「Ollama 什么都不调」是**没给它工具**，不是模型行为。验法见 `start-services`。
  **这一条不查，整个 A/B 的结论都是错的，而报告上完全看不出来。**
- ⚠ **对齐两边的 token 预算**：`.env.example` 里 `VLLM_MAX_TOKENS=8192`、
  `OLLAMA_MAX_TOKENS=4096`，**差一倍**。长回答在 Ollama 侧会**先被截断** —— 那是配置差
  不是模型差，而且从输出上分不出来。对比前把两边调成同一个值。
- ⚠ **确认两边是同一个模型**：vLLM 吃 HF 目录（`/root/autodl-tmp/models/Qwen3.5-9B`）、
  Ollama 吃 GGUF（`qwen3.5:9b-cpu` 是 `qwen3.5:9b` 的派生模型，共享同一份 blob）。
  **别拿 8B 比 9B** —— `docs/AutoDL部署步骤.md` 里至今写的是 `DeepSeek-R1-0528-Qwen3-8B`，
  照它理解就会以为两侧都是 8B。
- **重排关掉**（`rerank: false`）：CrossEncoder 是 CPU 重活，与本就在 CPU 上的 Ollama 抢核。
- **联网关掉**（`web_search: false`）：否则每题多一次 Tavily，延迟被网络主导。

## 2. 三句小样

**直接复用 `eval/agent_cases.jsonl` 的既有用例**，不另造 —— 预期路由**已经定好了**（不用现场
争论什么叫对），结果还能与 `eval/agent_eval.py` 的数字对照。

| 用例 | 问题 | allow / require |
| --- | --- | --- |
| **ag-05** | 交易笔数最多的供应商是哪几家？ | `[graph]` / `require=[graph]` |
| **ag-07** | 中标金额最高的项目是哪个？ | `[pg]` / `require=[pg]` |
| **ag-09** | 单一来源采购和竞争性谈判有什么区别？ | `[kb]` / `require=[]`（测**过度路由**） |

选它们的理由：**allow 列表最干净**（各只放行一个工具），且覆盖三种不同的路由压力 ——
图谱的「笔数排行」、采购库的「按金额排」（图谱答不了这个）、以及一道**不必要调工具**的对比题。

- ⚠ 刻意**不选** ag-04 / ag-06：那两条是 CLAUDE.md 记过的**重叠区**（图谱与采购库都能答），
  模型在两者间摇摆是**已知行为**（实测 5 次 3:2），拿它做判据会把噪声当结论。
- ⚠ **ag-09 的知识库覆盖极不均衡**：「单一来源」8 条 vs「竞争性谈判」**1 条**。模型答不全
  那一半是**对的**，别把知识缺口误读成模型缺陷。
- 想加一句纯寒暄负例（`ag-10`，「谢谢，刚才那个回答很清楚」，`allow=[]`）是一行的事 ——
  它抓「对任何输入都习惯性调工具」最狠。

## 3. 怎么跑

⚠ **中文请求体不能内联进 `curl -d`** —— Git Bash 按 GBK 发出去，服务端按 UTF-8 解析回 400
`error parsing the body`。所以下面这个脚本**从用例文件读问题**（不经命令行 argv，同一个病），
直接用：

```python
# ab_one.py —— 用法: uv run python ab_one.py <ollama|vllm> ag-05
import json, sys, pathlib, urllib.request
sys.stdout.reconfigure(encoding="utf-8")          # 否则 Git Bash 下中文乱码

provider, case_id = sys.argv[1], sys.argv[2]
cases = {c["id"]: c for c in map(json.loads,
         pathlib.Path("eval/agent_cases.jsonl").read_text(encoding="utf-8").splitlines())}
question = cases[case_id]["question"]

body = json.dumps({
    "question": question,
    "llm_provider": provider,   # 空串 = 跟服务端默认，A/B 必须显式给
    "rerank": False,            # A/B 前置：重排关
    "web_search": False,        # A/B 前置：联网关
}, ensure_ascii=False).encode("utf-8")

req = urllib.request.Request("http://127.0.0.1:8000/api/agent", data=body,
                             headers={"Content-Type": "application/json"})
calls, results, text, total = [], [], [], None
with urllib.request.urlopen(req, timeout=900) as resp:   # Ollama 在 CPU 上很慢，给足
    for raw in resp:                       # SSE 帧 = `data: {json}\n\n`，没有 event: 行
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        ev = json.loads(line[5:].strip())
        t = ev.get("type")
        if t == "tool_call":
            calls.append(f'R{ev["round"]} {ev["name"]} «{ev["query"]}»')
        elif t == "tool_result":
            results.append(f'ok={ev["ok"]} 新增={len(ev["new_indices"])} '
                           f'已知={len(ev["known_indices"])} err={ev["error"]!r} '
                           f'图={ev["graph"] is not None} 库={ev["pg"] is not None}')
        elif t == "delta":
            text.append(ev["content"])
        elif t == "error":
            text.append(f'[ERROR] {ev["message"]}')      # ⚠ 报错也要收下来
        elif t == "done":
            total = ev["total_ms"]

print(f'provider = {provider}   用例 = {case_id}   问题 = {question}')
print(f'调了哪些 = {calls or "（一个都没调）"}')
print(f'几轮     = {len(calls)}')
print(f'结果     = {results or "（无）"}')
print(f'总耗时   = {total} ms')
print(f'正文     = {"".join(text)}')
```

⚠ 走到这一步**先空跑一句再正式计**（见读数纪律 1）。

## 4. 记什么（六格）—— 每格的来源与局限

| 格 | 从哪来 | ⚠ |
| --- | --- | --- |
| 调没调 / 调了哪个 | `tool_call` 的 **`name`** | |
| 参数 | `tool_call` 的 **`query`** | ⚠⚠ 那是 `peek_display()` 的**展示串**，**不是**模型给的原始参数。四种形状：检索=原样 `query`、图谱=`「类型标签：实体」`、采购库=`「类别=学校，金额≥500000 元」`、联网=`「词（最多 N 条）」` |
| 几轮 | **`tool_call` 事件计数** | ⚠ 数**事件**不数 `round` —— 同一轮可以调多个工具。上限 `agent_max_rounds`（5） |
| 结果条数 | `tool_result` 的 `ok` / `new_indices` / `known_indices` | ⚠ 图谱与采购库的 `new_indices` **恒为空**（它们不发 `sources`）—— 那两类的「拿到没有」看 `graph` / `pg` 载荷**是不是 `null`**，以及 `ok` |
| 漏没漏标记 | 正文 + **后端日志 `grep 漏进正文`** | ⚠⚠ 见下 |
| 耗时 | `done.total_ms` | 服务端自己量的 Agent 全程，**不是**墙钟 |

⚠⚠ **「漏没漏标记」的判据**：`markup_leak` 是 **`/api/chat` 独有**的信号
（`base_client.py:426` 只在 `stream()` 里产出），**Agent 链路根本不发这个事件** ——
写「看有没有 `markup_leak`」是**永远为假**的判据。Agent 侧的泄漏凭证是
`tools/base.py:757-775` 那几条 `logger.warning`，文案含 **「漏进正文的」**：

```bash
grep 漏进正文 /root/autodl-tmp/backend.log | tail
```

⚠ **原始参数拿不到**。`tool_call.query` 是展示串（见上表）。要模型的**真实参数**得走
`eval/agent_eval.py` 的 `RecordingLLM`（它包住 `stream_with_tools` 记快照）——
⚠ 那条路**必须先停后端**（Qdrant 排他锁）。只在需要更硬的证据时才走。

## 5. 三条读数纪律

1. **预热不计入**。Ollama 首次调用要加载模型（几十秒），vLLM 首次可能触发编译。
   每个供应商**先空跑一句再正式计**。
2. **单次结果不作数**。沿用 `agent_eval.py` 实测的噪声底：**均值 ±0.005、单题 ±0.28**。
   工具路由这类题**跨次跑会翻** —— 实测 ag-08 连跑三次是 `[采购库、联网]` / `[未调]` /
   `[未调]`。看的是**同侧三句的整体倾向**，不是某一格。单题翻转**先怀疑噪声**。
3. **CPU vs GPU 是硬件差，不是模型差**。Ollama 侧的慢是 24 GB 卡装不下 9B 的必然结果
   （9B Q4 在 CPU 上约 5~10 tok/s）。**只比「路由对不对 / 收不收口 / 漏不漏标记」，不比速度。**

## 6. 产出形状

两列（vllm / ollama）× 三行（ag-05 / ag-07 / ag-09），每格填上面六格。结论只写
**同侧三句的整体倾向**与**两侧的差异**，不写单题胜负。
