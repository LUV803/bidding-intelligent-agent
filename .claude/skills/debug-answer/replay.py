#!/usr/bin/env python
"""重放一次工具调用，还原**模型当时看到的东西**。

存在的理由：`assess_outcome()` 产出的【结果质量】段落只被塞进
`messages` 里的 `role:"tool"` 消息（`agent.py:451-457`），**不进 SSE 事件、
不进后端日志**。所以出问题时，前端和日志里都没有它 —— 只能重放。

重放出来三样（前两样是模型真正读到的）：
  1. `_render_result()` 的正文（工具回传文本）
  2. 【结果质量】段落（不达标时才追加；合格时本工具不追加任何东西）
  3. 结构化结果（graph/pg 的 model_dump）—— 用来核对数字

⚠ 只有图谱 / 采购库 / 联网能重放。**知识库检索不行**：它要真 retriever，
会抢 Qdrant 排他锁（后端在跑时会直接抛）。要查检索那一路，看 SSE 的 `sources` 分数
（见 SKILL.md 第二步），别在这儿重放。

用法：
  uv run python .claude/skills/debug-answer/replay.py <工具名> '<JSON 参数>'
  uv run python .claude/skills/debug-answer/replay.py search_procurement_records '{"contains":"空调"}'
  uv run python .claude/skills/debug-answer/replay.py search_knowledge_graph '{"query_type":"subject_suppliers","entity":"空调"}'

  # 还原「当时本轮还挂着哪些工具」—— 它会出现在【结果质量】的第二行
  uv run python .claude/skills/debug-answer/replay.py <工具> '<JSON>' --available 检索,图谱,采购库

⚠ 重放的是**当前**数据。库改过的话（`to_postgresql.py` 重灌过）数字会与当时不同 ——
  那时以「形状」为准（有没有结果、闸门丢没丢），别硬比具体数字。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

if sys.version_info < (3, 12):
    sys.exit(f"需要 Python 3.12+（当前 {sys.version.split()[0]}）。用 uv run python 跑。")

sys.stdout.reconfigure(encoding="utf-8")

from bidding_qa.clients.base_client import ToolCall  # noqa: E402
from bidding_qa.tools.base import (  # noqa: E402
    SourceRegistry,
    ToolContext,
    assess_outcome,
    execute_tool,
)
from bidding_qa.tools.rag_tools import TOOL_GRAPH, TOOL_PG, TOOL_SCHEMAS  # noqa: E402

#: 工具名 → 中文短名。命令行里打中文比打 `search_procurement_records` 顺手。
ALIAS = {
    "检索": "search_bidding_knowledge", "知识库": "search_bidding_knowledge",
    "图谱": TOOL_GRAPH, "graph": TOOL_GRAPH,
    "采购库": TOOL_PG, "数据库": TOOL_PG, "pg": TOOL_PG,
    "联网": "search_web", "web": "search_web",
}

DEFAULT_NAMES = tuple(s["function"]["name"] for s in TOOL_SCHEMAS)


def _resolve(raw: str) -> str:
    return ALIAS.get(raw.strip(), raw.strip())


async def main() -> int:
    p = argparse.ArgumentParser(description="重放一次工具调用，还原模型看到的东西")
    p.add_argument("tool", help="工具名（或中文别名：图谱 / 采购库 / 联网）")
    p.add_argument("args", nargs="?", default="{}", help="参数 JSON，如 '{\"contains\":\"空调\"}'")
    p.add_argument("--available", default=None,
                   help="本轮还挂着哪些工具（逗号分隔，中文别名也行）；"
                        "不给就用 TOOL_SCHEMAS（进程级那三个）")
    p.add_argument("--json", action="store_true", help="只吐结构化结果")
    a = p.parse_args()

    name = _resolve(a.tool)
    try:
        args = json.loads(a.args)
    except json.JSONDecodeError as exc:
        print(f"参数不是合法 JSON：{exc}\n收到的是：{a.args!r}", file=sys.stderr)
        return 2

    if a.available:
        available = tuple(_resolve(x) for x in a.available.split(",") if x.strip())
    else:
        available = DEFAULT_NAMES

    # ⚠ retriever=None 是安全的：图谱/采购库/联网都不读它（唯一使用点在
    #   KnowledgeBaseTool.run 里）。所以这个脚本**后端在跑也能用**。
    #   ⚠ 但 `search_bidding_knowledge` 例外 —— 它会立刻 AttributeError，见文件头。
    ctx = ToolContext(
        registry=SourceRegistry(cap=20),
        retriever=None,  # type: ignore[arg-type]
        mode="hybrid",
        rerank=False,
        tool_names=available,
    )

    out = await execute_tool(
        ToolCall(id="replay", name=name, arguments=json.dumps(args)), ctx
    )

    if a.json:
        struct = out.graph if name == TOOL_GRAPH else out.pg
        print(json.dumps(
            {"ok": out.ok, "error": out.error, "data": struct.model_dump() if struct else {}},
            ensure_ascii=False, indent=2, default=str,
        ))
        return 0 if out.ok else 1

    # ---------- 模型看到的第一段：工具回传正文 ----------
    print("=" * 60)
    print(f"工具：{name}")
    print(f"参数：{json.dumps(args, ensure_ascii=False)}")
    print(f"本轮挂着：{'、'.join(available)}")
    print("=" * 60)
    print()
    print("---------- ① 工具回传正文（模型读到的原文） ----------")
    print(out.text)

    # ---------- 模型看到的第二段：【结果质量】 ----------
    quality = assess_outcome(name, out, available)
    print()
    print("---------- ② 【结果质量】（不达标才追加） ----------")
    if quality:
        print(quality)
    else:
        # ⚠ 合格时**什么都不追加** —— 连分隔符都不多一个。这是 `_render_result`
        #   字面量不变的保证，别以为是「漏了」。
        print("（无 —— 结果合格，或这个 error 码属于「模型知道怎么改」那类，")
        print("  `_SELF_CORRECTING_ERRORS` 里有它：unknown_tool / bad_arguments /")
        print("  bad_query_type / empty_query / duplicate_query）")

    # ---------- 第三段：结构化结果（核对数字用） ----------
    struct = out.graph if name == TOOL_GRAPH else out.pg
    print()
    print("---------- ③ 结构化结果 ----------")
    if struct is None:
        print("（这个工具没有结构化结果 —— 检索与联网都没有）")
    else:
        d = struct.model_dump()
        print(f"ok={d.get('ok')} mode={d.get('mode')} total={d.get('total')} "
              f"total_capped={d.get('total_capped')} truncated={d.get('truncated')}")
        if d.get("filters"):
            print(f"实际生效的筛选（核对「模型理解对了吗」）：{d['filters']}")
        if d.get("extra"):
            print(f"附加统计：{d['extra']}")
        if d.get("resolved") or d.get("candidates"):
            print(f"实体解析：resolved={d.get('resolved')!r} candidates={d.get('candidates')!r}")
        if d.get("note"):
            print(f"note：{d['note']}")
        print(f"rows（{len(d.get('rows') or [])} 条）：")
        print(json.dumps(d.get("rows") or [], ensure_ascii=False, indent=2, default=str))

    print()
    print(f"耗时：{out.elapsed_ms}ms")
    if not out.ok:
        print(f"⚠ ok=False，error={out.error!r}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
