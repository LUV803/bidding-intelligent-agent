"""智谱 AI 文本客户端（GLM-4.7-FlashX，纯文本对话）。

与 `vision_client.py` 的分工：那个走 `zai-sdk` 的**视觉**模型 `glm-4.6v-flashx`
解析图片；这个走**智谱的 OpenAI 兼容端点**做纯文本对话。两者共用
`ZHIPU_API_KEY` / `ZHIPU_BASE_URL`，但**各建各的连接**。

⚠ 配置项一律沿用 `zhipu_*` 前缀（而不是新建 `zai_*`）：`.env` 里用户已经填了
`ZHIPU_API_KEY` / `ZHIPU_BASE_URL`，新建前缀会让老 `.env` 里的键因
`Settings(extra="ignore")` 被**静默丢弃**——本仓库记过四次的坑。模块名按 SDK
的导入名（`zai`）叫 `zai_client`，与配置前缀不一致是有意的。
"""

from __future__ import annotations

from ..config import settings
from .base_client import OpenAICompatLLM

__all__ = ["ZaiLLM"]


class ZaiLLM(OpenAICompatLLM):
    """智谱 GLM 文本模型（OpenAI 兼容端点）。

    ⚠ **「关思考」的写法没实测过**：`base_client.thinking_disabled_body` 默认给的是
    DeepSeek 那份 `{"thinking": {"type": "disabled"}}`，智谱文档里的形状看起来一致，
    但**没有真机验证**。它只在 `/api/agent` 的「列举型问句强制首轮调图谱」那一轮用到
    （具名 `tool_choice` 与思考模式互斥）。真出问题时的表现是那一轮 400、
    `_stream_round_forced_graph()` 降级重跑——**不会**让整条请求炸掉，但那一轮的
    思考区会是空的。改这里之前先跑：

        LLM_PROVIDER=zai uv run python scripts/probe_tools.py
    """

    _NOT_CONFIGURED = "未配置 ZHIPU_API_KEY，请在 .env 中填写后重试。"

    @property
    def model(self) -> str:
        return settings.zhipu_chat_model

    @property
    def api_key(self) -> str:
        # 与 vision_client 共用同一个 Key，但各建各的连接
        return settings.zhipu_api_key

    @property
    def base_url(self) -> str:
        return settings.zhipu_base_url

    @property
    def default_temperature(self) -> float:
        return settings.zhipu_chat_temperature

    @property
    def default_max_tokens(self) -> int:
        return settings.zhipu_chat_max_tokens
