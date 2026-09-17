#!/usr/bin/env python
"""直调工具的执行入口（SKILL.md 里说的「模式 B」）。

⚠ 只调图谱 / 采购库 —— 两条路都不碰 ctx.retriever（唯一使用点在 rag_tools.py:118 的
KnowledgeBaseTool.run 里），所以 **后端在跑着也照用**（不抢 Qdrant 排他锁）。
本脚本**不 import 检索器**，正是为了保住这一条。

用法：
  uv run python .claude/skills/bid-analysis/tool.py schema
  uv run python .claude/skills/bid-analysis/tool.py graph top_suppliers
  uv run python .claude/skills/bid-analysis/tool.py graph subject_suppliers --entity 空调
  uv run python .claude/skills/bid-analysis/tool.py pg --contains 空调 --group-by category
  uv run python .claude/skills/bid-analysis/tool.py pg --sort-by amount --max-rows 10 --json

⚠ 中文参数在 Git Bash 里没问题（不是 curl 那条 GBK 路径），但输出要 reconfigure 成 utf-8。
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
from bidding_qa.tools.base import SourceRegistry, ToolContext, execute_tool  # noqa: E402
from bidding_qa.tools.rag_tools import TOOL_GRAPH, TOOL_PG, TOOL_SCHEMAS  # noqa: E402

# 本轮「挂着」的工具表。图谱/采购库在不在表里由 .env 决定（graph_store.ENABLED 等），
# 照抄 TOOL_SCHEMAS 就是如实反映服务端的可用集合 —— 手动写死会在没配 Neo4j 的机器上撒谎。
NAMES: tuple[str, ...] = tuple(s["function"]["name"] for s in TOOL_SCHEMAS)

# ⚠ ctx.retriever 必须显式传 None（ToolContext 是 dataclass，该字段无默认值）。
# 传 None 是安全的：图谱/采购库/联网三个工具都不读它。
CTX = ToolContext(
    registry=SourceRegistry(cap=20),
    retriever=None,  # type: ignore[arg-type]
    mode="hybrid",
    rerank=False,
    tool_names=NAMES,
)


async def call(name: str, args: dict) -> tuple[bool, str, dict]:
    """跑一次工具。返回 (ok, 渲染文本, 结构化结果)。"""
    # ⚠ ToolCall.arguments 是 **JSON 字符串**，不是 dict（clients/base_client.py:120-126）。
    out = await execute_tool(ToolCall(id="cli", name=name, arguments=json.dumps(args)), CTX)
    struct = out.graph if name == TOOL_GRAPH else out.pg
    return out.ok, out.text, (struct.model_dump() if struct is not None else {})


def _print(ok: bool, text: str, data: dict, as_json: bool) -> None:
    if as_json:
        # 结构化结果原样吐出，好让调用方自己算比例、拼报告
        print(json.dumps({"ok": ok, "data": data}, ensure_ascii=False, indent=2, default=str))
        return
    # ⚠ flush：下面几行走 stderr，不 flush 的话 stderr 会抢在整段正文之前冒出来
    #   （两个流各自缓冲），看着像「警告在前、正文在后」的错序。
    print(text, flush=True)
    if not ok:
        print(f"\n⚠ ok=False（err={data.get('note')!r}）—— 参数没被接受，按上面的提示改。",
              file=sys.stderr)
    # 结构化结果里的两个「不能当结论用」的字段，单列提醒，免得只看渲染文本时漏掉
    if data.get("total_capped"):
        print("\n⚠ total_capped=True ⇒ 上面的 total 只是**下限**（撞上服务端硬上限 500），"
              "不能说「一共 N 条」。", file=sys.stderr)
    if data.get("truncated"):
        print("⚠ truncated=True ⇒ rows 被截断过，明细不全。", file=sys.stderr)


# ---------------------------------------------------------------- 采购库参数
# arg 名 → 工具参数名。不用 argparse 的 dest 转换（`--group-by` → group_by 要手写）。
_PG_ARGS = [
    ("--contains", "contains"), ("--purchaser", "purchaser"), ("--supplier", "supplier"),
    ("--agency", "agency"), ("--province", "province"), ("--city", "city"),
    ("--county", "county"), ("--category", "category"), ("--source", "source"),
    ("--date-from", "date_from"), ("--date-to", "date_to"), ("--date-field", "date_field"),
    ("--sort-by", "sort_by"), ("--group-by", "group_by"),
]


def main() -> int:
    p = argparse.ArgumentParser(description="直调图谱 / 采购库工具（模式 B）")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ⚠ `--json` 必须挂在**每个子命令**上（用 parents 共享），不能挂在主解析器上：
    #   挂在主解析器上时 `tool.py pg --json` 会报 unrecognized arguments —— argparse
    #   只认「子命令之前」的全局选项。（踩过一次。）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="吐结构化结果而不是渲染文本")

    sub.add_parser("schema", help="打印两个工具的参数 schema", parents=[common])
    sub.add_parser("tools", help="打印本轮挂着的工具名", parents=[common])

    g = sub.add_parser("graph", help="知识图谱查询", parents=[common])
    g.add_argument("query_type", help="见 SKILL.md 的 9 个类型")
    g.add_argument("--entity", default="", help="实体名；6 个类型必填")

    q = sub.add_parser("pg", help="采购记录库查询", parents=[common])
    for flag, dest in _PG_ARGS:
        q.add_argument(flag, dest=dest, default=None)
    q.add_argument("--amount-min", dest="amount_min", type=float, default=None)
    q.add_argument("--amount-max", dest="amount_max", type=float, default=None)
    q.add_argument("--max-rows", dest="max_rows", type=int, default=None)
    q.add_argument("--sort-desc", dest="sort_desc", action="store_true", default=None)

    a = p.parse_args()

    if a.cmd == "schema":
        for s in TOOL_SCHEMAS:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "tools":
        print("\n".join(NAMES))
        return 0

    if a.cmd == "graph":
        if TOOL_GRAPH not in NAMES:
            print("⚠ 图谱工具不在本轮工具表里 —— NEO4J_URI 没配，或 neo4j 驱动没装。",
                  file=sys.stderr)
            return 2
        name, args = TOOL_GRAPH, {"query_type": a.query_type, "entity": a.entity}
    else:
        if TOOL_PG not in NAMES:
            print("⚠ 采购库工具不在本轮工具表里 —— POSTGRES_HOST 没配。", file=sys.stderr)
            return 2
        name = TOOL_PG
        args = {d: getattr(a, d) for _, d in _PG_ARGS if getattr(a, d) is not None}
        for k in ("amount_min", "amount_max", "max_rows", "sort_desc"):
            if getattr(a, k) is not None:
                args[k] = getattr(a, k)
        # ⚠ 一个条件都不给 = 全表 8751 行（工具会如实说「未应用任何筛选条件」）。
        #   不算错误（sort_by 单独存在也算「说清了要什么」），但几乎不会是你要的，提醒一句。
        if not args:
            print("⚠ 没给任何条件 —— 这条查询会扫全表。见 SKILL.md「空筛选」那条。",
                  file=sys.stderr)

    ok, text, data = asyncio.run(call(name, args))
    _print(ok, text, data, a.json)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
