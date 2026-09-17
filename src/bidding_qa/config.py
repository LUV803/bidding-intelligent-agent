"""全局配置：统一从项目根目录的 .env 读取。

注意：本模块在导入时会设置 HuggingFace 相关环境变量（HF_ENDPOINT / HF_TOKEN），
因此必须在导入 sentence_transformers 之前先导入本模块。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# <repo>/src/bidding_qa/config.py -> <repo>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)

#: 可选能力 → (判定字段, 关掉时那句话)。
#: ⚠ 只列**「字段非空即为开」**那几项 —— 它们的开关就长在本类里，加一行就是加一条。
#:   文本大模型**故意不在这里**：它的开关是「默认供应商对应的那个 Key」，
#:   而 provider→key 的映射在 clients/llm_factory.py，本模块不能反向 import clients
#:   （clients 全都 `from ..config import settings`，立刻成环；照抄一份映射则是
#:   本仓库反复记过的「加第三家供应商要改四处」）。那一条由 `_TEXT_LLM_SOURCES` 兜底。
#: ⚠ `zhipu_api_key` **身兼两职**：它既是「图片解析」的开关，又是 `LLM_PROVIDER=zai` 时的
#:   文本 Key。所以它空着时只报「图片解析不可用」是不够准的 —— 但那是 `llm_provider`
#:   默认成 deepseek 时的常见形态，而真要用的那家没配好会在**回答里**如实点名
#:   （`rag._no_key_hint()` 从客户端的 `_NOT_CONFIGURED` 里切键名）。这里不重复那件事。
_OPTIONAL_CAPS = (
    ("知识图谱", "neo4j_uri", "未配置 NEO4J_URI => 图谱工具不挂出"),
    ("采购记录库 / 对话历史", "postgres_host",
     "未配置 POSTGRES_HOST => 采购库工具不挂出、对话历史只存浏览器（两个能力共用这一个开关）"),
    ("联网搜索", "tavily_api_key", "未配置 TAVILY_API_KEY => search_web 工具不挂出、开关打开也不联网"),
    ("Exa 联网搜索", "exa_api_key", "未配置 EXA_API_KEY => search_web_exa 工具不挂出（Tavily 不受影响）"),
    ("图片解析", "zhipu_api_key", "未配置 ZHIPU_API_KEY => 附件按钮会如实报错"),
)

#: 「哪些字段非空 ⇒ 至少有一个文本大模型能用」。`_config_findings` 用它判断会不会
#: 退化成 `fallback_answer`。
#:
#: ⚠⚠ **只回答「全都没配」这一个问题**，刻意不回答「某一家配没配」——
#: 后者是 `llm_factory.available_providers()` 的事（它读各客户端自己的 `ready`）。
#: 在 config.py 里维护一份 provider→字段的映射会**立刻成环**，这正是它当初只写
#: 两个 Key 的合取、而不按 provider 映射判的原因。
#:
#: ⚠ **加新供应商时这里要加一行**。漏了的表现只是：只配了那一家时启动体检多报一句
#: 「会退化成 fallback_answer」——是句谎话，但不影响任何功能行为。
#:
#: ⚠ 云服务看 Key、本地推理看 base_url（`clients/local_client.py` 覆写了 `ready`），
#: 这张表遵循的正是同一个「字段非空即为开」判据。
_TEXT_LLM_SOURCES = (
    "deepseek_api_key",
    "zhipu_api_key",
    "vllm_base_url",
    "ollama_base_url",
)


def _undeclared_env_keys(path: Path | None = None) -> list[str]:
    """`.env` 里写了、但 `Settings` 没有同名字段的键（大小写不敏感，因为 `case_sensitive=False`）。

    ⚠⚠ 这就是本仓库踩过**四次**的那个坑：`extra="ignore"` 让 pydantic-settings 把没声明的键
    **静默丢弃**（Neo4j / Postgres / Zhipu / Tavily 各栽过一次）。症状是「`.env` 里明明写了，
    代码就是读不到」—— 不报错、不警告，只有行为对不上，而且往往是隔着几周才被发现。
    启动时点一次名，比每次靠人回想便宜得多。

    ⚠ 只查**方向二**（`.env` 有多余键）。方向一（字段有、`.env` 没写）是**正常状态** ——
    本类 83 个字段绝大多数靠默认值，逐个报会变成一屏噪声，那等于没报。
    """
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return []
    declared = {name.upper() for name in Settings.model_fields}
    unknown: list[str] = []
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key and key.upper() not in declared:
            unknown.append(key)
    return unknown


def _config_findings(s: Settings) -> list[str]:
    """启动配置体检的结果，一行一条**人话**（空列表 = 一切正常，不打印任何东西）。

    纯函数（不碰全局、不记日志），便于探针直接拿构造出来的 Settings 断言 ——
    校验器只负责把它念出来。

    ⚠ 这里**没有** `settings.validate()` 那回事：`Settings.validate` 是 pydantic 的
    `BaseModel.validate(value)`（校验单个数），与配置完整性无关。所以「缺失项」在本仓库
    从来没有一个集中的检查点 —— 这个函数就是补上的那一个。
    """
    findings: list[str] = []

    off = [f"{label}：{why}" for label, field, why in _OPTIONAL_CAPS
           if not getattr(s, field, "").strip()]
    if off:
        findings.append(f"未启用的可选能力（不影响问答，相关工具/入口会如实报不可用）：{'；'.join(off)}")

    if not any(getattr(s, field, "").strip() for field in _TEXT_LLM_SOURCES):
        findings.append(
            "没有任何文本大模型可用（DEEPSEEK_API_KEY / ZHIPU_API_KEY / "
            "VLLM_BASE_URL / OLLAMA_BASE_URL 全空）=> "
            "问答会退化成 fallback_answer（把知识库原文端出来），不是模型答案"
        )

    unknown = _undeclared_env_keys()
    if unknown:
        findings.append(
            f"⚠ .env 里有 {len(unknown)} 个键在 config.py 中没有同名字段，会被静默丢弃："
            f"{'、'.join(unknown)}（这是本仓库踩过四次的坑，要么补字段、要么删键）"
        )
    return findings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 文本大模型供应商 ----------
    # 纯文本对话（问答 / Agent / 评判）用哪一家。可选值见 clients/llm_factory.py 的
    # `_PROVIDERS`：deepseek（默认）| zai（智谱 GLM）。
    #
    # ⚠ 只影响**文本**链路。视觉（图片解析）永远是智谱 glm-4.6v-flashx，见下面 zhipu 段。
    # ⚠ 填了不认识的值**不报错**，会回退到 deepseek 并在日志里记一行 WARNING
    #   （同 qdrant_mode 的「降级而非报错」，拼错一个环境变量不该让整个服务起不来）。
    # ⚠ 与另两处联动：config.py / .env / .env.example 要**三处一起改**，
    #   本类 extra="ignore" 会把没声明的键静默丢弃——本仓库踩过四次。
    llm_provider: str = "deepseek"

    # ---------- DeepSeek 大模型 ----------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_temperature: float = 0.2
    deepseek_max_tokens: int = 2048
    # 送入模型的历史轮数（一问一答算一轮）
    history_rounds: int = 3

    # ---------- 本地推理：vLLM ----------
    # ⚠⚠ **空串 = 关**。这是 `clients/local_client.py` 里 `ready` 的判据
    #   （本地服务不校验 API Key，所以判据从「Key 非空」改成了「地址非空」）。
    #   默认空 ⇒ 没跑 vLLM 的机器上这个供应商**根本不出现在前端胶囊里**，
    #   于是本机开发环境的选项数量、/api/health 载荷、全部探针一个字节都不变。
    #
    # ⚠ 地址要带 `/v1` 后缀（OpenAI 兼容端点的根），例：http://127.0.0.1:8001/v1
    # ⚠ 端口别用 8000 —— 那是后端 FastAPI 自己占的。
    vllm_base_url: str = ""
    # 必须与 `vllm serve --served-model-name` **逐字一致**，否则服务端回 404 model not found
    vllm_model: str = "DeepSeek-R1-0528-Qwen3-8B"
    # ⚠ R1 系官方推荐的推理温度区间是 0.5~0.7（不是 eval 那边的 0.0）——
    #   本地模型是拿来**作答**的，不是拿来当裁判的，重复性问题不靠它。
    vllm_temperature: float = 0.6
    # ⚠ 给足：R1 的思维链会先吃掉预算，给 2048 的表现是「想了半天然后返回空」
    #   ——与 glm-4.6v 那次「图传上去了、返回空」是同一个病，最难查的那种。
    vllm_max_tokens: int = 8192

    # ---------- 本地推理：Ollama ----------
    # 同上：空串 = 关。例：http://127.0.0.1:11434/v1
    ollama_base_url: str = ""
    # ⚠ 必须与 `ollama list` 里那个名字**逐字一致**：`qwen3.5:9b` 与 `qwen3.5-9b`
    #   是两个不同的名字，冒号 / 横杠、tag 一个字符都不能差。写错的话服务端回
    #   404 model not found —— 而那看起来像「Ollama 没起来」。
    ollama_model: str = "qwen3.5:9b"
    ollama_temperature: float = 0.6
    ollama_max_tokens: int = 4096
    # ⚠⚠ **这是个「保守起点」，不是结论 —— 判据只能实测**：`ollama show <模型>` 看
    #   Capabilities 那行有没有 `tools`。
    #
    #   为什么默认 False：官方 `deepseek-r1:8b` 的模板里确实**没有 tools 段**，带 tools
    #   的请求会被直接拒掉（`... does not support tools`，ollama#15602）。而 /api/agent
    #   每轮必定带 tools，不降级的话「选 Ollama 提问」= 一个字都出不来。为假时本轮
    #   不挂工具、走「预检索 + 直接作答」——**法规类照答，图谱/数据库类答不了**。
    #
    #   ⚠⚠ 但默认模型 `qwen3.5:9b` **是支持工具的**，Ollama 侧的落地情况却**说法不一**，
    #   所以别照着「Qwen 支持工具」这句话直接拨 true：
    #     · 支持：Parallel 的工具调用文档把 Qwen 3.5 列为原生支持、且最可靠的一类；
    #     · 存疑：ollama#14795 有 `Qwen3.5:0.8b does not support tools` 的报障（那条是
    #       从 HF 导入、模板靠 GGUF 元数据自动推导的，**未必适用于官方库模型**）；
    #       PR #14167 的讨论则称 Ollama 主线给 Qwen3.5 接的是 Qwen3 的 Hermes JSON
    #       解析器，而它训练用的是 Qwen3-Coder 的 XML 格式 —— 认下来了但解析不对。
    #   ⇒ 拨成 true 之后**务必用 `scripts/probe_tools.py` 真机验一遍**，别只看接口没报错。
    ollama_supports_tools: bool = False

    # ---------- Qdrant 向量库 ----------
    # auto: 先连云服务，失败则自动回退本地嵌入式模式
    qdrant_mode: str = "auto"
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "bid_qa"
    qdrant_vector_size: int = 512
    qdrant_distance: str = "cosine"
    qdrant_timeout: int = 30
    # 本地嵌入式模式的数据目录（无需 Docker）
    qdrant_local_path: str = "data/qdrant_local"

    # ---------- 向量模型 ----------
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_device: str = "cpu"
    embedding_batch_size: int = 32
    # BGE 中文模型官方推荐的检索指令前缀（仅用于 query，不用于文档）
    embedding_query_instruction: str = "为这个句子生成表示以用于检索相关文章："
    embedding_max_seq_length: int = 512
    # 模型缓存目录（相对项目根目录），避免反复下载
    model_cache_dir: str = "models"
    hf_endpoint: str = ""
    hf_token: str = ""

    # ---------- 重排序 ----------
    # 对初筛结果做 CrossEncoder 二次精排。CPU 上开销可观（见 README「重排序」一节），
    # 因此默认关闭，由前端「重排序」开关按需启用。
    rerank_enabled: bool = False
    rerank_model: str = "BAAI/bge-reranker-base"
    # 只对初筛前 N 条做在线打分，耗时近似线性
    rerank_candidates: int = 10
    rerank_batch_size: int = 16
    # 截断长度直接决定算力：本知识库文本中位数约 176 token，
    # 问题在最前（最强信号），截断只会砍掉答案尾部。
    rerank_max_length: int = 256
    # 重排分数闸门（仅重排启用时生效）：CrossEncoder 直接判断「这条资料能否回答该问题」，
    # 比向量/BM25 原始分更准。实测相关条目 ≈0.73，知识库里没有答案的问题全部 ≈0.50，
    # 取 0.55 作为分界。置 0 可关闭。
    min_rerank_score: float = 0.55

    # ---------- Agent 模式 ----------
    # 允许模型主动调用工具的轮数；用尽后仍会强制再问一次（禁用工具）收口，
    # 保证任何情况下都能产出回答。
    #
    # 2 → 3 是因为多了一个工具：图谱类问题的自然路径是「entity_search 定位 →
    # 查详情」，两步就把预算用光了，再想换个子类查一次就没额度。实测
    # （scripts/probe_graph_tool.py）两个图谱问题都正好卡在 2 轮上限、靠强制收尾作答。
    # 提问早就有答案时循环会提前 break，所以加一轮只对「确实还想再查」的问题有开销。
    #
    # 3 → 5（2026-09-20）：那版注释写着「加了第三个工具也不用再动」，**已被实测推翻**。
    # 拿一个对比类问题（「单一来源采购和竞争性谈判有什么区别？」）打真机 /api/agent，
    # 模型**自己**发了 3 次检索、逐层收窄（整句 → 收窄到半边 → 再收窄），把 3 轮用满，
    # 且第 3 轮的查询**比第 2 轮还宽**（「竞争性谈判」vs「竞争性谈判采购方式适用情形」）
    # —— 那是预算见底时的抖动：它自始至终没给第二个实体单独发过一次干净的查询。
    # 对比类问题至少要「实体A → 实体B → 各自补救」四步，3 轮装不下。
    # ⚠ 轮数用尽**不等于**数据丢失（还有强制收尾轮兜底），丢的是**模型自我纠正的机会**。
    # 真正让 3 轮显得够用的是 pg_store 命中 0 行时给的「换个筛选值」提示（同一轮内自我纠正）
    # —— 那只覆盖「筛选值写错」，覆盖不了「问题本身需要拆成几问」。
    agent_max_rounds: int = 5
    # 单次请求的来源总数上限（含预检索）。上限压得低有两个原因：上下文膨胀，
    # 以及前端 Markdown.tsx 的行内引用正则 /\[(\d{1,2})\](?!\()/ 只认 1~2 位数字。
    #
    # 12 → 20（2026-09-20，与轮数一起提）：这两个数**是耦合的**，单提轮数会撞顶。
    # 开着联网时，本地 top_k=5 + 网页 5 条在**任何工具轮之前**就占掉 10 个，
    # 原上限 12 只剩 2 个额度 —— 轮数提到 5 之后，工具轮几乎每一轮都必然溢出，
    # 检索到的资料**静默**变成「新增 0 条」（原先还会连带触发一条错的「什么都没查到」
    # 质量提示，见 tools/base.py 的 empty_reason）。20 仍在两位数字范围内。
    agent_max_sources: int = 20
    # 工具单次检索可取的条数上限（模型偶尔会传 top_k=50）
    agent_tool_max_top_k: int = 8
    # 是否把 assistant 消息的 reasoning_content 回传给下一轮。
    # 实测当前 API 传与不传都接受（见 scripts/probe_tools.py），保留回传是防御性的；
    # 若某天服务端收紧导致 400，agent.py 会自动去掉该字段重试一次。
    agent_echo_reasoning: bool = True

    # ---------- 知识图谱（Neo4j）----------
    # 图谱是**可选**能力：连不上时 Agent 照常可用，只是图谱工具会如实报告失败。
    # 因此默认全空，且 graph_store.warmup() 吞掉所有异常（见该模块注释）。
    #
    # ⚠ 本类用 extra="ignore"，**没在这里声明的 NEO4J_* 会被静默丢弃**——
    #   .env 里原本就有这 7 个键，但在补齐字段之前一个都没生效。新增变量时必须同步加字段。
    neo4j_uri: str = ""
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_connection_timeout: int = 60
    neo4j_max_connection_lifetime: int = 3600
    neo4j_max_connection_pool_size: int = 50
    # 单条查询的超时（秒）。模板实测都在 400ms 内，留足余量兜住意外慢查询
    neo4j_query_timeout: float = 15.0
    # 单次图谱查询最多回传/展示多少行明细，超出只报总数（扇出实测可达 200+）
    graph_max_rows: int = 20

    # ---------- PostgreSQL（采购记录库）----------
    # Agent 的 search_procurement_records 工具查它：一张平表 bid_records（8751 行 × 19 列），
    # 由独立脚本 src/database/to_postgresql.py 灌入。
    #
    # 同样是**可选**能力：连不上时问答照常，只是这个工具如实报告失败（pg_store.warmup() 吞异常）。
    #
    # ⚠ 与上面 Neo4j 段同一个坑：本类 extra="ignore"，**没在这里声明的 POSTGRES_* 会被静默丢弃**。
    #   .env 里早就有这 5 个键，但在补齐字段之前一个都不生效。新增变量时必须同步加字段。
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = ""
    postgres_database: str = "chatbot"
    postgres_connect_timeout: int = 10
    # 单条查询的超时（秒）→ 作为 statement_timeout 在**建连时**设上。
    # 这是整条链路上唯一的查询级超时（execute_tool 自己没有超时，浏览器是唯一的取消手段），
    # 所以必须建连就设，不能指望调用方记得设。实测模板查询都在毫秒级。
    postgres_query_timeout: float = 8.0
    # 单次查询最多回传/展示多少行明细；总数另用 count(*) 精确统计，不截断
    pg_max_rows: int = 20

    # ---------- 智谱视觉模型（图片解析）----------
    # 前端的附件按钮把图片交上来，由 vision.py 调它抽成结构化字段（招标公告/中标公告等）。
    #
    # 同样是**可选**能力：没配 Key 时 /api/vision 如实报错，问答链路完全不受影响
    # （它不参与 Agent 工具路由，也不进 /api/chat 的检索，见 rag.with_image_context）。
    #
    # ⚠ 与前两段同一个坑，**这是第三次了**：本类 extra="ignore"，没在这里声明的 ZHIPU_*
    #   会被静默丢弃——.env 里那个 ZHIPU_API_KEY 原本只服务于 batch/ 下的独立脚本
    #   （那边自己手写了一个 .env 解析器），在补上这个字段之前对问答服务完全不可见。
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # ⚠ glm-4.6v-flashx 是**推理型**模型，会先吐思维链再出正文。本项目在 batch 那边
    #   踩过同一个坑（batch/run_batch.py 的注释：「GLM 是推理模型，思维链会先吃掉 max_tokens」），
    #   所以 vision.py 的 max_tokens 必须给足，否则表现为「图传上去了，返回空」。
    #   免费档是 glm-4.6v-flash，撞限额时改这里即可，不用动代码。
    zhipu_vision_model: str = "glm-4.6v-flashx"
    # 单次视觉调用的超时（秒）。这是它唯一的超时，调用方不另设。
    zhipu_vision_timeout: float = 60.0
    # 思维链 + 结构化 JSON 的余量。别照着文本问答的 2048 抄——会被思维链吃光。
    zhipu_vision_max_tokens: int = 4096
    # 单张图片的字节上限（前端用同一个数做预检）。超了**明确报错**，不静默截断。
    vision_max_image_bytes: int = 10 * 1024 * 1024

    # ----- 智谱**纯文本**对话（与上面那段视觉共用 Key/Base URL，但各建各的连接）-----
    # 只在 LLM_PROVIDER=zai 时生效，见 clients/zai_client.py。默认是免费的 FlashX 档。
    #
    # ⚠ 前缀刻意用 `zhipu_` 而不是 `zai_`：`.env` 里用户已经填了 ZHIPU_API_KEY /
    #   ZHIPU_BASE_URL，新起一个前缀只会让老 `.env` 里的键被 extra="ignore" 静默丢弃。
    #   模块名叫 zai_client 是因为 SDK 的导入名是 `zai`，两者不一致是有意的。
    zhipu_chat_model: str = "glm-4.7-flashx"
    zhipu_chat_temperature: float = 0.2
    zhipu_chat_max_tokens: int = 2048

    # ---------- 联网搜索（Tavily）----------
    # ⚠ **语义是「开关打开 ⇒ 每次提问都联网」，不是「本地答不出才联网」**（2026-09-20 改，
    #   别按旧注释理解）：本地检索照常先做，然后拿**原问题**无条件搜一次，结果
    #   **并入【参考资料】**交给模型，正文照常标 [n]；Agent 链路另注册 search_web 工具，
    #   让模型能换个更精确的词再搜（自动那次用原问题，往往不够精确）。
    #   代价：每次提问 1 个 credit、首个字晚 1~3 秒。开关默认关着。
    #
    # 与图谱/采购库/图片解析一样是**可选**能力：没配 Key 时工具不注册、提示词不提，
    # 问答链路完全不受影响。**但它多一维**——前端「联网搜索」开关是**每请求**的，
    # 而图谱/采购库只有进程级 ENABLED。两者必须同时为真才联网，
    # ⚠ **合取不要在调用方提前算**：`tools_for()` 内部合一次（工具表），
    #   api.py / agent.py 的联网判断各合一次，算重了就会出现「提示词提工具、工具表里没有」。
    #
    # ⚠ 与前三段同一个坑，**这是第四次了**：本类 extra="ignore"，没在这里声明的
    #   TAVILY_*/WEB_SEARCH_* 会被静默丢弃。新增变量时必须同步加字段。
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    # basic = 1 credit/次，advanced = 2。先用 basic，实测不够再调。
    tavily_search_depth: str = "basic"
    # 单次联网最多取几条。**这个值同时是上限**：模型在工具参数里要更多也会被夹到它
    # （见 web_search._clamp_max_results()）——schema 里的 maximum 只是给模型看的提示。
    # ⚠ 别调太大：开关开着时这几条**在模型第一轮之前就占掉了** agent_max_sources=12 的额度
    #   （本地 top_k=5 + 这里 5 = 10，留给工具轮的只剩 2），占满后模型后续调检索/联网工具
    #   会全部返回「0 新增 + 超出上限」。
    tavily_max_results: int = 5
    # 单次联网的总超时（秒）。联网卡在 SSE 关键路径上（/api/chat 必须等它完才作答），
    # 所以别给大。连接超时另在 web_search.py 里收到 3 秒——黑洞主机 3 秒就死。
    tavily_timeout: float = 8.0
    # **一次联网搜索的准入线：`to_sources()` 拿这批结果的 top 分跟它比，
    # 低于就整批丢弃（= 网上没这个东西）**。**0 = 不筛**。
    # ⚠⚠ 判据是 **top 分**、不是逐行分数（2026-09-20 改，原先逐行筛）。理由见
    # `web_search.to_sources()` 的 docstring：Tavily 的分随查询浮动，逐行绝对阈值
    # 会把「同一批里排第 3 但真相关」的那条误杀——用户实测香港那条就是这么被砍到只剩 1 条。
    # 0.70 是**实测标定**的（2026-09-20 复标，14 组查询各取 5 条）：
    #   真答得出的查询 top 分 **0.7863 ~ 0.9077**（含「香港物料供应及采购规例」
    #     「安徽省2026年8月最新公告」这类冷门/时效性的）
    #   网上根本没有答案的查询 top 分 **0.093 ~ 0.6594**（编造条文号 / 乱码 / 无关话题 /
    #     私有主体；最高的 0.6594 来自「某某公司内部员工食堂…」——**它穿过了旧的 0.62**，
    #     那正是旧阈值两头不讨好的证据）
    # 两簇之间是 (0.6594, 0.7863) 这条 0.127 宽的空带，0.70 取在其中、离两端各留约 0.04。
    # ⚠ 这条空带比初版标定（0.401~0.888）**窄得多**，余量只有 0.04 —— 改这个值之前
    #   务必先按 probe_web_search.py ④ 的方法重测分布，别凭手感挪。
    # ⚠ 取舍方向与 min_vector_score 一致：**误杀的代价更大**。阈值偏高会把「网上明明有答案」
    #   做成「没搜到」，模型随即照实说「知识库中没有」——是个**静默**的错答案（盐化工的形状）；
    #   偏低只是多带进一两条靠后的结果，而它在来源面板里带着分数、模型也被要求标注网页来源，
    #   错得看得见。
    # ⚠ 与 min_vector_score / GROUP_COLS 同一性质：**针对当前这批查询实测**。
    #   换搜索深度（basic↔advanced）、换域名白名单、或 Tavily 改了打分口径都要重测，
    #   probe_web_search.py ④ 有断言钉着「top 分在带里」与「低于线整批丢弃」两件事。
    web_search_min_score: float = 0.70
    # 逗号分隔的域名白名单（如 "gov.cn"）。留空 = 不限。
    # ⚠ **必须是 str，不能是 list[str]**：pydantic-settings v2 对复杂类型走 JSON 解析，
    #   写成 list 的话 .env 里填 "a.com,b.com" 会在 Settings() 构造时抛 SettingsError，
    #   而 settings 是模块级执行 → **整个服务起不来**（症状还长得像「配置没生效」）。
    #   照 cors_origins 的成例：声明 str，用 web_search_domain_list 属性切分。
    web_search_domains: str = ""
    # 单条网页内容进 Source / 提示词的长度上限（字符）。
    # ⚠ 不截断是静默故障：Source.answer 会经 sources 事件进前端 localStorage，
    #   超配额是**静默**失败（useChat 的 catch 吞掉），表现是「历史对话莫名其妙少了」。
    #   同一个串还会进 build_context，5 条 ×2KB 就是 10KB 上下文。
    web_content_max_chars: int = 400

    # ---------- Exa 联网搜索（第二个引擎，经 Exa 托管的 **MCP** 端点）----------
    # 与上面 Tavily 那段的**形状不同**：那边是直接打 Tavily 的 REST API，这边是打一个
    # **MCP 服务**（`https://mcp.exa.ai/mcp`，streamable HTTP），客户端在
    # `mcp/mcp_client.py` 里。**但 Agent 侧看不到这层差别** —— `search_web_exa` 是一个
    # 普通的 `BaseTool`，出进程的只是它 `run()` 里的实现细节，所以 CLAUDE.md 里
    # `SourceRegistry` 全局编号 / `recover_tool_calls()` 归属投票 / `assess_outcome()`
    # 结果质量这几条不变量一条都不破。
    #
    # ⚠ 它是**第二个**「每请求可变」的可选能力吗？—— 不是。它的开关就是这里这个 Key
    #   （进程级），与图谱/采购库同类；**但它挂不挂出去还要看前端那个「联网搜索」开关**
    #   （`tools_for(enable_web)` 里合取）。理由：联网是花钱的，用户明确关掉联网时
    #   模型还能自己掏钱去搜第二个引擎，是**违背用户意图**的静默扣费。
    #
    # ⚠ 与前面几段同一个坑，**这是第五次了**：本类 extra="ignore"，没在这里声明的
    #   EXA_* 会被静默丢弃（Neo4j / Postgres / Zhipu / Tavily 各栽过一次）。
    exa_api_key: str = ""
    # ⚠⚠ **`?tools=` 是「替换」语义，不是「追加」** —— 托管端点默认挂出来的那批工具里
    #   **没有** `web_search_advanced_exa`（它默认关闭），所以这个查询串不能省。
    #   要换工具就整串重写、把想要的列全，别指望叠加。
    # ⚠ 这个 URL 里**绝不带 Key**：认证走 `Authorization: Bearer` 头（见 mcp_client.py），
    #   与 `web_search.py` 那条「认证走 Header，绝不进 URL —— 免得密钥出现在日志/异常
    #   信息里」是同一条纪律。托管端点也认 `?exaApiKey=`，**那是不能用的那一种**。
    exa_mcp_url: str = "https://mcp.exa.ai/mcp?tools=web_search_advanced_exa"
    # 单次联网最多取几条。同时是**上限**：模型要更多也会被夹到它（见
    # `web_search_exa._clamp_max_results`）——schema 里的 maximum 只是给模型看的提示。
    # ⚠ 与 `tavily_max_results` 多一个约束：Exa 的 `numResults` 官方上限是 100，
    #   这里的默认值与 Tavily 保持一致，好让「两个引擎各占几个 [n] 编号」可预期
    #   （`agent_max_sources=20`，本地 5 + 联网 5，留给工具轮的还有 10）。
    exa_max_results: int = 5
    # 单次调用的总超时（秒）。**要覆盖「建连 + initialize 握手 + 一次搜索」三件事**，
    # 所以比对 Tavily 的 8 秒宽。
    # ⚠ 实测一次真实调用（2026-09-30，numResults=5）：connect 0.20s + initialize 0.63s
    #   + call_tool 5.83s = **6.7s**，其中端点自报 `searchTime` 3.9s；另一次（numResults=3）
    #   自报 10.3s。20 秒在正常波动下有富余，又不至于让黑洞连接把人钉住。
    # ⚠ 别给太大：它卡在 SSE 关键路径上（模型那一轮在等这个结果），
    #   而失败一次之后有 30 秒冷却兜着（见 `web_search_exa._FAIL_COOLDOWN`）。
    exa_timeout: float = 20.0
    # ⚠⚠⚠ **实测（2026-09-30）：`web_search_advanced_exa` 的结果里根本没有 `score` 字段。**
    #   同一次返回的 5 条，键分别是
    #   `id / url / title / text` + 可选的 `author / publishedDate / image / favicon`
    #   —— 没有任何一条带分数。所以这个旋钮**当前是死的**：`WebRow.score` 一律 0.0，
    #   而 0.0 < 任何正阈值 ⇒ 一旦有人把它设成正数，`to_sources()` 会把**整批**判成
    #   「网上没这个东西」，安静地把结果全丢掉。
    #   `web_search_exa.to_sources()` 为此加了一道守卫：**整批都没有分数时跳过闸门**。
    #
    # ⚠ 那为什么还留着这个字段？因为「没有 score」是**这个 MCP 代理**的行为，不是 Exa
    #   搜索 API 的行为（它的 REST `/search` 在 neural/fast 下是回 score 的）。将来换成
    #   直连 REST、或代理把它补回来，这里就是现成的旋钮。**在那之前，标定它的前提
    #   （拿到真实分布）根本不存在** —— 所以默认 0.0，且 `probe_web_search.py` 那套
    #   两簇空带的量法在这里**无从下手**。别照着它的数值抄一个「看起来合理」的进来。
    #
    # ⚠ 另：它**刻意不复用** `web_search_min_score`。就算哪天有了分数，Exa 的是 embedding
    #   语义相似度、Tavily 的是它自己那套（实测答得出 0.7863~0.9077 / 没答案 0.093~0.6594），
    #   两者不同源，照搬就是「换语料不重标 `MIN_VECTOR_SCORE`」那个病。
    exa_min_score: float = 0.0

    # ---------- 自建 MCP 服务器（把三个工具搬出进程）----------
    # 我们**自己写的** MCP 服务（`python -m bidding_qa.mcp_server`）暴露
    # `search_knowledge_graph` / `search_procurement_records` / `load_skill` 三个工具，
    # 后端起这个开关之后经 MCP 协议（streamable HTTP）去调它们，而不是在本进程里直接调
    # `graph_store` / `pg_store` / `skill_store`。
    #
    # ⚠⚠ **留空是默认且健康的状态**，不是「没配好」：
    #   空  ⇒ 三个工具在**本进程内**直接执行（今天的行为，逐字节不变）
    #   非空 ⇒ 那三个工具经 MCP 走独立进程
    # 所以它**刻意不进 `_OPTIONAL_CAPS`** —— 那一张表的语义是「字段非空即为开」，
    # 加一行会让**每一台正常机器**的启动体检都吐一句「未启用的可选能力」，而体检的规矩是
    # **配置齐备时一行都不输出**（见 `_config_findings`）。
    #
    # ⚠ 与 Exa 那个 `exa_mcp_url` 的区别：那个是**别家的托管端点**（要有 Key 才谈得上可用），
    #   这个是**我们自己的**、跑在回环地址上、不需要任何凭据。两者共用一个 MCP 客户端？
    #   —— 不共用，各写一个（见 `mcp/__init__.py` 与 `mcp_bridge.py` 的 docstring）。
    #
    # ⚠ 服务器与后端**必须读同一份 `.env`**：`Settings` 的 `env_file` 是
    #   `PROJECT_ROOT / ".env"`（绝对路径，与 CWD 无关），所以这一点天然成立。
    #   起点不一样（后端在仓库根、服务器可能在别处）也不会分家。
    mcp_server_url: str = ""

    # ---------- 数据与检索 ----------
    data_file: str = "data/processed/问题答案样例数据.xlsx"
    index_dir: str = "data/index"
    # 是否丢弃「仅标点/空白不同」的重复问题
    dedupe: bool = True
    # 返回给大模型的参考条数
    top_k: int = 5
    # 每路召回条数（融合前）
    recall_k: int = 20
    # RRF 融合参数
    # ⚠ **只有权重比与 k 有意义**：`hybrid.py` 用 `max_possible = (vw + bw) / (k + 1)`
    #   归一化，两个权重同倍放大等于没变（分子分母一起缩放）。
    # ⚠ `bm25_weight=4.0` 是 2026-09-22 按实测调过的（不是拍的）。依据与**代价**都写在
    #   CLAUDE.md「RRF 融合参数扫描」一节：语料 74 题上把 MRR 从 0.9932 拉到 1.0（修的是
    #   qa-0069「…招标项目」被 qa-0070「…中标项目」压住那一题，一个字的差别）。
    #   ⚠⚠ 但那个收益只有 **1 道题**，且**没有任何数据集能测出这个改动的代价** ——
    #   语料与留出集问题原文都在索引里，BM25 的失配弱点在那里看不见。要退回 1:1 只需
    #   把这三处（本文件 / .env / .env.example）一起改回去。
    rrf_k: int = 60
    vector_weight: float = 1.0
    bm25_weight: float = 4.0
    # 相关性闸门：RRF 只看排名，无法判断「答非所问」，
    # 因此再用原始分数兜底，任一通道达标即保留（两个通道是「或」的关系）。
    # 实测：本知识库中相关问题向量相似度 ≥0.84 / BM25 ≥20；
    #       无关问题向量 ≤0.67 / BM25 ≤3.8。
    min_vector_score: float = 0.72
    min_bm25_score: float = 8.0
    # 低于该融合分数的结果直接丢弃
    min_score: float = 0.0
    snippet_max_len: int = 180

    # ---------- 服务 ----------
    # ⚠ `0.0.0.0` = 绑定**所有网卡**，局域网内任何机器都能访问，而本服务**没有任何认证**。
    #   而 `web_search` 是**请求参数**，所以隔壁机器上的人可以白白消耗 Tavily 额度
    #   （免费档 1000 credits/月，每次提问 1 个）。CORS 拦不住这个——它是浏览器规则，
    #   curl / 脚本不理会。只在本机用就写 `127.0.0.1`；要拿手机/另一台电脑演示才需要 0.0.0.0。
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # 逗号分隔；默认放行本地前端开发端口
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # slowapi 限流：**每个端点**每个 IP 每分钟最多几个请求。**0 = 关闭限流**。
    #   ⚠⚠ 是「每个端点」不是「全站」——slowapi 的桶键是 `(客户端 IP, 端点函数名)`，
    #   `/api/chat` 与 `/api/agent` 各算各的（实测：一个端点打满 429 后，另一个照常 200）。
    #   真机验过，别照字面理解成全局限额。
    # ⚠ 默认值 120 不是随手写的：`scripts/eval_rag.py --retrieval http/auto`
    #   会从 127.0.0.1 连着打 74 个 `/api/search`（`--no-llm` 下几秒内打完），
    #   30/min 那一档会把评估脚本自己限流掉（好在 `raise_for_status()` 会大声报错，不是静默）。
    #   `/api/health` 另见 api.py，**整个豁免**（它的轮询间隔会被 429 反向推快，是个正反馈）。
    # ⚠ 本仓库单用户、无认证，限流只挡失控循环/误用，**挡不住**上面 api_host 那条真正的暴露。
    rate_limit_per_minute: int = 120

    # ---------- 启动配置体检 ----------
    @model_validator(mode="after")
    def _report_startup_config(self) -> Settings:
        """在 `Settings()` 构造时把配置一次说清（**只念不拦**）。

        ⚠⚠ **刻意只记日志、绝不 `raise`** —— 这是本仓库的既有性质，不是偷懒：
        四个可选能力全靠「没配就降级」活着（`graph_store.ENABLED` / `pg_store.ENABLED` /
        `web_search.ENABLED` / `vision.available` 全是 `bool(settings.X.strip())`），
        `.env.example` 交上去时这几个 Key **本来就是空的**。在这里 `raise` 等于
        「谁都没配 Key 就起不来服务」——把「可选」变成「必需」，与整套设计直接冲突。
        体检的价值在于**说清楚**，不在于拦住（那正是 `/api/health` 也在报同一件事的理由）。

        ⚠ 触发时机：`config.py` 末尾的模块级 `settings = get_settings()` —— **import 期**，
        早于 `api.py:49` 的 `logging.basicConfig`。所以这几行会经 `logging.lastResort`
        直接落到 stderr（格式是光秃秃一条消息，没有时间戳），排在所有带格式的日志之前。
        这是**故意的**：早于一切、绝不漏掉。别为了格式好看把它挪进 `_bootstrap()`——
        那样 `scripts/*.py` 与 `batch/*.py` 这些不跑 bootstrap 的入口就再也体检不到了。

        ⚠ 别指望它在运行期重跑：`mode="after"` 只在构造时执行一次。于是探针里那些
        `settings.rate_limit_per_minute = 3` 之类的**运行期赋值不会触发它**，
        各探针的既有行为逐字不变。
        """
        for line in _config_findings(self):
            logger.warning("配置体检：%s", line)
        return self

    # ---------- 派生属性 ----------
    @property
    def data_file_path(self) -> Path:
        return self._resolve(self.data_file)

    @property
    def index_path(self) -> Path:
        return self._resolve(self.index_dir)

    @property
    def model_cache_path(self) -> Path:
        return self._resolve(self.model_cache_dir)

    @property
    def qdrant_local_path_abs(self) -> Path:
        return self._resolve(self.qdrant_local_path)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def web_search_domain_list(self) -> list[str]:
        """联网搜索的域名白名单；留空表示不限。

        ⚠ 字段本身必须声明成 str（见上面那段注释）：pydantic-settings 对 list[str]
        走 JSON 解析，.env 里写逗号分隔会直接让服务起不来。这里手动切分。
        """
        return [d.strip() for d in self.web_search_domains.split(",") if d.strip()]

    @property
    def has_cloud_qdrant(self) -> bool:
        return bool(self.qdrant_url.strip() and self.qdrant_mode != "local")

    def mcp_server_bind(self) -> tuple[str, int, str]:
        """从 `mcp_server_url` **反解**出 `(host, port, path)` —— 服务器的监听三件套。

        ⚠⚠ 这是**同一个事实的两面**（客户端去哪儿调 / 服务器在哪儿听），所以刻意只有
        **一个**配置字段：另加 `mcp_server_host` / `mcp_server_port` 那种写法必然漂移，
        而漂移的表现是「三个工具全挂」，且看上去像服务器没起（见 CLAUDE.md
        「自建 MCP 服务器」）。服务器进程的 `--host/--port/--path` 默认值也取自这里，
        于是「改了 URL 却忘了改服务器参数」这件事从机制上不存在。

        留空时给的是**默认监听三件套**（`127.0.0.1:8765` 的 `/mcp`）—— 不是空值：
        服务器不带任何参数起时也要有个确定的落点，否则那个默认值就成了第三处事实。
        """
        raw = self.mcp_server_url.strip()
        if not raw:
            return ("127.0.0.1", 8765, "/mcp")
        parts = urlsplit(raw)
        host = parts.hostname or "127.0.0.1"
        # ⚠ 端口不写时**不用** URL scheme 的默认值（http=80）：那是给公网服务的惯例，
        #   而我们在回环上跑，80 既可能没权限又被别的东西占着。缺省仍取 8765。
        port = parts.port or 8765
        path = parts.path.rstrip("/") or "/mcp"
        return (host, port, path if path.startswith("/") else "/" + path)

    def _resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    _apply_hf_env(settings)
    return settings


def _apply_hf_env(settings: Settings) -> None:
    """必须在导入 huggingface_hub / sentence_transformers 之前调用。

    项目面向国内网络环境，默认走 hf-mirror 镜像下载模型。
    """
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # huggingface_hub >= 1.0 默认走 Xet 传输协议，hf-mirror 等镜像不支持，
    # 会导致下载静默卡死，这里强制回退到普通 HTTP 下载。
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    if settings.hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    if settings.hf_token:
        os.environ.setdefault("HF_TOKEN", settings.hf_token)


settings = get_settings()
