---
name: start-services
description: 起停本项目（招投标 RAG）的全部服务 —— vLLM(8001) / Ollama(11434) / 后端(8000) / 前端(3000) / PostgreSQL / SSH 隧道，含启动顺序、等待判据、停服确认，以及四个文档里没写的静默坑。命令不抄在这里，只指路。Use when 用户说「起服务」「重启服务」「服务都没了」「前端打不开」「后端没响应」「改了 .env 要重载」，或要把 AutoDL 实例上的服务拉起来。
---

# 起停服务（招投标 RAG）

**命令的单一事实来源在 `docs/` 与代码注释里，这份只写「按什么顺序做、怎么算成功、哪里会
静默出错」。** 别凭记忆报命令 —— 本文档所有路径都已实测过期过一次（见下面的 ⚠⚠）。

## 先判断我在哪台机器上

- `curl -s localhost:8000/api/health` **通** ⇒ 我在能直接操作后端的那台机器上。
- **不通** ⇒ 我多半在用户的 Windows 本机，而服务在远端 AutoDL 上。**本机唯一该跑的是
  SSH 隧道**（`docs/AutoDL部署步骤.md` §9，逐字写着「在本机开」）：

  ```bash
  ssh -CNg -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 -p <端口> root@<实例域名>
  ```

  ⚠ 它**挂着不返回**是正常的（`-N` 纯转发），别当成卡住。其余命令都得用户贴到服务器的
  bash 里跑 —— 这台 Windows 机器够不到那台机器。

## 指针表

| 要做什么 | 看哪 |
| --- | --- |
| **日常每次开机起服务**（主路径） | `docs/AutoDL部署步骤.md` **§7「关机 → 有卡开机」** |
| ⚠⚠ **vLLM 那条命令** | **别用 §7 的！** 见下面「§7 已过期」 |
| 起后端与前端 | 同上 **§8** |
| 验证清单 | 同上 **§10** |
| 必踩的坑（8 条表） | 同上 **§12** |
| SSH 隧道（**在本机开**） | 同上 **§9** |
| 一次性安装 / 重装（**不是**日常路径） | 同上 **§4~§6** |
| PG 没起 / 重启后要做什么 | `docs/AutoDL安装PostgreSQL.md` **§8**「容器重启」分支 |
| **别做的事**（改 `listen_addresses` 等） | 同上 **§9** |

⚠ **§7 与 §5/§6 的区分是这张表最值钱的一行**：§5/§6 是**一次性安装**（装 Ollama、装 vLLM、
验 parser），§7 才是**每次开机**要跑的三段。不区分就会指着一份 550 行的文档说「照着做」——
而日常根本不该重跑安装步骤。

## ⚠⚠ §7 里的 vLLM 命令已过期，别再抄

它现在写的是 `/root/autodl-tmp/DeepSeek-R1-0528-Qwen3-8B` + `--tool-call-parser <第6步验通的那个>`
（占位符），而**当前真机跑的是 `Qwen3.5-9B` + `qwen3_coder`**。

**当前有效的命令在代码里**，是 2026-09-27 真机验过的原文：

> `src/bidding_qa/clients/local_client.py` 的 `VllmLLM` 类 docstring（`:60-87`）
> —— `vllm serve /root/autodl-tmp/models/Qwen3.5-9B --served-model-name qwen3.5:9b
> --port 8001 --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`

⚠ 那段注释甚至**点名了文档这份是错的**（「本文档原先写的候选是 `hermes` / `deepseek_v3`，
两个都不对」）。**要抄就抄代码。** 配错的形状是**静默的**：`tool_calls` 字段整个不存在、
`finish_reason` 是 `"stop"`、整段标记原样落在 `content` 里 —— 与「模型不愿意调工具」长得
一模一样。

**OOM 时**（`[1]+ Killed`）：实测那条带了 `--enforce-eager --max-num-seqs 4 --max-model-len 16384`
仍然被杀，真正的绕行是在命令**前面**加环境变量前缀
`VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0`（⚠ 出自会话记录，**不在任何文档里**）。

## 启动顺序与等待判据

**顺序**：vLLM 最先（它吃约 16~20 GB，后起的只能将就）→ Ollama → 后端 → 前端。
⚠ **`OLLAMA_MODELS` 必须在 `ollama serve` 之前 export**（事后再设，blob 已经落到系统盘，
那个盘几十 G 就满）—— 这一条 §7 里是逐字写对的。

**等待判据比「命令返回了」重要**（`nohup … &` 全都秒回）：

| 服务 | 判据 |
| --- | --- |
| vLLM | 日志出现 `Uvicorn running on http://0.0.0.0:8001`（`tail -f /root/autodl-tmp/vllm.log`） |
| Ollama | `curl -s http://127.0.0.1:11434/api/version` 有回 |
| 后端 | 日志出现 `Uvicorn running on http://0.0.0.0:8000`。⚠ 首次要加载向量模型与索引，**几十秒** |
| 后端真的可用 | `curl -s localhost:8000/api/health` 里 **`status` 是 `"ok"`** —— ⚠ **不是 HTTP 200**：`_bootstrap` 期间 health 回的是 **503**，那时索引还没好，提问会失败 |
| 四家供应商齐了 | 同一个 health 的 `llm_providers` 是 **4 家**（DeepSeek / GLM / vLLM / Ollama）。⚠ 只列真的配了地址/Key 的，少一家是**配置问题不是 bug** |
| PostgreSQL | `pg_lsclusters` 看到 `online`（⚠ 不是 `systemctl`，容器里没有 systemd） |

## 停服

**`docs/` 里一条 kill 命令都没有** —— 下面这套是补的。

⚠⚠ **这台容器里没有 `ss`**（2026-09-28 真机实测：`bash: ss: command not found`）。
用 bash 内建 `/dev/tcp` 替代，零依赖：

```bash
# 谁在听（替代 ss -ltnp）
for p in 3000 8000 8001 11434; do
  (echo >/dev/tcp/127.0.0.1/$p) >/dev/null 2>&1 && echo "$p ✓ 在听" || echo "$p ✗ 没起"
done

# 谁在跑 + PID
ps aux | grep -E 'vllm|ollama|uvicorn|next' | grep -v grep

kill <pid>          # 只 kill 从上面找到的确切那一个
```

⚠ **端口在听 ≠ 服务能用**：vLLM 在加载权重期间端口就已经在听了。活性判据是打端点
（`curl -s 127.0.0.1:8001/v1/models` / `…:11434/api/version`）。
⚠ **别用 `/api/health` 的 `llm_providers` 判断进程死活** —— `local_client.py:49-57` 的
`ready` 是 `bool(base_url.strip())`，**纯读配置、不发请求**。胶囊里有 vLLM 只说明 `.env`
填了地址；服务没起来的症状是**提问时报连接错误**。

- ⚠⚠ **vLLM 停下后必须 `nvidia-smi` 确认 VRAM 真掉下来再重启**。没掉就重启等于两个进程
  同时抢显存，后起的那个直接 OOM —— 而表现只是「新进程起不来」，看不出是被旧的占着。
- ⚠ 别用 `pkill -f vllm` 一把梭：它会连带命中任何命令行里含 `vllm` 的进程。
- ⚠ 前端 `npm run dev` 与 `npm run build` **不能同时跑**（共用 `frontend/.next/`）。build 会把
  dev 的产物覆盖成生产版，dev server 进程不察觉、仍回 200 的 HTML，但 `/_next/static/**` 全 404
  —— 表现是「页面出来了但没样式」。撞上了就 `rm -rf frontend/.next` 再重启 dev。

## 四个静默坑（前三个文档里没有）

### 1. ⚠⚠ `OLLAMA_SUPPORTS_TOOLS` 为假 ⇒ 整张工具表被剥空

`agent.py:253`：

```python
tools = tools_for(enable_web) if getattr(llm, "supports_tools", True) else []
```

所以 `OLLAMA_SUPPORTS_TOOLS=false` 时，**Ollama 那一侧一个工具都挂不出去**，退化成像
`/api/chat` 那样的「预检索 + 直接作答」。表现是「Ollama 从来不调工具」——**界面上完全看不出
这是配置还是模型行为**。做任何两侧对比之前先验（只印 True/False，不泄密）：

```bash
cd /root/rag_agent && uv run python -c "from bidding_qa.config import settings; print(settings.ollama_supports_tools)"
```

⚠ `ollama show` 说 Capabilities 里有 `tools`，**不等于调用跑得通**（Ollama 侧对 Qwen3.5 的
解析器有争议）。拨成 `true` 之后用 `scripts/probe_tools.py` 再验一遍。

### 2. ⚠ `.env` 是 import 期读的 —— 改了必须重启后端

`ENABLED` 那批常量（图谱/采购库/联网/本地供应商的地址）全在模块导入时算死。
**热改不生效**，表现是「`.env` 明明改了，首屏那几张卡片还是灰的」。

### 3. ⚠ Ollama 被钉在 CPU 靠的是**派生模型**，不是配置

`num_gpu` 与 `qwen3.5:9b-cpu` 这两个字符串在整份仓库里**零命中**（含 `docs/`）—— 它只存在于
会话记录里。验法：

```bash
ollama show --modelfile <模型名> | grep PARAMETER    # 找 num_gpu 0
```

⚠ `OLLAMA_MODEL` 必须**逐字**指向那个派生模型名。指向 `qwen3.5:9b` 就等于把它放回 GPU、
与 vLLM 抢那 24 GB。⚠ **换机 / 重置镜像后它就不存在了**，而症状（vLLM 被挤崩、Ollama OOM）
看起来像「vLLM 自己太重」—— 别照着这句话去调 `--gpu-memory-utilization`。
⚠ `OLLAMA_NUM_GPU=0` 这个环境变量**没有官方确认**，别用；维护者认的是 Modelfile 的 `num_gpu`。

### 4. ⚠ 后端与 `ingest` / `eval_rag --retrieval live` 互斥

本地 Qdrant 是嵌入式文件模式，同目录**独占锁**。跑那两个之前先停后端（`get_retriever()` 会
直接抛 `RuntimeError`，脚本明确报错退出、不静默降级）。`eval/agent_eval.py` 同一条限制 ——
但 `eval/dashboard.py` **不用停**（它只读 PostgreSQL，不碰 Qdrant）。
