"""兼容转发 → `bidding_qa.clients`（2026-09-20 迁移）。

⚠ **新代码请直接 `from .clients import ...`，别再用这个模块。** 留着它只为不打断
`scripts/` 下那几个探针与 CLAUDE.md 里的既有引用（全仓没有任何脚本断言模块路径字符串）。

⚠ **下划线开头的名字必须在这里显式列出**：`probe_markup.py` 用的是
`_MarkupSuppressor` 与 `_TOOL_MARKUP_RE` 那套，`from ... import *` 带不出它们
（`import *` 默认跳过下划线名），所以这里一个都不能省。

真实现所在的文件见 `clients/` 包的 docstring。
"""

from __future__ import annotations

from .clients.base_client import (
    _TOOL_MARKUP_RE,
    _TOOL_MARKUP_TOKEN_RE,
    BaseLLMClient,
    LLMEvent,
    LLMNotConfiguredError,
    OpenAICompatLLM,
    ToolCall,
    _MarkupSuppressor,
    strip_tool_markup,
)
from .clients.deepseek_client import DeepSeekLLM
from .clients.llm_factory import available_providers, get_llm, resolve_provider
from .clients.local_client import OllamaLLM, VllmLLM
from .clients.zai_client import ZaiLLM

__all__ = [
    "BaseLLMClient",
    "DeepSeekLLM",
    "LLMEvent",
    "LLMNotConfiguredError",
    "OllamaLLM",
    "OpenAICompatLLM",
    "ToolCall",
    "VllmLLM",
    "ZaiLLM",
    "_MarkupSuppressor",
    "_TOOL_MARKUP_RE",
    "_TOOL_MARKUP_TOKEN_RE",
    "available_providers",
    "get_llm",
    "resolve_provider",
    "strip_tool_markup",
]
