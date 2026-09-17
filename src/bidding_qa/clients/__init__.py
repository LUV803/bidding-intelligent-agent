"""大模型客户端层：文本（多供应商）与视觉。

```
base_client.py     BaseLLMClient（契约）+ OpenAICompatLLM（共享实现）
                   + ToolCall / LLMEvent / _MarkupSuppressor / strip_tool_markup
llm_factory.py     get_llm()：按 LLM_PROVIDER 选供应商
deepseek_client.py DeepSeek（默认）
zai_client.py      智谱 GLM-4.7-FlashX（纯文本）
local_client.py    vLLM / Ollama（本地推理，**同样走 OpenAI 兼容协议**）
vision_client.py   智谱 glm-4.6v-flashx（图片解析，**同步**，不属于 LLM 契约）
```

⚠ `vision_client` **不在这里 import**：它走 `zai-sdk` 而不是 `openai`，形状也不同
（同步 + 结构化提取，没有 `stream`）。要用请显式
`from .clients import vision_client`——别为了「整齐」把它塞进来，
那会让 `import clients` 拖上视觉 SDK 的依赖。
"""

from __future__ import annotations

from .base_client import (
    BaseLLMClient,
    LLMEvent,
    LLMNotConfiguredError,
    OpenAICompatLLM,
    ToolCall,
    strip_tool_markup,
)
from .deepseek_client import DeepSeekLLM
from .llm_factory import available_providers, get_llm, resolve_provider
from .local_client import OllamaLLM, VllmLLM
from .zai_client import ZaiLLM

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
    "available_providers",
    "get_llm",
    "resolve_provider",
    "strip_tool_markup",
]
