"""工具层：`base`（引擎）+ `rag_tools`（具体工具）。

⚠ **本模块必须把两个子模块都拉进来**。`base.py` 的默认工具池是 `rag_tools` 在 import 期
用 `set_default_pool()` 交出去的，只 import `base` 的话那个池是空的 ——
`recover_tool_calls()` 会把一切泄漏标记判成「判不出归属」而丢掉。
（那是个**安全的**降级形态，不会抛异常，所以漏了也看不出错，只会静默失效。）

⚠ 下划线开头的名字**必须显式列出**：`from .base import *` 会跳过它们，而
`agent.py` 的 re-export 与几个探针要用的正是 `_peek_display` / `_match_schema` /
`_normalize_args` / `_duplicate` / `_HANDLERS`。这与 `llm.py` / `vision.py` 两个转发
shim 记的是同一条坑。
"""

from . import base, rag_tools
from .base import (
    BaseTool,
    SourceRegistry,
    ToolContext,
    ToolOutcome,
    assess_outcome,
    default_schemas,
    execute_tool,
    get_tool,
    outcome_from_payload,
    outcome_to_payload,
    recover_tool_calls,
    register,
    registered_names,
    registered_tools,
    set_default_pool,
    to_openai_schema,
    tolerant_int,
    _duplicate,
    _HANDLERS,
    _match_schema,
    _normalize_args,
    _peek_display,
    _resolve_enum,
)
from .rag_tools import (
    EXA_TOOL,
    GRAPH_TOOL,
    PG_TOOL,
    SEARCH_TOOL,
    SKILL_TOOL,
    TOOL_EXA,
    TOOL_GRAPH,
    TOOL_NAMES,
    TOOL_PG,
    TOOL_SCHEMAS,
    TOOL_SEARCH,
    TOOL_SKILL,
    TOOL_WEB,
    WEB_TOOL,
    exa_lookup,
    tools_for,
    web_lookup,
    _FORCE_GRAPH_CHOICE,
    _render_result,
)

__all__ = [
    "base",
    "rag_tools",
    # --- base ---
    "BaseTool",
    "SourceRegistry",
    "ToolContext",
    "ToolOutcome",
    "assess_outcome",
    "default_schemas",
    "execute_tool",
    "get_tool",
    "outcome_from_payload",
    "outcome_to_payload",
    "recover_tool_calls",
    "register",
    "registered_names",
    "registered_tools",
    "set_default_pool",
    "to_openai_schema",
    "tolerant_int",
    # --- rag_tools ---
    "EXA_TOOL",
    "GRAPH_TOOL",
    "PG_TOOL",
    "SEARCH_TOOL",
    "SKILL_TOOL",
    "TOOL_EXA",
    "TOOL_GRAPH",
    "TOOL_NAMES",
    "TOOL_PG",
    "TOOL_SCHEMAS",
    "TOOL_SEARCH",
    "TOOL_SKILL",
    "TOOL_WEB",
    "WEB_TOOL",
    "exa_lookup",
    "tools_for",
    "web_lookup",
    # --- 下划线名（显式列出，理由见模块 docstring）---
    "_FORCE_GRAPH_CHOICE",
    "_HANDLERS",
    "_duplicate",
    "_match_schema",
    "_normalize_args",
    "_peek_display",
    "_render_result",
    "_resolve_enum",
]
