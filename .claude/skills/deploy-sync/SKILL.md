---
name: deploy-sync
description: 把本地改动同步到远端 AutoDL 服务器 —— 打包 / scp / 解包 / 重启后端 / 验证，含两个静默风险（带 .env 会覆盖服务端已填的地址；.env.bak 未被 gitignore 也未被 tar 排除，会明文进包或进 commit）。Use when 用户说「同步到服务器」「部署上去」「传上去」「本地改完了怎么上服务器」「服务器上还是旧代码」。
---

# 部署同步：本机 → AutoDL 服务器

**首次全量部署**在 `docs/AutoDL部署步骤.md` **§2（本机打包）/ §3（本机上传）/ §4（解包+装环境）**
—— 那是「无卡模式开机 → 解包 → `uv sync` → 配 `.env`」的一次性流程。

⚠⚠ **增量同步那一节 `docs/` 里没有** —— 日常「本地改完再传一次」是本文下面这些。

## 别从 `git clone` 装

⚠ §1 明写：这个仓库只有 1 个 commit、**没有 remote**，而且有 30 多个**代码必须依赖但还没提交**
的文件（`clients/`、`tools/`、`eval/`、十几个探针脚本、几个前端组件）。**必须整棵树打包**，
`git clone` 拿到的是一份跑不起来的残缺树。

## 本机：打包

```bash
# 在仓库根目录跑
tar czf /tmp/rag_agent.tar.gz \
  --exclude='.venv' --exclude='models' \
  --exclude='frontend/node_modules' --exclude='frontend/.next' \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.git' --exclude='*.log' \
  --exclude='.env.bak*' \
  .
```

**必须打进去的三样**（少了就得重做，且 git 补不回来 —— §2 那张表）：

| 目录 | 为什么 |
| --- | --- |
| `data/qdrant_local/` | 向量库。**带上它就不用重新 ingest** |
| `data/index/bm25_index.json` | BM25 索引。⚠ 与向量库**必须同时搬**，否则 `doc_id` 对不上 |
| `data/processed/*.xlsx` | 源数据，将来重建索引时要用 |

### ⚠⚠ 风险一：`.env` 带上去会**静默覆盖**服务端那份

§2 的默认写法是 `--exclude='.env'`（到服务器上从 `.env.example` 复制一份手工填），
但**本次部署走的是「包含 `.env`」那条**（靠 `scp` 的 SSH 加密通道 + 落地后立刻
`chmod 600 /root/rag_agent/.env`）。

⚠⚠ **首次部署可以这样，增量同步时要想清楚**：本机这份 `.env` 里
`VLLM_BASE_URL=` 是**空的**（vLLM 侧当前是关闭状态），而服务器那份多半填着
`http://127.0.0.1:8001/v1`。带 `.env` 上去 = **把服务端填好的地址抹掉**，
表现是「同步完 vLLM / Ollama 那两家从供应商胶囊里消失了」—— 而代码一个字没改。

**同步前先比一下两边的地址**（只比键名和有没有值，**别打印值**）：

```bash
grep -c '^VLLM_BASE_URL=.\+' .env    # 本机：0 表示是空的
```

要么给 tar 加 `--exclude='.env'`，要么确认两边确实该一致。

### ⚠⚠ 风险二：`.env.bak-0927-1450` 两边都不挡

实测（2026-09-28）：仓库根目录的 `.env.bak-0927-1450` **既没被 `.gitignore` 忽略**
（`.gitignore` 里只有 `.env` 和 `.env.local` 两条），**也不在 §2 那条 tar 排除规则里**。
它里面有真实凭据。所以：

- **打包**要显式加 `--exclude='.env.bak*'`（上面那条已经有了）；
- **提交前**确认它没被 stage —— `git add -A` 会把它**明文**提交进版本库：
  ```bash
  git status --short | grep -i 'env.bak'    # 有输出就先处理掉再 add
  ```

⚠ 顺带：这六个凭据在 2026-09 的会话记录里被打印过、**尚未轮换**，按已泄露处理（§2 有一整段）。

## 本机：上传

```bash
scp -P <端口> /tmp/rag_agent.tar.gz root@<实例域名>:/root/autodl-tmp/
```

⚠⚠ **`scp` 用大写 `-P`，`ssh` 用小写 `-p`** —— 写反了 scp 会把端口当普通参数吞掉，
然后连 22 端口超时（§12 第一条坑）。
⚠ 落到 **`/root/autodl-tmp/`（数据盘）**，不是 `/root/`（系统盘通常只有 30G）。

## 服务器：解包 → 重启 → 验证

```bash
tar xzf /root/autodl-tmp/rag_agent.tar.gz -C /root/rag_agent
cd /root/rag_agent
uv sync            # ⚠ 只在 pyproject.toml / uv.lock 变了时才必要，很慢
```

然后重启后端（**停服的那套命令见 `start-services` skill** —— 只 kill 用 `ps aux` 找到的确切 PID。
⚠ 那台容器里**没有 `ss`**）。

**谁需要重启，看改了什么**：

| 改了什么 | 要重启什么 |
| --- | --- |
| `.env` 任意一项 | **后端**。⚠ `.env` 是 **import 期**读的（`ENABLED` 那批常量），热改不生效 |
| `src/` 任意 Python | **后端** |
| `frontend/` | dev server 的 HMR 通常够；**新增依赖**要重启。⚠ 与 `npm run build` 互斥（共用 `.next/`） |
| 只有 `.env` 里的**供应商地址**变了 | 后端即可 —— vLLM / Ollama 是**独立进程**，不用动 |
| `pyproject.toml` / `uv.lock` | `uv sync` + 重启后端 |

**验什么**（判据同 `start-services`）：

```bash
curl -s localhost:8000/api/health
```

- **`status` 必须是 `"ok"`** —— ⚠ **不是 HTTP 200**：`_bootstrap` 期间 health 回的是 **503**，
  那时索引还没加载完，提问会失败。首次加载向量模型与索引要**几十秒**。
- **`llm_providers` 的家数**与你打包前一致（4 家 = DeepSeek / GLM / vLLM / Ollama）。
  ⚠ **少一家就是这次同步真的弄坏了什么** —— 最常见的就是上面那个 `.env` 覆盖。
- 改了启动相关的东西再看一眼日志开头：`tail -50 /root/autodl-tmp/backend.log`
  —— 那段「启动配置体检」会把没启用的可选能力和**未声明的 `.env` 键**逐个点名。

## 别在服务器上跑的

- `uv run python -m bidding_qa.ingest --recreate` —— 索引已经打包带过来了（§4.3）。
  真要跑**必须先停后端**（Qdrant 独占锁）。
- `batch/to_neo4j.py` —— 它建的**英文标签那套**按供应商聚合金额会笛卡尔积（虚高 24 倍）。
- `batch/run_batch.py` —— 已明确超出范围。
