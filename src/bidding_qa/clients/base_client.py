"""文本大模型客户端的基类与 OpenAI 兼容共享实现。

**为什么两家共用一个实现**：DeepSeek 与智谱 GLM 都提供 OpenAI 兼容的
`/chat/completions`（都支持流式、`tool_calls`、`reasoning_content`），
现有 DeepSeek 客户端走的本来就是 `openai` SDK 而不是手写 HTTP。
所以「支持多家」这件事在这里**不是两套 HTTP 实现，而是同一份流式拼装 + 两份配置**
—— 硬写两套只会把 `_absorb()` / 抑制器 / 收尾逻辑重复一遍，日后改一处漏一处。

⚠ **真正因供应商而异的是三件事**，都收敛成了可覆盖的 property：
`model` / `api_key` / `base_url`（配置），以及 `thinking_disabled_body`（关思考的写法）。
后者的映射**必须实测**，见 `zai_client.py` 里的说明。
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 模型 chat template 的工具调用特殊 token，包在竖线里。竖线可能是全角（U+FF5C，
# 实测泄漏的就是这种）也可能是半角，两个都认。
#
# 开头的 ``</?`` 把包裹标记的尖括号一并吃掉：只匹配 DSML 本身的话，紧挨着它的
# ``</`` 会作为「标记之前的正文」被放行出去，前端就会显示一个孤零零的 ``</``。
# 要求带尖括号还有个好处——正文里若真出现「DSML」这个词（本项目语料里没有），
# 也不会被误伤。
#
# ⚠ 这两个正则**只认 DSML 这一种标记**（DeepSeek 的 chat template）。换一家供应商后
#   若它泄漏的标记长相不同，抑制器**拦不住**——那时要在这里补对应模式，而不是
#   把抑制器摘掉（摘掉就是正文污染 + localStorage 永久留存，见 _MarkupSuppressor）。
_TOOL_MARKUP_RE = re.compile(r"</?[|｜]{1,2}\s*DSML\s*[|｜]{1,2}[^>]{0,200}>?")


# 只剥标记 token 本身（竖线裹着 DSML 那一段），**不动包裹它的尖括号和标签名**。
# 与 _TOOL_MARKUP_RE 的分工：那个是给抑制器做**检测**用的，必须连标签一起吃掉，
# 否则会漏出半个收尾尖括号；这个是给**解析**用的，要保住标签结构，否则按标签名
# 切块会全部落空。
_TOOL_MARKUP_TOKEN_RE = re.compile(r"[|｜]{1,2}\s*DSML\s*[|｜]{1,2}")


def strip_tool_markup(text: str) -> str:
    """剥掉标记 token，留下干净的结构（``<invoke name="...">`` 这种）。

    还原工具调用前必须先剥：闭合标签里也夹着标记 token，不剥的话按标签名去匹配
    闭合位置会全部落空。
    """
    return _TOOL_MARKUP_TOKEN_RE.sub("", text)


class _MarkupSuppressor:
    """抑制模型把工具调用标记当正文吐出来。

    DeepSeek v4 偶发把本该走 ``delta.tool_calls`` 的特殊 token 写进 ``delta.content``
    （实测见 CLAUDE.md「模型把工具标记写进正文」）。一旦透传出去，这段文字会被渲染进
    回答气泡、存进浏览器 localStorage、并作为历史回传下一轮——污染是永久的。

    **不能逐片做子串匹配**：流式分片会把标记拦腰截断（``｜`` / ``｜DSM`` / ``L｜`` …），
    逐片检查必然漏。所以留一段尾巴，确认它不是标记前缀之后再放行。

    命中之后本轮后续 content 全部丢弃——模型一旦进了「标记模式」，后面吐的都是同一段
    结构化内容。真正该执行的 ``tool_calls`` 走的是另一个字段，不受影响。

    ⚠ 这条链路的**两条分支共用同一个类**：`OpenAICompatLLM.stream()`（`/api/chat`）与
    `stream_with_tools()`（Agent）各 new 一个。别再按「只有带工具的链路才可能被污染」
    来判断——实测 `/api/chat` 一个工具都没注册，模型照样把一次 `search_web` 调用当正文吐出来。
    """

    # 必须覆盖标记的最长前缀（``｜｜DSML｜`` 这类）。留 32 足够，代价只是正文尾部
    # 滞后 32 个字符，收流时由 flush() 一次性放出。
    _TAIL = 32
    # 捕获上限：模型真进了「标记模式」时可能一口气吐很多，别让它撑爆内存
    _CAPTURE_MAX = 8000

    def __init__(self) -> None:
        self._buf = ""
        self.tripped = False
        # 被拦下来的原文。**不是丢掉就完事**——那段标记里往往含着一个模型真心想发起的
        # 工具调用（只是走错了通道），agent.py 会拿它去恢复，见 recover_tool_calls()。
        self.captured = ""

    def _capture(self, text: str) -> None:
        if len(self.captured) < self._CAPTURE_MAX:
            self.captured += text[: self._CAPTURE_MAX - len(self.captured)]

    def feed(self, text: str) -> str:
        """吃进一个分片，返回可以安全发给前端的部分（可能为空串）。"""
        if self.tripped:
            self._capture(text)
            return ""
        self._buf += text
        match = _TOOL_MARKUP_RE.search(self._buf)
        if match:
            self.tripped = True
            head = self._buf[: match.start()].rstrip()
            self._capture(self._buf[match.start() :])
            self._buf = ""
            return head
        if len(self._buf) <= self._TAIL:
            return ""
        out, self._buf = self._buf[: -self._TAIL], self._buf[-self._TAIL :]
        return out

    def flush(self) -> str:
        """收流。没命中过就把扣着的尾巴放出来，命中过则什么都不放。"""
        if self.tripped:
            return ""
        out, self._buf = self._buf, ""
        return out


class LLMNotConfiguredError(RuntimeError):
    pass


@dataclass
class ToolCall:
    """一次工具调用请求，由流式分片拼装而成。"""

    id: str
    name: str
    arguments: str
    index: int = 0


@dataclass
class LLMEvent:
    """stream_with_tools() 产出的事件。

    - ``type="reasoning"`` / ``"content"``：增量片段，取 ``text``
    - ``type="message"``：终态，携带整轮累计结果。async generator 不能 ``return``
      值，只能靠这样一个终止事件把 content / tool_calls 带出来。
    """

    type: str
    text: str = ""
    content: str = ""
    reasoning: str = ""
    # 思维链这次流里是否「出现过」（不论那个字段叫 `reasoning_content` 还是
    # `reasoning`，见 `_delta_reasoning`）。判据是**出现过而非非空**——模型在工具
    # 轮次里常返回空串。回传策略见 agent.py 的 `_assistant_tool_message()`。
    reasoning_seen: bool = False
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    # 模型写进正文、被 _MarkupSuppressor 拦下来的工具调用标记原文（没拦到就是空串）。
    # agent.py 用它尝试把这次「走错通道」的调用捞回来。
    leaked_markup: str = ""


def _absorb(
    acc: dict[int, ToolCall], order: list[int], fragments: object, last_index: int
) -> int:
    """把一帧 ``delta.tool_calls`` 的碎片并入累计结果，返回最新的 index。

    分片规则（实测）：首帧带 ``id`` + ``function.name``，其后每帧只追加
    ``function.arguments`` 的一小段。因此：

    - 按 ``index`` 归并，**不能按出现顺序 append**（多个调用会交错出现）
    - ``id`` / ``name`` 首次非空即定，后续分片带的空串不能覆盖
    - ``index`` 缺失时沿用上一个（SDK 用 construct_type() 宽松构建，缺字段静默变 None）
    """
    for frag in fragments:  # type: ignore[union-attr]
        index = getattr(frag, "index", None)
        frag_id = getattr(frag, "id", None)
        fn = getattr(frag, "function", None)
        fn_name = getattr(fn, "name", None) if fn is not None else None
        fn_args = getattr(fn, "arguments", None) if fn is not None else None

        # 什么都没带的空帧（流末尾偶尔出现），跳过
        if index is None and not frag_id and not fn_name and not fn_args:
            continue
        if index is None:
            if last_index < 0:
                continue  # 首帧就没有 index，无从定位，直接丢弃
            index = last_index
        last_index = index

        call = acc.get(index)
        if call is None:
            call = ToolCall(id="", name="", arguments="", index=index)
            acc[index] = call
            order.append(index)

        if frag_id and not call.id:
            call.id = frag_id
        if fn_name and not call.name:
            call.name = fn_name
        if fn_args:
            call.arguments += fn_args

    return last_index


def _finalize_tool_calls(
    acc: dict[int, ToolCall], order: list[int], prefix: str
) -> list[ToolCall]:
    """整理成最终的工具调用列表。"""
    calls: list[ToolCall] = []
    seen: set[str] = set()
    for index in order:
        call = acc[index]
        if not call.name:
            # 只有 id 没有函数名的幽灵调用，回传回去必然 400
            logger.warning("丢弃缺少函数名的工具调用分片: index=%s", index)
            continue
        if not call.id or call.id in seen:
            # id 缺失或与上一轮重名：assistant 消息里的 tool_calls[i].id 与
            # role="tool" 消息的 tool_call_id 必须是同一个字符串，且跨轮唯一
            call.id = f"call_{prefix}_{index}" if prefix else f"call_{index}"
        seen.add(call.id)
        calls.append(call)
    return calls


def _delta_reasoning(delta: object) -> str | None:
    """从流式 delta 里取思维链片段。**字段名各家不同，两个都读**。

    ⚠⚠ 2026-09-27 真机实测：DeepSeek 用 ``reasoning_content``，而 **vLLM 配
    ``--reasoning-parser qwen3`` 时用 ``reasoning``**（流式与非流式都是后者，
    实测 SSE 里就是 ``{"delta":{"reasoning":"用户"}}``）。

    两个名字**都不是 openai SDK 的声明字段**——``ChoiceDelta.model_fields`` 只有
    content / role / tool_calls / function_call / refusal，能取到纯粹是因为
    ``model_config = {'extra': 'allow'}``。所以**只认一个名字的话，另一家不会报错、
    不会崩溃，只是整条思维链静默消失**：``reasoning_seen`` 恒为 False、前端
    「深度思考」那一格空的，很容易误读成「这家模型不思考」。

    **读到第一个非 None 就返回，不拼接**：万一哪家两个字段都发（内容还不同），
    拼起来会把同一段思考算两遍。

    ``None`` = 这次流里没出现过；``""`` = 字段在但是空的。调用方靠这个区分
    （``stream_with_tools`` 拿它判 ``reasoning_seen``），**别把两者合并**。
    """
    for name in ("reasoning_content", "reasoning"):
        value = getattr(delta, name, None)
        if value is not None:
            return value
    return None


class BaseLLMClient(ABC):
    """文本大模型客户端的契约。

    ⚠ **只做「文档 + 契约声明」，不做运行期关卡。** 本仓库的探针大量使用
    鸭子类型的假 LLM（`eval_rag.py` / `probe_recover_e2e.py` / `probe_web_search.py`），
    它们都不继承这个类；在基类上加 `isinstance` 检查会把那些替身挡在门外，
    探针会集体报假失败。所以本类**唯一的消费方是类型标注**。
    """

    @property
    @abstractmethod
    def model(self) -> str:
        """模型名（`/api/health` 与 `/api/config` 都会回显它）。"""

    @property
    @abstractmethod
    def ready(self) -> bool:
        """配了 Key。**纯读配置，不发网络请求**。"""

    #: 这个后端**能不能接工具清单**。
    #:
    #: `False` 时 `agent.run_agent_stream` 本轮**一个工具都不挂**，`rag` 那三段提示词
    #: 也不再提工具（见 `rag.build_agent_messages` 的 `tools_ok`）——「提示词提到工具
    #: ⟺ 工具列表里有它」这条不变量由构造成立，不靠两处记得同步。
    #:
    #: ⚠ 唯一的 `False` 是 Ollama（官方 `deepseek-r1:8b` 的模板里没有 tools 段，
    #: 带 tools 请求会被服务端直接拒掉，**不是答得差、是一个字都出不来**）。
    #:
    #: ⚠ 消费方一律写 `getattr(llm, "supports_tools", True)` 取 —— 探针里的假 LLM 是
    #: 鸭子类型、不继承本类（见类 docstring），硬取属性会把那些替身全挡在门外。
    supports_tools: bool = True

    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """流式生成，产出 `(kind, text)`。kind ∈ reasoning / content / markup_leak。"""

    @abstractmethod
    def stream_with_tools(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        disable_thinking: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        call_id_prefix: str = "",
    ) -> AsyncIterator[LLMEvent]:
        """带工具调用的流式生成，结尾产出一个 `type="message"` 的终态事件。"""

    @abstractmethod
    async def complete(self, messages: list[dict], *, temperature: float | None = None) -> str:
        """非流式生成，直接返回正文。"""


class OpenAICompatLLM(BaseLLMClient, ABC):
    """OpenAI 兼容协议客户端的共享实现。

    子类**只需把自家的配置以 property 的形式喂进来**（`model` / `api_key` /
    `base_url` / `default_temperature` / `default_max_tokens`，需要时再覆盖
    `thinking_disabled_body`），流式拼装、工具调用吸收、标记抑制全部在这里。

    ⚠ 配置一律走 **property**（每次调用时读 `settings`）而不是构造参数——
    原实现就是这么写的，且 `probe_vision.py` 会在运行期把 `settings.zhipu_api_key`
    置空来验「未配置」分支；构造函数里captured 一份会让那类测试**静默失效**。
    """

    #: 建连超时。原实现在 `client` 里写死 60.0，这里提成类属性
    timeout: float = 60.0

    #: 没配 Key 时的报错文案，子类覆盖
    _NOT_CONFIGURED = "未配置 API Key，请在 .env 中填写后重试。"

    def __init__(self) -> None:
        # 懒建：构造时不碰网络、不需要 Key。
        # ⚠ `probe_markup.py` 直接 `DeepSeekLLM()` 造替身再注入 `_client`，
        #   所以**零参构造必须继续可用**、且 `_client` 这个名字不能改。
        self._client = None

    # —— 子类要填的配置 ——

    @property
    @abstractmethod
    def api_key(self) -> str: ...

    @property
    @abstractmethod
    def base_url(self) -> str: ...

    @property
    @abstractmethod
    def default_temperature(self) -> float: ...

    @property
    @abstractmethod
    def default_max_tokens(self) -> int: ...

    @property
    def thinking_disabled_body(self) -> dict:
        """关掉思考模式要带的 `extra_body`。

        DeepSeek 用 `{"thinking": {"type": "disabled"}}`；别家若不认这个写法，
        子类覆盖本 property 即可，不必动共享逻辑。
        """
        return {"thinking": {"type": "disabled"}}

    @property
    def ready(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def client(self):
        if self._client is None:
            if not self.ready:
                raise LLMNotConfiguredError(self._NOT_CONFIGURED)
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self.api_key.strip(),
                base_url=self.base_url.strip().rstrip("/"),
                timeout=self.timeout,
                max_retries=1,
            )
        return self._client

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """流式生成，产出 (kind, text)。

        推理型模型（DeepSeek v4 系列、GLM 的 thinking 档）会先输出一段思维链
        （reasoning_content），再输出正式回答（content）。两者分开透传，前端可将
        思维链渲染成「深度思考」折叠区，避免用户面对长时间空白等待。

        ⚠ **这条链路同样会被工具调用标记污染**，别再按「只有带工具的那条才可能」来判断。
        模型是在**幻想**一个工具，而不是在复述我们给它的工具表——`/api/chat` 一个工具都
        没注册，照样会把一次 `search_web` 调用当成正文吐出来（实测见 CLAUDE.md「模型把工具
        标记写进正文」）。这条链路没有工具循环可恢复，标记只能丢掉。

        第三种产出：``("markup_leak", 标记原文)``，仅在拦下标记时出现，**只给调用方用、
        不能转发给前端**（api.py 拿它判断这是不是一次联网请求）。
        """
        # 与 stream_with_tools() 共用同一个抑制器，别另写一份
        suppressor = _MarkupSuppressor()
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.default_temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.default_max_tokens,
            stream=True,
        )
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = _delta_reasoning(delta)
            if reasoning:
                yield ("reasoning", reasoning)
            content = getattr(delta, "content", None)
            if content:
                safe = suppressor.feed(content)
                if safe:
                    yield ("content", safe)

        # 收流：把扣着的尾巴放出来（没命中标记时它是正常正文的一部分）
        tail = suppressor.flush()
        if tail:
            yield ("content", tail)
        if suppressor.tripped:
            logger.warning(
                "模型把工具调用标记写进了正文（/api/chat 链路），已抑制本轮后续正文"
            )
            yield ("markup_leak", suppressor.captured)

    async def stream_with_tools(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        # "none" / "auto" 这类字符串，或 {"type":"function","function":{"name":...}}
        # 这样的具名字典（agent.py 用它强制首轮调用图谱工具）。原样透传给服务端。
        tool_choice: str | dict | None = None,
        # 关掉思考模式。**具名 tool_choice 必须配它**，见下面 params 里的注释。
        disable_thinking: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        call_id_prefix: str = "",
    ) -> AsyncIterator[LLMEvent]:
        """带工具调用的流式生成。

        与 ``stream()`` 的区别：除了正文与思维链，还会拼装 ``delta.tool_calls``
        的流式分片，并在结尾产出一个 ``type="message"`` 的终态事件携带整轮结果。

        **终止信号是「async for 自然结束」，不是 finish_reason** —— SDK 会吞掉
        流末尾的 ``[DONE]`` 标记，只有迭代结束才是真的结束。同理，迭代中途抛异常时
        已拼装的 arguments 是残缺的，调用方不应据此执行任何工具。
        """
        params: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.default_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
            "stream": True,
        }
        if tools:
            params["tools"] = tools
        if tool_choice:
            params["tool_choice"] = tool_choice
        if disable_thinking:
            # ⚠ 具名 tool_choice 与思考模式**互斥**——实测带 reasoning 的请求传具名
            #   tool_choice 会被服务端直接拒（400 "Thinking mode does not support this
            #   tool_choice"，"required" 同样不行，只有 "none"/"auto" 能过）。
            #   所以要强制调用某个工具，就只能在这一轮关掉思考。
            #   代价：这一轮不再有 reasoning_content，前端那一格是空的；**只影响被强制的
            #   那一轮**，后续轮次照常带思考。实测这个参数能用，且关掉后具名 tool_choice 生效。
            params["extra_body"] = self.thinking_disabled_body

        acc: dict[int, ToolCall] = {}
        order: list[int] = []
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_seen = False
        finish_reason: str | None = None
        last_index = -1
        # 抑制器；stream() 那条链路**也有**一个，见那边的注释（原先这里写着「那边不设防，
        # 也没必要」——实测被推翻：模型会幻想出压根没注册过的工具，与工具表有没有关系无关）
        suppressor = _MarkupSuppressor()

        stream = await self.client.chat.completions.create(**params)
        # async with 保证客户端中途取消（前端点「停止」）时及时关闭连接
        async with stream:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                raw_reasoning = _delta_reasoning(delta)
                if raw_reasoning is not None:
                    reasoning_seen = True
                    if raw_reasoning:
                        reasoning_parts.append(raw_reasoning)
                        yield LLMEvent(type="reasoning", text=raw_reasoning)

                text = getattr(delta, "content", None)
                if text:
                    safe = suppressor.feed(text)
                    if safe:
                        content_parts.append(safe)
                        yield LLMEvent(type="content", text=safe)

                fragments = getattr(delta, "tool_calls", None)
                if fragments:
                    last_index = _absorb(acc, order, fragments, last_index)

        # 收流：把 suppressor 扣着的尾巴放出来（没命中标记时它是正常正文的一部分）
        tail = suppressor.flush()
        if tail:
            content_parts.append(tail)
            yield LLMEvent(type="content", text=tail)
        if suppressor.tripped:
            logger.warning(
                "模型把工具调用标记写进了正文，已抑制本轮后续正文"
                "（这一轮的真实 tool_calls 不受影响，共 %d 个）",
                len(acc),
            )

        yield LLMEvent(
            type="message",
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            reasoning_seen=reasoning_seen,
            tool_calls=_finalize_tool_calls(acc, order, call_id_prefix),
            finish_reason=finish_reason,
            leaked_markup=suppressor.captured,
        )

    async def complete(self, messages: list[dict], *, temperature: float | None = None) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.default_temperature if temperature is None else temperature,
            max_tokens=self.default_max_tokens,
            stream=False,
        )
        return response.choices[0].message.content or ""
