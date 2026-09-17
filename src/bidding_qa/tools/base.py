"""工具引擎：**与具体能力无关**的那一层。

这里装着「一个工具是什么、怎么被调用、怎么把结果回传给模型」的全部机制，
但一个具体工具都没有——四个真实工具在 `rag_tools.py`。

分层理由（与 `clients/` 同一条）：`agent.py` 曾经既是对话主循环又是工具仓库，
1431 行里约 1000 行与工具有关。搬出来之后，加一个工具的落点清楚了。

依赖方向（**唯一的硬约束**）：本模块只能 import
``..config / ..schemas / ..clients``，**绝不 import ``..agent``**，也绝不 import
``.rag_tools`` —— 后者要 import 本模块拿 ``BaseTool`` / ``ToolContext``，反向依赖立刻成环。
所以「默认工具池」（`_match_schema` / `recover_tool_calls` 的兜底）走**注册表**：
`rag_tools.py` 在 import 期把每个工具 `register()` 进来，本模块只认注册表不认人。

四条设计要点，改之前先读：

1. ⚠⚠ **`to_openai_schema()` 的清洗不能省**。`_normalize_args()` 直接读裸 JSON Schema 键
   （``prop["enum"]`` / ``prop.get("type") == "integer"``），而 Pydantic 的
   `model_json_schema()` 产出形状不同：对 ``Optional[int]`` 它只给 ``anyOf``，
   **没有顶层 ``type``**。`_normalize_args` 用的是 `.get` 所以不报错，它会**掉进 else
   分支把原值原样收下** —— 恢复路径传进来的是字符串，于是 `top_k="5"` 变成字符串 `"5"`
   而不是 `5`。不崩溃、不报错，只是类型悄悄变了，正是本仓库最警惕的那类故障。
2. **工具执行永不抛异常**（`execute_tool`）。工具报错却漏掉 ``role="tool"`` 的响应消息，
   下一轮请求会因为 ``tool_call_id`` 无人匹配直接 400。
3. **宽容优先于严格**。Args 模型里的 ``mode="before"`` 校验器一律**只归一、不拒绝**
   （解析不出整数就回 ``None``，让 handler 走它原有的兜底），因为现有容错是逐条实测标定过的。
4. ⚠⚠ **`ToolOutcome` 的字段集是「跨进程契约」**。三个工具可以经 MCP 走独立进程
   （见 `mcp_bridge` 与 CLAUDE.md「自建 MCP 服务器」），那条路上 `ToolOutcome` 要编成
   dict、过 JSON、再解回来 —— 编解码就是紧跟 `ToolContext` 的 `outcome_to_payload()` /
   `outcome_from_payload()`。**本文件里唯一一处「加字段要记得改别的文件」**，
   由 `probe_mcp_server.py` ② 的字段集断言钉着（见那两个函数的 docstring）。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from ..clients import ToolCall, strip_tool_markup
from ..schemas import GraphResult, PgResult, Source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- schema 生成
def _drop_noise(prop: dict) -> dict:
    """剥掉 Pydantic 产出里对模型没意义的两样东西。

    - ``title``：Pydantic 把字段名转成英文标题（``top_k`` → ``"Top K"``），纯噪声。
    - ``default: null``：本项目每个参数字段的默认值都是 ``None``，语义是「这个参数
      可不填」，而**今天手写的 schema 里一个 ``default`` 都没有**。留着它等于凭空
      多出一批「默认值是 null」的暗示（模型可能据此显式吐一个 null 回来）。
      同理，删掉之后清洗结果是**逐字节等于改动前的手写 schema** 的 —— 那是个能直接
      写成断言的强性质，比「差不多」值钱得多。
    """
    out = {k: v for k, v in prop.items() if k != "title"}
    if out.get("default") is None:
        out.pop("default", None)
    return out


def _clean_property(prop: dict) -> dict:
    """把 Pydantic 的属性 schema 洗成 `_normalize_args` 认得的形状。

    做三件事：**剥 ``title``**（模型看不懂中文 title，纯噪声）、**展平单分支 `anyOf`**
    （``Optional[int]`` 的产物）、**保留多分支 `anyOf`**。

    ⚠ 最后那一条是实测踩出来的：初版写成「``anyOf`` 一律展平」，于是 ``float | str``
    （两个非 null 分支）被整块丢掉，字段变成**没有任何类型信息**。保留成
    ``anyOf: [{type:number},{type:string}]`` 才对——它是合法 JSON Schema，
    模型据此知道 ``"50万"`` 与 ``50000`` 都行，而 `_normalize_args` 落到 else 分支
    原样透传给 `pg_store` 自己的解析器。
    """
    variants = prop.pop("anyOf", None)
    if not variants:
        return _drop_noise(prop)
    real = [_drop_noise(v) for v in variants if v.get("type") != "null"]
    if len(real) == 1:
        # 单分支（Optional[X]）：把内容提到顶层，保住 description 等外层键
        merged = dict(real[0])
        merged.update(prop)
        return _drop_noise(merged)
    return _drop_noise({"anyOf": real, **prop})


def to_openai_schema(
    model: type[BaseModel], *, name: str, description: str, required: tuple[str, ...] = ()
) -> dict:
    """Pydantic 模型 → OpenAI 工具 schema。

    ``required`` 由各工具**显式声明**，不从模型的必填性推导：Args 模型里的字段一律给了
    宽松默认值（缺失时交给 handler 报出它自己的友好文案），而 schema 的 ``required``
    是**给模型看的提示**。两者分开是有意的——把模型的必填性直接投影上去，
    会让「query 缺失」从「返回一句可读的中文错误」变成一次 ValidationError。
    """
    raw = model.model_json_schema()
    params: dict[str, Any] = {
        "type": "object",
        "properties": {k: _clean_property(v) for k, v in raw.get("properties", {}).items()},
        # ⚠ **无条件**写进去，哪怕它是空列表。写成 `if required:` 会让采购记录工具少掉
        #   这个键 —— 它恰恰是**唯一**没有必填参数的工具，而「一个必填参数都没有」正是
        #   `recover_tool_calls` 那条「名字对上了但一个参数都不沾就退回投票」守卫的前提。
        #   空列表与键缺失对模型语义相同，所以这种差异没人会注意到 —— 正因如此，
        #   `probe_recover.py` 用例 0 除了比对 `params["required"]` 的值，还**单独**断言
        #   这个键**存在**（缺键在那里会先 KeyError 崩掉，不是一次可读的 FAIL）。
        "required": list(required),
    }
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


def tolerant_int(value: Any) -> int | None:
    """能转就转，转不了回 ``None``（**不抛异常**）。

    `mode="before"` 校验器专用：回 ``None`` 等于「模型没给这个参数」，
    而每个 handler 对「没给」本来就有兜底分支。抛出去就变成一次硬失败，
    那是把现有的宽容行为改掉了。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 注册表
_REGISTRY: dict[str, BaseTool] = {}
# 工具名 → 执行体。与 `_REGISTRY` 同生同长，**不是第二份事实来源**，只是给「我只想要
# 那张 name→callable 的表」的调用方一个视图（`agent.py` 把它 re-export 成 `_HANDLERS`，
# `probe_pg_store.py` 钉着那个名字）。
_HANDLERS: dict[str, Callable[[dict, ToolContext], Awaitable[ToolOutcome]]] = {}


def register(tool: BaseTool) -> None:
    """登记一个工具。由 `rag_tools.py` 在 import 期调用。

    ⚠ 重复登记**直接报错**而不是覆盖：工具名同时是模型的调用依据、恢复机制的归属依据
    与前端展示表的键，撞名是必须当场炸掉的那种错（照 `agent.py` 里
    `QUERY_TYPE_LABELS` 那个 import 期 assert 的惯例）。
    """
    if tool.name in _REGISTRY:
        raise ValueError(f"工具名重复登记: {tool.name!r}")
    _REGISTRY[tool.name] = tool
    _HANDLERS[tool.name] = tool.run


def registered_tools() -> list[BaseTool]:
    return list(_REGISTRY.values())


def get_tool(name: str) -> BaseTool | None:
    return _REGISTRY.get(name)


def registered_names() -> tuple[str, ...]:
    """已登记的工具名。

    ⚠ **这是「引擎认识谁」，不是「本轮挂给模型的是谁」**。挂出去的那批是
    `ctx.tool_names`（由 `run_agent_stream` 从 `tools_for()` 的结果注入）。
    两者不能互相顶替，见 `execute_tool` 里那段注释。
    """
    return tuple(_REGISTRY)


# ---------------------------------------------------------------- 默认工具池
# 「进程级」的那批工具（= 能力模块 ENABLED 过滤之后的 `TOOL_SCHEMAS`），供
# `_match_schema` / `recover_tool_calls` 的 `schemas=None` 兜底使用。
#
# ⚠ 为什么不直接用 `registered_schemas()`（注册表里全部工具）：**注册 ≠ 挂出去**。
#   图谱/采购库没配好时（`graph_store.ENABLED` 为假）它们**不在** `TOOL_SCHEMAS` 里，
#   于是模型漏进正文的图谱形态标记应当**判不出归属**、整条丢弃；换成注册表兜底的话
#   它会「恢复成功」，然后执行一个必然失败的工具调用，白吃一轮。
#
# ⚠ 也不能在这里缓存成一个 list：`eval_rag.py --pipeline agent` 会**替换**
#   `rag_tools.TOOL_SCHEMAS`（摘掉图谱/采购库）来测「只会检索的 Agent」，而它替换的是
#   模块全局。所以这里存的是一个**读的时候才去取**的 provider，而不是快照 —— 与搬迁前
#   `_match_schema` 直接读 `TOOL_SCHEMAS` 全局的时机逐字相同。
_POOL_PROVIDER: Callable[[], list[dict]] | None = None


def set_default_pool(provider: Callable[[], list[dict]]) -> None:
    """由 `rag_tools.py` 在 import 期调用，交出「怎么取进程级工具池」。"""
    global _POOL_PROVIDER
    _POOL_PROVIDER = provider


def default_schemas() -> list[dict]:
    """进程级工具池。`rag_tools` 尚未 import 时为空 —— `_match_schema` 返回 None、
    `recover_tool_calls` 返回空列表，都是安全的降级形态（不会抛）。"""
    return _POOL_PROVIDER() if _POOL_PROVIDER is not None else []


# ---------------------------------------------------------------- 数据结构
class SourceRegistry:
    """全局引用编号表。

    ``Retriever._to_source()`` 分配的 ``index`` 是**每次查询都从 1 重新开始**的，
    直接用会让工具结果的编号与预检索撞车。这里统一改发全局连续编号：

    - 新 doc_id → 追加编号 N+1
    - 已见的 doc_id → **复用首次那个 Source 对象**（编号、得分、融合位次都沿用），
      否则前端「重排位次升降」会错乱
    """

    def __init__(self, *, cap: int) -> None:
        self._by_id: dict[str, Source] = {}
        self._ordered: list[Source] = []
        self._cap = cap

    def add(self, sources: list[Source]) -> tuple[list[Source], list[Source], int]:
        """登记一批来源，返回 (新增, 已存在, 因超上限丢弃的条数)。"""
        fresh: list[Source] = []
        known: list[Source] = []
        dropped = 0
        for src in sources:
            existing = self._by_id.get(src.doc_id)
            if existing is not None:
                known.append(existing)
                continue
            if len(self._ordered) >= self._cap:
                dropped += 1
                continue
            stamped = src.model_copy(update={"index": len(self._ordered) + 1})
            self._by_id[src.doc_id] = stamped
            self._ordered.append(stamped)
            fresh.append(stamped)
        return fresh, known, dropped

    def dump(self) -> list[Source]:
        return list(self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)


@dataclass
class ToolOutcome:
    """一次工具执行的结果。text 永远非空。"""

    text: str
    ok: bool
    new_sources: list[Source] = field(default_factory=list)
    new_indices: list[int] = field(default_factory=list)
    known_indices: list[int] = field(default_factory=list)
    # 命中却**因 SourceRegistry 撞顶而未登记**的条数（见 SourceRegistry.add 第三个返回值）。
    # ⚠⚠ 它存在的唯一理由是让 `empty_reason()` 能区分两种**形状完全相同**的结局：
    #   「库里一条没匹配到」（真的空手）与「匹配到了、但来源额度已满」（找到了没给出）。
    # 少了它，`new_sources` 与 `known_indices` 双双为空会被一律读成前者，于是
    # `assess_outcome()` 在工具文本刚说完「另有 N 条超出本次来源上限」的**下一行**
    # 告诉模型「没有拿到任何结果…可以如实说没有查到」—— 两句直接打架。
    # 图谱/采购库不走登记表，恒为 0。
    dropped: int = 0
    elapsed_ms: float = 0.0
    error: str | None = None
    # 图谱工具的结构化结果 → 前端独立的「知识图谱查询」块。检索工具恒为 None。
    # 注意：图谱结果**不进 SourceRegistry、不发 sources、正文不标 [n]**——
    # Source 是问答对形状（question/answer/snippet），图谱行是「实体+统计」，语义不同。
    graph: GraphResult | None = None
    # 采购记录工具的结构化结果 → 前端独立的「采购数据库查询」块。理由同上。
    pg: PgResult | None = None


@dataclass
class ToolContext:
    """一次 Agent 请求内共享的工具执行上下文。

    把 retriever / mode / rerank / seen 收进一个对象，是为了让「新增一个工具」=
    「加一个 handler」，而不是再加一串参数。
    """

    registry: SourceRegistry
    retriever: Any  # Retriever；这里只调用它的 .search，不 import 以免多一条依赖
    mode: str
    rerank: bool | None
    seen: set[tuple] = field(default_factory=set)
    # 本轮**实际挂出去**的工具名。默认空 = 退回进程级那批（见 execute_tool）。
    # ⚠ execute_tool 的「没有名为 X 的工具」那条报错文案要用它，不能直接用 registered_names()：
    # 开着联网时进程级那批不含 search_web，会把一个逐字正确的工具名报成不存在的，
    # 白送模型一次「换个工具试试」的乱猜。
    tool_names: tuple[str, ...] = ()


def outcome_to_payload(outcome: ToolOutcome) -> dict:
    """`ToolOutcome` → **可 JSON 化**的 dict。与 `outcome_from_payload()` 是一对。

    用在三个走 MCP 的工具上（`rag_tools` 里那三个 `if mcp_bridge.is_enabled():` 分支）：
    服务器进程把结果编成这个 dict 交给 MCP 协议，客户端进程再解回 `ToolOutcome`。
    于是**两边的 `ToolOutcome` 是同一个类的两个副本**，中间隔了一次 JSON。

    ⚠⚠ **字段集必须与 dataclass 逐字段相等**，**包括** `new_sources` / `new_indices` /
    `known_indices` 这三个对那三个工具**恒为空**的字段。理由不是洁癖，而是
    `probe_mcp_server.py` ② 那条 `set(payload) == {f.name for f in fields(ToolOutcome)}`
    —— 它是整个 codec 的支点：**日后往 `ToolOutcome` 加字段而忘了这里，红的是探针**，
    而不是「前端那个块莫名其妙渲染成一片空白」。写成「除这三个以外」就等于把那道防线
    拆掉一半，换来的不过是省下三个空列表。

    ⚠ 也正因为它必须是**手写**的（用 `fields()` 自动派生的话那条断言就成了空转，见上），
    字段名与 dataclass 的一致靠探针钉，**不靠自觉**。

    ⚠ `GraphResult` / `PgResult` / `Source` 都是 pydantic，所以 `model_dump()` 直接可用；
    它们内部**没有** `Decimal` / `date`（那些在 `pg_store` 构造时就转成了 float / ISO 串），
    否则会在 MCP 服务器那句 `json.dumps` 处炸 —— 与 `api.py` 那条是同一次教训。
    """
    return {
        "text": outcome.text,
        "ok": outcome.ok,
        "new_sources": [s.model_dump() for s in outcome.new_sources],
        "new_indices": list(outcome.new_indices),
        "known_indices": list(outcome.known_indices),
        "dropped": outcome.dropped,
        "elapsed_ms": outcome.elapsed_ms,
        "error": outcome.error,
        "graph": outcome.graph.model_dump() if outcome.graph is not None else None,
        "pg": outcome.pg.model_dump() if outcome.pg is not None else None,
    }


def outcome_from_payload(payload: dict) -> ToolOutcome:
    """`outcome_to_payload()` 的逆。**契约破坏时抛，缺可选字段时降级**。

    ⚠⚠ 这是有意的一处**不对称**，两边各自都有道理：

    - `text` / `ok` **缺失就抛 `ValueError`**。少了它们这个载荷根本无从解释，
      而「返回一个 text 为空串的 ToolOutcome」会让模型收到一条**空的** `role:"tool"`
      消息 —— 那是本仓库最警惕的那种「看起来正常、其实是坏的」形态。
      抛出去由 `execute_tool` 的最后一道闸转成一句如实的中文失败，正好。
    - 其余字段一律 `.get(名字, 默认值)`。它们的默认值就是 dataclass 自己的默认值，
      所以「服务器是旧版本、少发了一个字段」会退化成**加这个字段之前的行为**，
      而不是整条调用失败。版本偏移在这里是真实存在的（两个进程各自重启）。

    ⚠ `graph` / `pg` 的**嵌套**结构走 pydantic 的 `model_validate`：形状不对会抛
    `ValidationError`，同样由 `execute_tool` 兜住 —— 这里刻意不 try/except，
    因为「解不出来」和「没这个字段」是两件事，前者不该被静默吞掉。
    """
    try:
        text = payload["text"]
        ok = payload["ok"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"MCP 返回的载荷缺少必需字段（{exc}）") from None

    def _model(cls: Any, key: str) -> Any:
        raw = payload.get(key)
        return cls.model_validate(raw) if raw is not None else None

    return ToolOutcome(
        text=str(text),
        ok=bool(ok),
        new_sources=[Source.model_validate(s) for s in payload.get("new_sources") or []],
        new_indices=[int(i) for i in payload.get("new_indices") or []],
        known_indices=[int(i) for i in payload.get("known_indices") or []],
        dropped=int(payload.get("dropped", 0) or 0),
        elapsed_ms=float(payload.get("elapsed_ms", 0.0) or 0.0),
        error=payload.get("error"),
        graph=_model(GraphResult, "graph"),
        pg=_model(PgResult, "pg"),
    )


def _duplicate() -> ToolOutcome:
    return ToolOutcome(
        "该调用已执行过，结果与上次完全相同。请基于已有信息作答，不要重复调用。",
        ok=True,
        error="duplicate_query",
    )


# ---------------------------------------------------------------- 工具基类
class BaseTool(ABC):
    """一个工具的定义：名字、给模型看的描述、参数模型、执行体。

    子类只需填 ``name`` / ``description`` / ``args_model`` / ``schema_required``
    并实现 `run()`，schema 由 `to_openai_schema()` 生成——**描述与校验不可能漂移**，
    它们在同一个类里。

    ⚠ 本类是**真 ABC**，但**不要在任何地方对它做 ``isinstance`` 运行期校验**：
    同 `clients/base_client.py` 的 `BaseLLMClient`，这里的抽象只用来说明形状。
    探针们走的是 `execute_tool()` 这个真实入口，不构造替身工具，所以加校验目前不会
    坏事——但那条纪律值得一并守着，省得日后有人加一个把替身挡在门外的关卡。
    """

    #: 模型看到的工具名。**同时是**恢复机制的归属依据与前端展示表的键。
    name: ClassVar[str]
    #: 给模型看的用途说明（写清「适用/不适用」，路由质量几乎全靠它）
    description: ClassVar[str]
    #: 参数模型。schema 由它生成，`parse_args()` 用它做形状校验。
    args_model: ClassVar[type[BaseModel]]
    #: 写进 schema 的 ``required``。**显式声明**，不从模型的必填性推导（见 to_openai_schema）
    schema_required: ClassVar[tuple[str, ...]] = ()
    #: **名字限定**的别名。只有在泄漏标记写的工具名精确命中本轮工具表时才生效。
    name_scoped_aliases: ClassVar[dict[str, str]] = {}

    def to_openai_schema(self) -> dict:
        return to_openai_schema(
            self.args_model,
            name=self.name,
            description=self.description,
            required=self.schema_required,
        )

    def parse_args(self, raw: dict) -> tuple[dict, str | None]:
        """形状校验，返回 ``(参数字典, 错误文案)``；成功时错误文案为 ``None``。

        ⚠ 返回的是 ``model_dump(exclude_unset=True)`` ——**不是** ``exclude_none``：
        要保住「模型没说这个参数」与「模型说了 null」的区别。`_run_pg` 的去重键
        就是遍历这个字典建的，多出几个值为 ``None`` 的键会让同一次调用算出两个键。

        ⚠ 报错文案里**不回显异常细节**：Pydantic 的 ValidationError 是一大段英文
        加输入回显，塞给模型只会浪费一轮。这里是「形状不对」的兜底，
        正常路径上不该出现——各工具的 handler 对空值 / 非法值都有自己的中文文案。
        """
        try:
            model = self.args_model.model_validate(raw)
        except ValidationError:
            logger.warning("工具 %s 的参数形状不合法，已按错误处理: %r", self.name, raw)
            return {}, f"错误：{self.name} 的参数格式不对，请检查后重新调用。"
        return model.model_dump(exclude_unset=True), None

    def peek_display(self, args: dict) -> str:
        """仅供前端展示：参数拼不完整时也要给出可读文本。

        默认读主参数 ``query``。⚠ **每个主参数不叫 query 的工具都必须覆写它**——
        原先是一长串 ``if name == TOOL_X``，第三个工具进来时掉进检索分支，
        在演示界面上显示成一对空的「」。改成覆写之后，加工具时的落点从
        「记得去改分派链」变成「照着邻居写一个方法」。
        """
        return str(args.get("query") or "")

    #: 本工具**空手而归**时，`assess_outcome()` 的提示里点名它用的短标签。
    #: 留空则该工具在提示里只出现名字。**别把 description 抄一遍** —— 这句要短。
    route_label: ClassVar[str] = ""

    def empty_reason(self, outcome: ToolOutcome) -> str | None:
        """这次调用**什么都没拿到**时返回一句结构化的证据，否则返回 ``None``。

        默认判据是 ``new_sources`` / ``known_indices`` 双双为空（检索类工具的形状：
        没命中、或命中的全在【资料】里）。图谱与数据库工具覆写它，改读自己那份
        ``total``。

        ⚠ 判据**只能是结构性的**（条数 / 行数），不要引入相似度分数。理由见
        `assess_outcome()`：向量分对「答非所问」没有区分力，照它判会把最需要
        提示的那一类恰好放过去。

        ⚠⚠ ``dropped`` 那一支不是可选的：撞顶时 ``new_sources`` / ``known_indices``
        双双为空，与「真的一条没命中」**形状完全一样**。少了它，模型会在工具文本
        「另有 N 条超出本次来源上限」的下一行读到「没有拿到任何结果…可以如实说没有查到」。
        """
        if outcome.new_sources or outcome.known_indices or outcome.dropped:
            return None
        return "没有拿到任何新条目"

    @abstractmethod
    async def run(self, args: dict, ctx: ToolContext) -> ToolOutcome:
        """执行。**不得抛异常**（错误一律转成文本回传给模型）。

        ``args`` 是 `parse_args()` 归一后的字典。
        """
        raise NotImplementedError


def _peek_display(name: str, arguments: str) -> str:
    """按工具名取展示文案（`agent.py` 与探针按这个签名调用）。

    查不到工具、或参数不是合法 JSON 时回空串——**不抛异常**，它跑在 tool_call 事件的
    推送路径上。
    """
    tool = _REGISTRY.get(name)
    if tool is None:
        return ""
    try:
        args = json.loads(arguments)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(args, dict):
        return ""
    return tool.peek_display(args)


# ---------------------------------------------------------------- 结果质量检查
#: 这几个 `error` 码是「模型把参数写错了」，**不是**「查了但没查到」——
#: 各自的文案已经带修正线索，不需要再补「换个工具」。
#: ⚠ 加新码时想清楚：只有「模型知道怎么改」的才放进来。真正的失败（连接超时、
#:   handler 崩溃）**必须**走提示，那才是它该考虑换工具或如实说明的时候。
_SELF_CORRECTING_ERRORS = frozenset(
    {"unknown_tool", "bad_arguments", "bad_query_type", "empty_query", "duplicate_query"}
)


def assess_outcome(
    name: str, outcome: ToolOutcome, available: tuple[str, ...] = ()
) -> str | None:
    """检查一次工具调用的**结果质量**，不达标时给出显式的下一步；合格则返回 ``None``。

    由 `run_agent_stream()` 在把 `role:"tool"` 消息塞进对话**之前**调用，返回值**追加**
    在 `outcome.text` 之后（不替换、不修改 —— `_render_result` 的字面量被几个探针钉着）。

    ⚠⚠ **判据一律是结构性的**（行数 / 条数），**刻意不用相似度分数**。这不是省事：
    向量分对「答非所问」恰恰**没有**区分力——实测完全不相干的资料得 0.8166，在
    ``MIN_VECTOR_SCORE=0.72`` **之上**（盐化工那次）。唯一能判「这份资料能不能回答」
    的信号是 CrossEncoder 的重排分（能答的 ≈0.73 / 答不了的 ≈0.50），但它**只在重排
    开启时才存在**，而重排是用户可随时关掉的开关 —— 拿它当闸门等于「关了重排就没有
    质量检查」。所以这里只用「拿到了几条」这件**绝对可靠、不依赖任何标定**的事实。

    ⚠⚠ **本函数只做「告知 + 把选项摆到眼前」，不替模型做决定。** 系统无从判断
    「数据库答不了但图谱能答」——那要理解问题语义，是模型的工作。所以它做的是
    **点名本轮还挂着的其他工具**（让路由选项变得显眼），再**允许**模型如实说
    「没查到」。这与 CLAUDE.md 记的那条教训是同一条纪律：`/api/chat` 上「答不出就
    自动联网」那套整块删除，根因正是**服务端替模型做语义判断**必然静默出错。
    把这份判断留在这里，模型每一轮都能看到工具表，它才是唯一有依据做这个决定的人。
    """
    if outcome.error in _SELF_CORRECTING_ERRORS:
        # 调错了（名字 / 形状 / 枚举值 / 必填空着）——**不是**查空了。这几个分支的中文
        # 文案本身就带修正线索（「可用工具：…」「可用类型：…」），再叠一段「换个工具」
        # 只会把「把参数改对」这条更重要的指令淹掉。
        return None

    tool = _REGISTRY.get(name)
    if tool is None:
        return None
    reason = tool.empty_reason(outcome)
    if reason is None:
        return None

    verdict = "本次调用没有拿到任何结果" if outcome.ok else "本次调用失败，没有拿到任何结果"
    lines = [f"【结果质量】{verdict}（{reason}）。"]

    others = [n for n in available if n != name and n in _REGISTRY]
    if others:
        # ⚠ **至少还有一个别的工具**时才提「换工具」。只剩检索工具时提它，等于让模型
        #   去调一个本轮根本没挂出去的工具（同 rag.py 里 `_AGENT_DB_CROSS_TOOL_RULE`
        #   「只在图谱与数据库都启用时才拼进去」是同一条纪律）。
        detail = "；".join(
            f"{n}（{_REGISTRY[n].route_label}）" if _REGISTRY[n].route_label else n
            for n in others
        )
        lines.append(f"本轮还挂着这些工具，这个问题若更适合它们请改调：{detail}。")

    lines.append(
        "也可以换个更精确的参数或表述再调一次同一个工具（别用一模一样的参数）；"
        "若都不适用，就如实告诉用户「没有查到」，不要用常识或推测补足。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 执行
async def execute_tool(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
    """执行一次工具调用。

    **本函数不抛异常**：任何失败都返回可回传给模型的说明文本。
    注意 ``asyncio.CancelledError`` 继承自 BaseException，不会被下面的
    ``except Exception`` 吞掉，取消仍能正常穿透。
    """
    tool = _REGISTRY.get(call.name)
    if tool is None:
        # ⚠ 可用列表取自**本轮实际挂出去的**那批（ctx.tool_names），不是进程级的
        # default_schemas()：开着联网时后者不含 search_web，会把一个逐字正确的工具名
        # 报成不存在的，白送模型一次「换个工具试试」的乱猜。
        # （`ctx.tool_names` 由 `run_agent_stream` 从 `tools_for()` 注入；建得早的
        #   ToolContext 可能是空元组，那时才退回进程级那批。）
        names = ctx.tool_names or tuple(s["function"]["name"] for s in default_schemas()) or registered_names()
        return ToolOutcome(
            f"错误：没有名为 {call.name} 的工具。可用工具：{'、'.join(names)}。",
            ok=False,
            error="unknown_tool",
        )

    try:
        raw = json.loads(call.arguments or "{}")
        if not isinstance(raw, dict):
            raise ValueError("arguments 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as exc:
        return ToolOutcome(
            f"错误：参数不是合法 JSON（{exc}）。请重新调用 {call.name}。",
            ok=False,
            error="bad_arguments",
        )

    args, shape_error = tool.parse_args(raw)
    if shape_error is not None:
        return ToolOutcome(shape_error, ok=False, error="bad_arguments")

    try:
        return await tool.run(args, ctx)
    except Exception as exc:  # noqa: BLE001 - 最后一道闸：handler 内部已各自 try 过
        logger.exception("工具 %s 执行异常", call.name)
        return ToolOutcome(f"工具执行失败（{exc}）。请基于已有信息作答。", ok=False, error=str(exc))


# ---------- 把「走错通道」的工具调用捞回来 ----------
#
# DeepSeek v4 偶发把工具调用发成正文里的标记（拦它的是 clients/base_client.py 的
# _MarkupSuppressor）。那种情况下 delta.tool_calls 是空的，这一轮会被 run_agent_stream()
# 当成「最终回答」，于是模型说的「我再补充搜索一下」就成了一句空话——用户看到的就是这个
# bug：声明了要查，却什么都没查，也没有补充内容。
#
# 捞回来的原则：**名字精确对上已注册工具时优先，对不上就只认结构**（2026-09 改）。
#
# 前半句是后加的：模型把工具名写对时，只按参数集合归属会把它判到别的工具上 —— 实测
# `name="search_web"` + 参数 `query` 被判成了检索工具（`query` 是检索的主参数，1:0 独得
# 选票），一次联网调用静默变成了检索。所以名字精确命中时先试它。
# 后半句是原样保留的：模型**写错**工具名（实测把两个真名揉成一个不存在的名字）时
# 名字毫无信息量，仍靠参数集合判定。
#
# 两条路径的收尾一致：参数名、枚举值一律拿去和真实 schema 对，对不上就整条丢弃，
# **绝不猜**。执行仍走 execute_tool()，白名单与只读防线一道不少——这里放宽的只是
# 「从哪个通道读调用」，不是「允许调什么」。

# ``[^>]*>?`` 用来吃掉标签名后面的其余属性（实测带 ``string="true"``）。只写 ``\s*>?``
# 的话标签收不干净，属性会粘进参数值里。
_INVOKE_HEAD_RE = re.compile(r"invoke\s+name=[\"'](?P<name>[^\"']+)[\"'][^>]*>?")
_PARAM_HEAD_RE = re.compile(r"parameter\s+name=[\"'](?P<key>[^\"']+)[\"'][^>]*>?")

# 模型给参数起的别名。只收有实测依据或语义无歧义的——**别名表越短，误判越少**，
# 所以别顺手往里加「看着像」的同义词（keyword 是实测泄漏里那个，另两个是它的变体）。
_PARAM_ALIASES = {
    "keyword": "entity",
    "keywords": "entity",
    "entity_name": "entity",
}


def _parse_invokes(clean: str) -> list[tuple[str, dict[str, str]]]:
    """从剥掉标记的文本里切出 (工具名, 参数) 列表。

    不用一条大正则去匹配嵌套：按 ``invoke name=`` 的出现位置切块，块内再按
    ``parameter name=`` 切，值取到下一个 ``</`` 为止。层层切片比嵌套正则好读也好调。
    """
    heads = list(_INVOKE_HEAD_RE.finditer(clean))
    out: list[tuple[str, dict[str, str]]] = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(clean)
        body = clean[head.end() : end]
        params: dict[str, str] = {}
        for pm in _PARAM_HEAD_RE.finditer(body):
            raw = body[pm.end() :]
            cut = raw.find("</")
            params[pm.group("key")] = (raw[:cut] if cut >= 0 else raw).strip()
        out.append((head.group("name"), params))
    return out


def _resolve_enum(value: str, allowed: list[str]) -> str | None:
    """把枚举值对到真实取值上。

    实测模型会把词序写反（把 entity_search 写成 search_entity），所以先精确匹配，
    再按「下划线切词后排序」比对。**只有唯一命中才接受**，分不清就宁可放弃。
    """
    if value in allowed:
        return value

    def norm(s: str) -> tuple[str, ...]:
        return tuple(sorted(s.lower().split("_")))

    hits = [a for a in allowed if norm(a) == norm(value)]
    return hits[0] if len(hits) == 1 else None


def _bind_params(
    raw: dict[str, str], name: str, named: dict | None
) -> tuple[dict[str, str], frozenset[str]]:
    """按别名表改写参数名，返回 (改写后的参数, **被改写过的键**)。

    ``named`` 非空（= 工具名精确命中本轮工具表）时，额外叠上**该工具**声明的名字限定
    别名（`BaseTool.name_scoped_aliases`）。

    ⚠ 别名表从**工具定义**里取，不在这里写裸工具名字面量：那样日后改工具名时，
    这张表会**静默失效**（别名落空 → 一次联网泄漏被投票判给检索工具），
    而「改名顺手改这里」是没有提示的。挂在工具类上就跟着名字一起走。

    ⚠ 但 ``named is not None`` 这道**闸**必须保留：它保证「本轮没挂的工具不享受
    名字限定别名」。`probe_web_search.py` ⑧ 钉着「不传本轮工具表时 search_web 的泄漏
    必须恢复不出来」。
    """
    scoped: dict[str, str] = {}
    if named is not None:
        tool = _REGISTRY.get(name)
        if tool is not None:
            scoped = tool.name_scoped_aliases
    table = {**_PARAM_ALIASES, **scoped}
    return (
        {table.get(k, k): v for k, v in raw.items()},
        # ⚠ 收集的是**改写之后**的名字（entity），不是原始的名字（keyword）——
        # _normalize_args 拿它去比对的也是改写后 params 的键。写成原始名字的话
        # 两边永远对不上，加固会静默失效（实测踩过，probe_recover 的对应用例抓到的）。
        frozenset(table[k] for k in raw if k in table),
    )


def _match_schema(params: dict[str, str], *, schemas: list[dict] | None = None) -> dict | None:
    """按参数集合判定模型想调哪个工具。

    不看模型写的工具名——它会把名字拼错。参数名才是可靠信号：``query_type``
    这种参数只有一个工具带。

    **一个都对不上、或前两名打平就返回 None**。打平说明参数被揉混了（正常调用
    不会同时出现两个工具的主参数），这时按顺序取第一个是抛硬币——宁可放弃，
    让调用方走「没有工具调用」的收尾，也不要执行一个模型没打算发起的调用。

    ⚠ ``schemas`` **默认是进程级那批（`registered_schemas()`），不含 per-request 的
    `search_web`**。调用方必须把本轮实际的工具表传进来（`tools_for()` 的结果），否则模型
    把一次**联网**调用漏进正文时，这里根本看不见那个 schema → `web_query` 得 0 票 →
    整条丢弃 → 表现正是那条线上 bug 的形状：「模型说了要联网，结果什么都没查」。
    """
    pool = default_schemas() if schemas is None else schemas
    scores = []
    for schema in pool:
        props = schema["function"]["parameters"].get("properties", {})
        scores.append((sum(1 for key in params if key in props), schema))
    scores.sort(key=lambda pair: pair[0], reverse=True)
    if not scores or scores[0][0] == 0:
        return None
    if len(scores) > 1 and scores[0][0] == scores[1][0]:
        return None
    return scores[0][1]


def _normalize_args(
    schema: dict, params: dict[str, str], *, aliased: frozenset[str] = frozenset()
) -> dict | None:
    """把参数对到真实 schema 上。对不上（枚举无解、必填缺失）就返回 None。

    ``aliased`` 是**被 _PARAM_ALIASES 改写过**的键。这些键对不上所选 schema 的属性时
    **整条调用丢弃**，而不是像普通未知参数那样静默跳过——见下面的注释。

    ⚠⚠ **本函数直接读裸 JSON Schema 键**（``prop["enum"]`` / ``prop.get("type")`` /
    ``spec["required"]``），所以 `to_openai_schema()` 那层清洗是它成立的前提。
    给字段加 ``Optional`` 之前先确认清洗没把顶层 ``type`` 弄丢，否则整数会静默变字符串。
    """
    spec = schema["function"]["parameters"]
    props = spec.get("properties", {})
    args: dict = {}
    for key, value in params.items():
        prop = props.get(key)
        if prop is None:
            # 别名改写过的键落不到所选 schema 上，说明**归属判错了**：模型写的
            # `keyword` 本意是某个工具的筛选词，可我们选的这个工具根本没有这个概念。
            # 旧行为是静默 `continue`，表现是「模型以为按关键词筛了，实际筛没了」——
            # 正是「对不上就整条丢弃，绝不猜」要消灭的那类静默错答案。
            # 未被改写的未知参数仍然静默跳过（模型偶尔多带一两个没用的字段，不该因此作废）。
            if key in aliased:
                return None
            continue
        if not value:
            continue
        allowed = prop.get("enum")
        if allowed:
            resolved = _resolve_enum(value, allowed)
            if resolved is None:
                return None  # 枚举对不上，整条丢弃——不猜
            args[key] = resolved
        elif prop.get("type") == "integer":
            try:
                args[key] = int(value)
            except ValueError:
                continue
        else:
            args[key] = value
    if any(req not in args for req in spec.get("required", [])):
        return None
    return args


def _schema_by_name(name: str, pool: list[dict]) -> dict | None:
    """泄漏标记里写的工具名**精确等于本轮某个已注册工具名**时，返回那个 schema。

    ⚠ 两条「必须」都是被既有断言钉死的，改之前先看清楚：

    1. **只能在传进来的 `pool`（= 本轮工具表）里查名**，不能用注册表。联网是按请求挂的，
       拿进程级的表去查名等于把**没注册**的工具也认下来 —— `probe_web_search.py` ⑧
       正钉着「不传工具表时 search_web 的泄漏必须恢复不出来」。
    2. **必须精确相等**，不能学 `_resolve_enum()` 那样做「下划线切词后排序」的归一。
       否则模型把两个真名揉出来的假名会被拉向其中一个真名，
       `probe_recover.py` 的第一个用例整条判错。
    """
    for schema in pool:
        if schema["function"]["name"] == name:
            return schema
    return None


def recover_tool_calls(
    markup: str, *, prefix: str, schemas: list[dict] | None = None
) -> list[ToolCall]:
    """把模型漏进正文的工具调用标记还原成可执行的 ToolCall。

    任何一步对不上就丢掉那一条并记日志——**恢复失败必须退化成「没有工具调用」**，
    由调用方走原有的收尾逻辑，不能因为这里抛异常而中断整个回答。

    ``schemas`` 默认 `None` = 用进程级那批。⚠ **默认值不能省**：`probe_recover.py`
    有一批只传 `prefix` 的单 kwarg 调用，它们验的是进程级那批工具的归属判定。
    传本轮工具表的调用方是 `run_agent_stream`（联网是按请求挂的）。

    **归属判定：名字精确优先，否则只认结构**（2026-09 改，原先一律只认结构）。
    模型把工具名写对时优先信名字，因为「只认参数集合」会把一次**联网**调用判成检索：
    实测泄漏原文是 ``name="search_web"`` + 参数 ``query``，而 `query` 恰是检索工具的
    主参数 → 它 1:0 独得选票 → **联网被当成检索执行了**。
    名字对不上（模型把两个真名揉成一个不存在的名字）时行为与改动前**逐字相同**，仍走投票。
    名字对上但**一个参数都不沾它**（多半是幻觉的名字）时同样退回投票，理由见下面的守卫。
    """
    if not markup:
        return []
    pool = default_schemas() if schemas is None else schemas
    calls: list[ToolCall] = []
    for index, (name, raw_params) in enumerate(_parse_invokes(strip_tool_markup(markup))):
        # 名字优先：模型这回把工具名写对了，就先信它（写错的情形见 _schema_by_name）
        named = _schema_by_name(name, pool)
        params, aliased = _bind_params(raw_params, name, named)

        # ⚠ 光名字对上还不够：**至少得有一个参数真的落在它身上**，否则退回投票。
        #   漏掉这一条会开一个静默错答案的口子：采购记录工具没有必填参数，
        #   模型若把 `query` 这种参数写在它名下，_normalize_args 会「成功」返回一个**空
        #   args**，然后执行一次没有任何筛选条件的查询（pg_store 那条「空筛选必须挡住」
        #   是最后一道防线，不该靠它兜）。而同样输入按参数投票本可以恢复出一次正常检索。
        #   名字对、参数一个都不沾，说明名字多半是模型的幻觉，别信它。
        if named is not None:
            props = named["function"]["parameters"].get("properties", {})
            if not any(k in props for k in params):
                named = None
                params, aliased = _bind_params(raw_params, name, None)

        # 归一化的候选顺序：① 名字命中的那个 → ② 按参数投票选出的那个。
        # ①失败（必填缺失 / 枚举对不上 / 别名落空）才轮到②，**②的原逻辑一字未改**。
        voted = _match_schema(params, schemas=pool)
        candidates = [s for s in (named, voted) if s is not None]
        schema: dict | None = None
        args: dict | None = None
        for cand in candidates:
            got = _normalize_args(cand, params, aliased=aliased)
            if got is not None:
                schema, args = cand, got
                break
        if schema is None or args is None:
            if not candidates:
                logger.warning(
                    "漏进正文的工具调用判不出归属，已忽略: name=%r params=%s", name, list(params)
                )
            else:
                logger.warning(
                    "漏进正文的工具调用参数对不上 schema，已忽略: name=%r params=%s", name, params
                )
            continue
        real = schema["function"]["name"]
        if named is not None and voted is not None and voted is not named:
            # 参数集合同时指向另一个工具，但名字是精确的 —— 按名字执行。
            # （旧行为在这里是「打平即丢弃」，改成按名字是 2026-09 的决定。）
            logger.warning(
                "漏进正文的调用 name=%r 与参数投票结果 %r 不一致，按名字执行",
                name,
                voted["function"]["name"],
            )
        if real != name:
            logger.warning("模型把工具名写成了 %r，按参数判定为 %r", name, real)
        calls.append(
            ToolCall(
                id=f"call_{prefix}_recovered_{index}",
                name=real,
                arguments=json.dumps(args, ensure_ascii=False),
            )
        )
    return calls
