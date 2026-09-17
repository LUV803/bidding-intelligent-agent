"""验证 _MarkupSuppressor：模型漏进正文的工具调用标记必须被拦住。

标记会被流式分片拦腰截断，逐片做子串匹配必然漏——用例专门覆盖「逐字符喂」
「定长切片」两种切断方式，以及「干净文本必须原样通过」「断在前缀不能吞正文」。

跑法：

    uv run python scripts/probe_markup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa.llm import _MarkupSuppressor  # noqa: E402

BAR = chr(0xFF5C)  # 全角竖线
FULL = BAR * 2 + "DSML" + BAR * 2  # 实测泄漏的那种
HALF = "||DSML||"

head = "再为您补充搜索政府机关、学校等其他类型的采购人。"
# 复刻用户报的那次泄漏：正常正文 + 收尾标记 + 一段 invoke
leak = (
    head
    + "\n\n</"
    + FULL
    + 'tool_calls> '
    + FULL
    + 'invoke name="search_knowledge_graph"> '
    + FULL
    + 'parameter name="keyword">管理局</'
    + FULL
    + "parameter>"
)

failures: list[str] = []


def check(label: str, got: str, want: str, tripped: bool, want_tripped: bool) -> None:
    ok = got == want and tripped == want_tripped
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(f"{label}\n   got={got!r} tripped={tripped}\n   want={want!r} tripped={want_tripped}")


def run(chunks: list[str]) -> tuple[str, bool]:
    sup = _MarkupSuppressor()
    out = "".join(sup.feed(c) for c in chunks) + sup.flush()
    return out, sup.tripped


# 1. 一次性喂进去
got, t = run([leak])
check("单块：标记被拦、正文留下", got, head, t, True)

# 2. 切成单字符流 —— 模拟分片把标记拦腰截断，这是最容易漏的情形
got, t = run(list(leak))
check("逐字符：分片切断标记也拦得住", got, head, t, True)

# 3. 每 7 个字符切一刀
got, t = run([leak[i : i + 7] for i in range(0, len(leak), 7)])
check("定长切片", got, head, t, True)

# 4. 半角竖线变体
got, t = run([head + " <" + HALF + "tool_calls>"])
check("半角竖线变体", got, head, t, True)

# 5. 干净文本原样通过（尾巴扣留逻辑不能吃掉正常正文）
clean = "根据《政府采购法》第二十六条，采购方式包括公开招标等六种。[1]"
got, t = run([clean])
check("干净文本：单块", got, clean, t, False)

got, t = run(list(clean))
check("干净文本：逐字符", got, clean, t, False)

# 6. 流在标记前缀处断掉 —— flush 必须原样放出，不能吞掉用户正文
partial = head + BAR + BAR + "DS"
got, t = run([partial])
check("断在前缀：flush 原样放出", got, partial, t, False)

# 7. 标记出现在正文中段：之前的保留，之后的丢弃
got, t = run(["前半句。[1]<" + FULL + "invoke>后半句不该出现"])
check("中段命中：保留前半句", got, "前半句。[1]", t, True)

# 8. 命中之后再来分片，必须继续静默
sup = _MarkupSuppressor()
sup.feed("<" + FULL)
after = "".join(sup.feed(c) for c in "后续全是标记") + sup.flush()
check("命中后持续静默", after, "", sup.tripped, True)

# 9. 裸标记（前面没有尖括号）不该触发——真实泄漏一定带 <，要求它能把误伤压到最低
got, t = run(["正文" + FULL + "正文"])
check("裸标记不误伤", got, "正文" + FULL + "正文", t, False)

# ======================================================================
# 10. llm.stream() 的接线 —— 上面验的是抑制器本身，这里验它**接在 /api/chat 链路上**
#
# 用户报的那次泄漏就出在这条链路上：`_MarkupSuppressor` 原先只长在
# `stream_with_tools()`（Agent 链路）里，而 `/api/chat` 走的是
# `rag.stream_answer()` → `llm.stream()`，逐片原样 yield，**一个字的过滤都没有**。
# 下面用一个假客户端喂分片，不需要任何服务、任何 Key。
# ======================================================================
from types import SimpleNamespace  # noqa: E402

from bidding_qa.llm import DeepSeekLLM  # noqa: E402

TAIL = _MarkupSuppressor._TAIL


def _chunk(content=None, reasoning=None):
    """一个流式分片。只带上真实存在的属性——stream() 用 getattr(…, None) 读它们。"""
    delta = SimpleNamespace()
    if content is not None:
        delta.content = content
    if reasoning is not None:
        delta.reasoning_content = reasoning
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class _FakeStream:
    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


class _FakeClient:
    """替掉 DeepSeekLLM._client。DeepSeekLLM() 的构造函数不建连接，所以不需要 Key。"""

    def __init__(self, chunks: list) -> None:
        async def _create(**kwargs):
            return _FakeStream(chunks)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


def stream_out(chunks: list) -> list[tuple[str, str]]:
    """跑一遍 llm.stream()，收集全部 (kind, text)。"""

    async def _go():
        llm = DeepSeekLLM()
        llm._client = _FakeClient(chunks)
        return [pair async for pair in llm.stream([{"role": "user", "content": "问"}])]

    import asyncio

    return asyncio.run(_go())


def expect(label: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(f"{label}\n   got ={got!r}\n   want={want!r}")


# 10a. ⭐ 无标记时**逐片相同**：改动只带来「尾部滞后 TAIL 字符」，flush 补齐，
#      拼接结果与输入逐字节一致。这条是「冻结路径不变」的护栏。
long_clean = "根据《政府采购法》第二十六条，采购方式包括公开招标、邀请招标等六种方式。[1][2]"
assert len(long_clean) > TAIL, "这条用例要求正文长于扣留的尾巴"
out = stream_out([_chunk(content=long_clean)])
expect("无标记：拼接后与原文逐字节相同", "".join(t for _, t in out), long_clean)
expect(
    f"无标记：逐片相同，仅尾部 {TAIL} 字符滞后一拍",
    out,
    [("content", long_clean[:-TAIL]), ("content", long_clean[-TAIL:])],
)
expect("无标记：不产出 markup_leak", [k for k, _ in out], ["content", "content"])

# 短正文（≤ 尾巴长度）全程扣着，收流一次放出——顺序与内容仍然不变
short_clean = "答案很短。[1]"
out = stream_out([_chunk(content=short_clean)])
expect("无标记：短正文由 flush 一次放出", out, [("content", short_clean)])

# 10b. 含标记：正文只到标记之前，且**额外产出 markup_leak**（原文交给调用方去恢复）
real_leak = (
    head
    + "\n\n<"
    + FULL
    + 'tool_calls> <'
    + FULL
    + 'invoke name="search_web"> <'
    + FULL
    + 'parameter name="query" string="true">香港政府采购招标制度</'
    + FULL
    + "parameter> </"
    + FULL
    + "invoke> </"
    + FULL
    + "tool_calls>"
)
out = stream_out([_chunk(content=head), _chunk(content=real_leak[len(head) :]), _chunk(content="后面全是标记")])
expect("有标记：正文只到标记之前", [t for k, t in out if k == "content"], [head])
expect("有标记：恰好产出一次 markup_leak", [k for k, _ in out].count("markup_leak"), 1)
expect("有标记：markup_leak 排在正文之后", [k for k, _ in out][-1], "markup_leak")
expect("有标记：markup_leak 带的是拦下来的原文（含工具名，交调用方恢复用）",
       "search_web" in dict(out)["markup_leak"], True)

# 逐字符喂（分片把标记拦腰截断）—— 最容易漏的情形。
# ⚠ 这里比的是**拼接结果**而不是分片列表：尾部扣留会让正文被切成几片（前几片先放行、
# 最后一片由命中那一下补上），切片边界取决于分片方式，不是这条用例要钉的东西
# （「无标记时逐片相同」由 10a 用单块输入精确钉住）。
out = stream_out([_chunk(content=c) for c in real_leak])
expect("有标记：逐字符分片也拦得住", "".join(t for k, t in out if k == "content"), head)
expect("有标记：逐字符分片仍能恢复出标记原文",
       "search_web" in dict(out).get("markup_leak", ""), True)

# 10c. ⚠ 只抑制 content，**不动 reasoning**：与 stream_with_tools() 的既有范围一致。
#      标记命中之后仍要照常透传思维链（抑制器不该把整条流掐死）。
out = stream_out([_chunk(reasoning="先想想。"), _chunk(content=head),
                  _chunk(content=real_leak[len(head) :]), _chunk(reasoning="被拦之后的思考。")])
expect("有标记：reasoning 照常透传（抑制器只管 content）",
       [t for k, t in out if k == "reasoning"], ["先想想。", "被拦之后的思考。"])

# 10d. 标记只出现在 reasoning 里时**不触发**抑制——范围是 content，本次刻意不放宽
out = stream_out([_chunk(reasoning="<" + FULL + "invoke name=x>")])
expect("标记只在 reasoning 里 → 不产出 markup_leak",
       [k for k, _ in out], ["reasoning"])

print()
if failures:
    print(f"{len(failures)} 项失败：")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("全部通过")
