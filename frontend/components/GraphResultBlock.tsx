"use client";

import { ChevronDown, Network } from "lucide-react";
import { useState } from "react";
import clsx from "clsx";

import type { GraphPayload } from "@/lib/types";

const int = (v: number) => v.toLocaleString("zh-CN");

/** 图谱里的交易额都是整元，不保留小数 */
const money = (v: number) => `¥${v.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;

/** 概要数字按名字判断格式：含「额」的是金额，其余是计数 */
const statValue = (key: string, v: number) => (key.includes("额") ? money(v) : int(v));

/**
 * agent 工具 search_knowledge_graph 的结果块。
 *
 * **刻意独立于 SourcePanel**：来源面板是带 [n] 编号的知识库问答对，会被正文角标联动；
 * 图谱是结构化统计，不参与引用编号。混在一起会让人以为图谱行也是「资料」。
 *
 * 表格列按数据动态决定（哪些字段非空就显示哪些），不写死：
 * entity_search 只有笔数、subject_profile 有笔数+地区、聚合排行有笔数+金额。
 */
export function GraphResultBlock({ result }: { result: GraphPayload }) {
  const [manual, setManual] = useState<boolean | null>(null);
  const expanded = manual ?? true;

  const showAmount = result.rows.some((r) => r.amount != null);
  const stats = Object.entries(result.extra);
  // 服务端做过模糊纠名：必须显示出来，否则用户会以为查的就是自己问的那个
  const renamed = Boolean(result.resolved && result.resolved !== result.entity);
  const total = result.total_capped ? `${int(result.total)}+` : int(result.total);

  return (
    <div className="mb-3 overflow-hidden rounded-2xl border border-line bg-sidebar">
      <button
        type="button"
        onClick={() => setManual(!expanded)}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left transition-colors hover:bg-hover"
      >
        <Network className={clsx("h-3.5 w-3.5", result.ok ? "text-brand" : "text-amber-500")} />
        <span className="shrink-0 text-[13px] font-medium text-ink">知识图谱查询</span>
        <span className="truncate text-[12px] text-muted">{result.query_label}</span>
        {result.ok && (result.rows.length > 0 || stats.length > 0) && (
          <span className="shrink-0 rounded-md bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand">
            共 {total} 条
          </span>
        )}
        <ChevronDown
          className={clsx(
            "ml-auto h-4 w-4 shrink-0 text-faint transition-transform",
            expanded && "rotate-180",
          )}
        />
      </button>

      {expanded && (
        <div className="border-t border-line px-3.5 py-2.5">
          {!result.ok ? (
            <p className="text-[12.5px] text-amber-600 dark:text-amber-400">
              {result.note ?? "图谱查询失败。本次回答只依据知识库资料。"}
            </p>
          ) : (
            <>
              {result.entity && (
                <p className="text-[12px] text-muted">
                  关键词「{result.entity}」
                  {renamed && (
                    <>
                      　→　实际命中
                      <span className="ml-1 font-medium text-ink">{result.resolved}</span>
                    </>
                  )}
                </p>
              )}

              {stats.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {stats.map(([key, value]) => (
                    <span
                      key={key}
                      className="rounded-lg bg-canvas px-2 py-1 text-[11.5px] text-muted"
                    >
                      {key}
                      <span className="ml-1 font-medium text-ink">{statValue(key, value)}</span>
                    </span>
                  ))}
                </div>
              )}

              {result.rows.length > 0 ? (
                <table className="mt-2 w-full text-[12.5px]">
                  <thead>
                    <tr className="text-[11.5px] text-faint">
                      <th className="w-6 py-1 text-left font-normal">#</th>
                      <th className="py-1 text-left font-normal">名称</th>
                      <th className="w-16 py-1 text-right font-normal">笔数</th>
                      {showAmount && <th className="w-24 py-1 text-right font-normal">金额</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, i) => (
                      <tr key={`${row.label}-${i}`} className="border-t border-line">
                        <td className="py-1 text-faint">{i + 1}</td>
                        <td className="break-all py-1 pr-2 text-ink">{row.label}</td>
                        <td className="py-1 text-right tabular-nums text-muted">{int(row.count)}</td>
                        {showAmount && (
                          <td className="py-1 text-right tabular-nums text-muted">
                            {row.amount == null ? "—" : money(row.amount)}
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                stats.length === 0 && (
                  <p className="text-[12.5px] text-muted">图谱中没有匹配的数据。</p>
                )
              )}

              {result.truncated && (
                <p className="mt-1.5 text-[11.5px] text-faint">
                  {result.total_capped
                    ? "命中的实体较多，此处只展示前若干条。"
                    : `共 ${int(result.total)} 条，此处只展示前 ${result.rows.length} 条。`}
                </p>
              )}

              {result.candidates.length > 0 && (
                <p className="mt-1.5 text-[11.5px] text-faint">
                  还匹配到其他实体：{result.candidates.slice(0, 8).join("、")}
                  {result.candidates.length > 8 && " 等"}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
