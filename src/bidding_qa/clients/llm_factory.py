"""文本大模型客户端工厂：按供应商名取客户端，**每个供应商一个进程内实例**。

供应商从哪来（两维，别合并）：

| 维度 | 来源 | 什么时候定 |
| --- | --- | --- |
| **默认** | `settings.llm_provider`（`.env` 的 `LLM_PROVIDER`） | 进程启动 |
| **每请求覆盖** | `ChatRequest.llm_provider`（前端那个胶囊） | 每次提问 |

`get_llm()` 不传参 ⇒ 用默认，行为与 2026-09-20 之前**逐字相同**（全仓 16 处调用点都这么用）。

加第 N 家要做的事：写一个 `xxx_client.py`（继承 `OpenAICompatLLM`，把配置映射成 property）
→ 在下面 `_PROVIDERS` 里加一条 → 在 `_LABELS` 里加一条短名 → `.env.example` 里把可选值列上。
**共享逻辑一行都不用动。**

⚠ 但**「开没开」的判据各家可以不同**：云服务看 `api_key` 非空（基类默认），
本地推理看 `base_url` 非空（`local_client._LocalLLM` 覆写了 `ready`）。
差异只该收在各自的客户端里，**不要**漏到本模块来 —— 这里一律只问 `llm.ready`。
"""

from __future__ import annotations

import logging

from ..config import settings
from .base_client import BaseLLMClient
from .deepseek_client import DeepSeekLLM
from .local_client import OllamaLLM, VllmLLM
from .zai_client import ZaiLLM

logger = logging.getLogger(__name__)

#: 供应商名 → 实现。键就是 `.env` 里 `LLM_PROVIDER` 与请求体里 `llm_provider` 的取值（小写比较）
#:
#: ⚠⚠ **这里的顺序 = 前端那个胶囊的循环顺序**（`available_providers()` 按 dict 声明序
#: 输出，`ChatInput.cycleLlm()` 对它取模循环）。所以新增供应商**一律追加在末尾**，
#: 中间插队会改变用户已经习惯的点击次序。
_PROVIDERS: dict[str, type[BaseLLMClient]] = {
    "deepseek": DeepSeekLLM,
    "zai": ZaiLLM,
    # 本地推理：默认 `base_url` 为空 ⇒ 不 ready ⇒ **不出现在胶囊里**，
    # 所以这两条对本机（没跑 vLLM / Ollama 的机器）是零影响。
    "vllm": VllmLLM,
    "ollama": OllamaLLM,
}

#: 供应商名 → 前端胶囊上显示的**短名**。键必须与 `_PROVIDERS` 一致。
#: ⚠ 漏一条不会报错，只是回落到键名本身（`zai` 会显示成 "zai" 而不是 "GLM"）——
#:   `probe_llm_provider.py` 里有断言挡这个。
_LABELS: dict[str, str] = {
    "deepseek": "DeepSeek",
    "zai": "GLM",
    "vllm": "vLLM",
    "ollama": "Ollama",
}

#: 名字认不出来时回退到哪家。必须是 `_PROVIDERS` 的键。
_DEFAULT_FALLBACK = "deepseek"

#: 按供应商名缓存。⚠ **缓存住实例同时也就缓存住了** `OpenAICompatLLM._client` 那个懒建的
#: `AsyncOpenAI`（`base_client.py` 的 `client` property）—— 每次新建实例会丢掉连接池。
_llms: dict[str, BaseLLMClient] = {}


def resolve_provider(name: str | None = None) -> str:
    """把外部给的供应商名归一化成一个**一定存在于 `_PROVIDERS` 里的键**。

    `None` / `""` / 全空白 ⇒ 回落到 `settings.llm_provider`（即「不传就是默认」）。

    ⚠ **认不出来时降级而非报错**（同 graph_store / pg_store / QDRANT_MODE=auto 的既有惯例）：
    拼错一个环境变量、或前端传来一个不存在的名字，都不该让整个服务起不来或让这条请求 500，
    问答照常，日志里留一行真话。
    """
    raw = (name or "").strip().lower()
    if not raw:
        raw = settings.llm_provider.strip().lower()
    if raw in _PROVIDERS:
        return raw

    logger.warning(
        "供应商名 %r 不是已知的（可选：%s），本次回退到 %s。",
        raw,
        "、".join(_PROVIDERS),
        _DEFAULT_FALLBACK,
    )
    return _DEFAULT_FALLBACK


def get_llm(provider: str | None = None) -> BaseLLMClient:
    """取（必要时构造）某个供应商的客户端。`provider=None` ⇒ 服务端默认那家。

    ⚠ **无锁**，与改动前一致：构造只给 `._client` 赋个 None，不建连接、不读 Key，
    没有需要串行化的东西。（`embedding` / `reranker` 那边有锁，是因为它们真加载模型。）

    ⚠ 名字**合法但没配好**时照办、返回那个实例（`ready` 为 False），
    **绝不悄悄换成另一家** —— 那会让用户拿到一个他没要的模型的答案，且界面上看不出来。
    让它如实走 `_NOT_CONFIGURED` 那条报错路径（云服务是「没配 Key」，本地是「没配地址」）。
    前端只会把 `ready` 的列出来，所以正常点选路径上碰不到这种；能碰到的只有手写的请求体。
    """
    name = resolve_provider(provider)
    llm = _llms.get(name)
    if llm is None:
        llm = _PROVIDERS[name]()
        _llms[name] = llm
        logger.info("文本大模型客户端：%s（%s）", type(llm).__name__, llm.model)
    return llm


def available_providers() -> list[dict[str, str]]:
    """`/api/health` 用：**真的配了 Key** 的供应商，给前端胶囊列选项。

    只列 `ready` 的 —— 没配 Key 的选项点了必然失败，摆出来是骗人。

    ⚠ 顺序就是 `_PROVIDERS` 的声明顺序（dict 保序），前端按它取模循环，所以这里是稳定的。
    ⚠ 会顺手把各家的实例建出来（缓存的，只建一次）。`ready` / `model` 都是**纯读配置、
    不发网络请求**的 property，所以每 30 秒轮询一次健康检查是安全的。
    """
    out: list[dict[str, str]] = []
    for name in _PROVIDERS:
        llm = get_llm(name)
        if llm.ready:
            # label 兜底回落到键名：漏配 _LABELS 时显示成 "zai" 总好过整个胶囊是空的
            out.append({"name": name, "label": _LABELS.get(name, name), "model": llm.model})
    return out
