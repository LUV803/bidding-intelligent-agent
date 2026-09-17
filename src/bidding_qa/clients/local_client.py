"""本地推理客户端：vLLM 与 Ollama（都是 OpenAI 协议兼容端点）。

与 `deepseek_client.py` / `zai_client.py` 的关系：那两家是**云服务**，靠 API Key 鉴权；
这两家是**本机进程**，靠端口连通。差异只有两条，都收在 `_LocalLLM` 里：

| | 云服务（DeepSeek / GLM） | 本地（vLLM / Ollama） |
| --- | --- | --- |
| 开没开 | `api_key` 非空 | **`base_url` 非空** |
| 鉴权 | 真 Key | 不校验，但 SDK 拒空串 ⇒ 给占位符 |

⚠⚠ **`ready` 的判据在这里被改写（改成「base_url 非空」）是按 `.env` 的
「字段非空即为开」惯例**（同 `NEO4J_URI` / `POSTGRES_HOST` / `TAVILY_API_KEY`）。
副作用正是我们要的：**默认空串 ⇒ 本机开发环境里这两个供应商根本不出现**
（`available_providers()` 只列 `ready` 的），所以对现有行为**零影响**。
`.env.example` 里那两行也是空的，谁配谁开。

⚠ **vLLM 与 Ollama 不是两套 HTTP 实现**：两者都是 OpenAI 协议兼容，共享实现
（`OpenAICompatLLM`）一行都不用动，本文件只是**两份配置映射**
—— 同 `zai_client.py` 与 `deepseek_client.py` 的关系。
"""

from __future__ import annotations

from ..config import settings
from .base_client import OpenAICompatLLM

__all__ = ["OllamaLLM", "VllmLLM"]


class _LocalLLM(OpenAICompatLLM):
    """本地推理服务的共享基类。**不要直接实例化**，用下面两个子类。

    ⚠ 只实现 `api_key` / `ready` 两处**本地服务特有的**差异；
    `model` / `base_url` / `default_temperature` / `default_max_tokens` 仍由子类填
    —— 与 `zai_client.py` 同一形状，漏了在 import 期就会因 ABC 报错，不会静默。
    """

    @property
    def api_key(self) -> str:
        """本地服务不校验 Key，但 SDK 那一关过不去（`AsyncOpenAI` 拒空串）。

        所以给一个**非空占位符**。`base_client.client` 里那句
        `AsyncOpenAI(api_key=self.api_key.strip(), ...)` 照常能过。
        vLLM 官方文档给的示例用的也是这个形状。
        """
        return "EMPTY"

    @property
    def ready(self) -> bool:
        """⚠ **判据被改写**：基类是「api_key 非空」，本地服务看 `base_url`。

        纯读配置、不发网络请求（`/api/health` 每 30 秒轮询一次，不可能去 ping 服务）。
        所以它表达的是「**配了地址**」而不是「**服务真的活着**」——与
        `web_search.ENABLED`（配了 Key）vs「Tavily 真的应答」是同一条限制。
        服务没起来时的表现是提问时报连接错误，**不是**从胶囊里消失。
        """
        return bool(self.base_url.strip())


class VllmLLM(_LocalLLM):
    """vLLM（`vllm serve`）暴露的 OpenAI 兼容端点。

    启动示例（⚠ `--served-model-name` 必须与 `VLLM_MODEL` **逐字一致**，否则 404 ——
    注意「磁盘目录名」与「服务名」是两回事，`Qwen3.5-9B` 是前者、`qwen3.5:9b` 是后者）：

        vllm serve /root/autodl-tmp/models/Qwen3.5-9B \\
          --served-model-name qwen3.5:9b \\
          --port 8001 --enable-auto-tool-choice \\
          --tool-call-parser qwen3_coder --reasoning-parser qwen3

    ⚠⚠ **两个 parser 都在真机上验过**（2026-09-27，Qwen3.5-9B / vLLM 0.29），
    它们决定 `/api/agent` 的工具能不能用，而**配错的表现全是静默的**：

    - **`--tool-call-parser` 必须是 `qwen3_coder`**（本文档原先写的候选是
      `hermes` / `deepseek_v3`，**两个都不对**）。Qwen3.5 吐的是
      `<tool_call><function=NAME><parameter=ARG>…</parameter></function></tool_call>`
      这套 **Coder XML**，而 `hermes` 期望的是 `{"name":…,"arguments":{…}}` 的 JSON。
      配错的实测形状：**`tool_calls` 字段整个不存在**、`finish_reason` 是 `"stop"`、
      整段标记原样落在 `content` 里 —— 与「模型不愿意调工具」长得一模一样。
    - **`--reasoning-parser` 不加，思维链会漏进正文**：实测 `content` 末尾挂着一个
      游离的 `</think>`，而 `reasoning` 字段**不存在**。加上 `qwen3` 后两者才正确分开。

    ⚠ 与 DeepSeek 的**两条服务端约束在 vLLM 上都不存在**（具名 `tool_choice` 与思考
    互斥、带 `tool_calls` 的 assistant 消息缺 `reasoning_content` 会 400），
    所以 `probe_tools.py` 的 ⑤/⑤b 在这里会打 `[SKIP]`（判据是 `type(llm).__name__`）
    —— 那是**预期行为，不是回归**。
    """

    _NOT_CONFIGURED = "未配置 VLLM_BASE_URL，请在 .env 中填写 vLLM 服务地址后重试。"

    @property
    def model(self) -> str:
        return settings.vllm_model

    @property
    def base_url(self) -> str:
        return settings.vllm_base_url

    @property
    def default_temperature(self) -> float:
        return settings.vllm_temperature

    @property
    def default_max_tokens(self) -> int:
        return settings.vllm_max_tokens

    @property
    def thinking_disabled_body(self) -> dict:
        """vLLM 走 **chat template 参数**，不是 DeepSeek 那套 `{"thinking": …}`。

        ⚠ 这条**没有真机验证**（本仓库当前没有在跑的 vLLM）。它只在 `/api/agent`
        的「列举型问句强制首轮调图谱」那一轮用到（具名 `tool_choice` 与思考模式互斥）。
        `DeepSeek-R1-0528-Qwen3-8B` 是 Qwen3 架构，其模板认 `enable_thinking`；
        若模板不认这个变量，Jinja 把它当未定义处理，**不会报错**。
        真出问题时的表现是那一轮 400、`_stream_round_forced_graph()` 降级重跑。
        """
        return {"chat_template_kwargs": {"enable_thinking": False}}


class OllamaLLM(_LocalLLM):
    """Ollama 暴露的 OpenAI 兼容端点（默认 `http://127.0.0.1:11434/v1`）。

    ⚠⚠ **`supports_tools` 默认是 `False`，但这个默认值是个「保守起点」而不是结论。**
    判据**只能实测**，一条命令就见分晓：

    ```bash
    ollama show <模型名>      # 看 Capabilities 那行有没有 tools
    ```

    **为什么默认 `False`**：官方 `deepseek-r1:8b` 的模板里确实**没有 tools 段**，
    带 `tools` 的请求会被服务端直接拒掉（`... does not support tools`，
    ollama/ollama#15602）。而 `/api/agent` 是前端唯一链路、**每轮必定带 tools**，
    不降级的话「选 Ollama 提问」= 一个字都出不来。

    降级后的行为（`.env` 的 `OLLAMA_SUPPORTS_TOOLS=false`）：本轮不挂任何工具，
    走「预检索 + 直接作答」。法规类问题照答，问图谱/数据库的问题答不了 ——
    因为工具确实没挂出去，不是答错了。

    ⚠⚠ **`qwen3.5` 系（默认模型）是支持工具的**，但 Ollama 侧的落地情况**说法不一**，
    所以别照着「Qwen 支持工具」这句话就把开关拨到 `true`：
    - 支持的一面：Parallel 的工具调用文档把 Qwen 3.5 列为原生支持、且最可靠的一类。
    - 存疑的一面：ollama#14795 有 `Qwen3.5:0.8b does not support tools` 的报障
      （那是**从 HF 导入**、模板靠 GGUF 元数据自动推导的，**未必适用于官方库模型**）；
      PR #14167 的讨论则称 Ollama 主线上 Qwen3.5 接的是 Qwen3 的 Hermes JSON 解析器，
      而它训练用的是 Qwen3-Coder 的 XML 格式 —— 即「认下来了但解析不对」。
    ⇒ **`ollama show` 说有 `tools` 不等于调用一定能跑通**。拨成 `true` 之后务必用
    `scripts/probe_tools.py` 真机验一遍，别只看接口没报错。

    ⚠ **24G 显存上 Ollama 建议跑 CPU**（与 vLLM 那个 8B 争显存会装不下两个）：
    9B Q4 约 6GB，60G 内存绰绰有余，代价是 5~10 tok/s。
    """

    _NOT_CONFIGURED = "未配置 OLLAMA_BASE_URL，请在 .env 中填写 Ollama 服务地址后重试。"

    @property
    def model(self) -> str:
        return settings.ollama_model

    @property
    def base_url(self) -> str:
        return settings.ollama_base_url

    @property
    def default_temperature(self) -> float:
        return settings.ollama_temperature

    @property
    def default_max_tokens(self) -> int:
        return settings.ollama_max_tokens

    @property
    def supports_tools(self) -> bool:
        """⚠ **property 而不是类常量** —— 它读活的 `settings`。

        写成类属性会在 import 期定死，改了 `.env` 不重启不生效；
        而本仓库对这类「import 期快照」有明确纪律（同 `available_providers()`
        读活 `settings` 那条）。
        """
        return settings.ollama_supports_tools

    @property
    def thinking_disabled_body(self) -> dict:
        """Ollama 不认 DeepSeek 那套关思考的写法，返回空 ⇒ 不加 `extra_body`。

        （`base_client` 拿到空 dict 时合进去等于没变。真正要关掉 R1 的思考得靠
        `--think=false` 之类的启动参数，不在本 property 的能力范围内。）
        """
        return {}
