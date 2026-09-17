"""DeepSeek 文本客户端。

本文件**只有配置映射**：流式拼装、工具调用、标记抑制全在
`base_client.OpenAICompatLLM` 里。要看行为改那里，这里只回答「DeepSeek 的
模型名/密钥/地址/默认采样参数是什么」。
"""

from __future__ import annotations

from ..config import settings
from .base_client import OpenAICompatLLM

__all__ = ["DeepSeekLLM"]


class DeepSeekLLM(OpenAICompatLLM):
    """DeepSeek（OpenAI 兼容接口）。

    ⚠ **类名与 `_client` 属性名别改**：`scripts/probe_markup.py` 直接
    `DeepSeekLLM()` 零参构造、再把假客户端写进 `llm._client`；
    `scripts/probe_tools.py` 用的是 `llm.client.chat.completions.create(...)`。
    """

    _NOT_CONFIGURED = "未配置 DEEPSEEK_API_KEY，请在 .env 中填写后重试。"

    @property
    def model(self) -> str:
        return settings.deepseek_model

    @property
    def api_key(self) -> str:
        return settings.deepseek_api_key

    @property
    def base_url(self) -> str:
        return settings.deepseek_base_url

    @property
    def default_temperature(self) -> float:
        return settings.deepseek_temperature

    @property
    def default_max_tokens(self) -> int:
        return settings.deepseek_max_tokens

    # timeout 沿用基类的 60.0（原实现在 client property 里就是写死这个数）
