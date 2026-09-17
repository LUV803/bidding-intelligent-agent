"use client";

import { ChevronDown, Wrench } from "lucide-react";
import { useState } from "react";
import clsx from "clsx";

import type { ToolStep } from "@/lib/types";

/**
 * 工具名 → 三种文案（标题 / 运行中 / 完成）。
 *
 * **刻意合并成一张表**：原先这里是三处各写一遍工具名的分支——`TOOL_LABELS`、
 * `doneText()`、以及下面运行中文案的三元表达式。新增一个工具要改三个地方，
 * 漏掉任何一处的表现都是「模型调了但界面上一片空白」。一处漏改总好过三处。
 *
 * ⚠ 服务端那边新增一个工具**只有一处**：写一个 `BaseTool` 子类（schema 由 Pydantic 模型
 * 生成、处理器由注册表派生、展示由 `peek_display()` 覆写）。这里这张表是**前端**那一处，
 * 加上它下面的 `doneText()` 分支 —— 总共两处，漏掉任何一处的表现都是「模型调了但界面上一片空白」。
 */
const TOOLS: Record<string, { label: string; running: string }> = {
  search_bidding_knowledge: { label: "检索知识库", running: "检索中…" },
  search_knowledge_graph: { label: "查询知识图谱", running: "查询图谱中…" },
  search_procurement_records: { label: "查询采购数据库", running: "查询数据库中…" },
  search_web: { label: "联网搜索", running: "联网搜索中…" },
  search_web_exa: { label: "换引擎搜索", running: "换引擎搜索中…" },
  load_skill: { label: "加载技能", running: "加载技能中…" },
};

/**
 * 完成态文案按工具分流。
 *
 * 不能一律套「新增 N 条资料 / 没有新增资料（与已有资料重复）」——图谱查询与数据库查询
 * 都不是检索，返回 0 行只是「那边没这个数据」，说成「与已有资料重复」是错的。
 */
function doneText(step: ToolStep): string {
  if (step.graph) {
    const g = step.graph;
    if (!g.ok) return g.note ?? "图谱查询失败";
    if (!g.rows.length && !Object.keys(g.extra).length) return "图谱中没有匹配数据";
    const totalText = g.total_capped ? `${g.total}+` : `${g.total}`;
    return g.total_capped || g.rows.length < g.total
      ? `返回前 ${g.rows.length} 条 / 共 ${totalText} 条`
      : `返回 ${g.rows.length} 条`;
  }
  if (step.pg) {
    const p = step.pg;
    if (!p.ok) return p.note ?? "数据库查询失败";
    if (!p.rows.length) return "数据库中没有匹配记录";
    // 总数是 count(*) 的精确值（不是图谱那种「下限」），所以直接说「共 N」
    const unit = p.mode === "group" ? "组" : "条";
    return p.rows.length < p.total
      ? `返回前 ${p.rows.length} ${unit} / 共 ${p.total} ${unit}`
      : `返回 ${p.total} ${unit}`;
  }
  // 联网搜索：它的结果同样带 [n] 进来源面板（所以 newCount 是有值的，不像图谱/数据库
  // 那两个被守卫置成 undefined），但**「0 条」的含义不同**——知识库那套叫「与已有资料
  // 重复」，而联网搜出 0 条是「网上也没有」，两者听起来差不多、实际结论相反。
  // ⚠ 判 `step.name` 而不是判载荷：ToolStep 上没有 web 载荷（联网结果已并入来源面板，
  //   再加一个结构化字段就是同一份数据画两遍）。
  if (step.name === "search_web") {
    return step.newCount ? `新增 ${step.newCount} 条网页结果` : "网上没有搜到相关结果";
  }
  // Exa 备用引擎：与 search_web 同形（结果同样并入来源面板、同样只有 newCount），
  // 但文案要**分清是哪个引擎**——两个引擎可能在同一次提问里先后各跑一次，
  // 两句都写「网上没有搜到」的话，用户分不清是哪一次落空。
  // ⚠ 必须判在下面那个兜底 return **之前**（同 search_web 那条注释）。
  if (step.name === "search_web_exa") {
    return step.newCount ? `换引擎新增 ${step.newCount} 条网页结果` : "换引擎也没有搜到";
  }
  // 加载技能：读进来的是一份**说明书**，不是资料 —— 所以既不能说「新增 N 条资料」，
  // 更不能说「与已有资料重复」。⚠ 同样按 `step.name` 判：它的 `newCount` 是 undefined
  // （见 useChat.ts 那个守卫），落进下面的兜底就会显示成「没有新增资料（与已有资料重复）」。
  if (step.name === "load_skill") {
    return "已读入说明书";
  }
  return step.newCount ? `新增 ${step.newCount} 条资料` : "没有新增资料（与已有资料重复）";
}

/**
 * Agent 模式的工具调用步骤。
 *
 * 与 ThinkingBlock 相同的折叠惯用法：用户一旦手动操作过，就不再自动干预。
 * 默认展开——这几步是「回答为什么是这么来的」的直接证据，藏起来就失去意义了。
 */
export function ToolCallBlock({ steps }: { steps: ToolStep[] }) {
  const [manual, setManual] = useState<boolean | null>(null);
  const running = steps.some((s) => s.status === "running");
  const expanded = manual ?? true;

  return (
    <div className="mb-3 overflow-hidden rounded-2xl border border-line bg-sidebar">
      <button
        type="button"
        onClick={() => setManual(!expanded)}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left transition-colors hover:bg-hover"
      >
        <Wrench
          className={clsx("h-3.5 w-3.5", running ? "animate-blink text-brand" : "text-muted")}
        />
        <span className="text-[13px] font-medium text-ink">
          {running ? "正在调用工具" : `已调用 ${steps.length} 次工具`}
        </span>
        {!running && (
          <span className="rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-muted">已完成</span>
        )}
        <ChevronDown
          className={clsx(
            "ml-auto h-4 w-4 text-faint transition-transform",
            expanded && "rotate-180",
          )}
        />
      </button>

      {expanded && (
        <ol className="border-t border-line px-3.5 py-2.5">
          {steps.map((step) => (
            <li key={step.id} className="flex items-start gap-2 py-1.5 text-[13px]">
              <span
                className={clsx(
                  "mt-[6px] h-1.5 w-1.5 shrink-0 rounded-full",
                  step.status === "running" && "animate-blink bg-brand",
                  step.status === "done" && "bg-brand",
                  step.status === "error" && "bg-amber-500",
                )}
              />
              <div className="min-w-0 flex-1">
                <span className="text-muted">{TOOLS[step.name]?.label ?? step.name}</span>
                {step.query && (
                  <span className="ml-1.5 break-all text-ink">「{step.query}」</span>
                )}
                <div className="mt-0.5 text-[11.5px] text-faint">
                  {step.status === "running" && (TOOLS[step.name]?.running ?? "调用中…")}
                  {step.status === "done" && doneText(step)}
                  {step.status === "error" && (step.error ?? "调用失败")}
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
