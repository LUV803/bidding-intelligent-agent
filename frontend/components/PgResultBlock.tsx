"use client";

import { ChevronDown, Database } from "lucide-react";
import { useState } from "react";
import clsx from "clsx";

import type { PgPayload } from "@/lib/types";

const int = (v: number) => v.toLocaleString("zh-CN");

/** 采购金额都是整元，不保留小数 */
const money = (v: number) => `¥${v.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;

/** 概要数字按名字判断格式：含「额」的是金额，其余是计数 */
const statValue = (key: string, v: number) => (key.includes("额") ? money(v) : int(v));

/**
 * agent 工具 search_procurement_records 的结果块。
 *
 * **刻意独立于 SourcePanel**（与 GraphResultBlock 同一条理由）：来源面板是带 [n] 编号的
 * 知识库问答对，会被正文角标联动；采购记录是「项目 + 金额 + 时间」的结构化行，
 * 不参与引用编号。混在一起会让人以为数据库里的行也是「资料」。
 *
 * 顶部把 `filters` 显式渲染出来是**这个块的主要价值**：模型把「安徽」理解成省份筛选、
 * 把「过亿」理解成金额下限、把「最多」理解成按笔数分组——这些理解偏差不摆出来的话，
 * 一个答非所问的结果看起来和正确的完全一样。
 *
 * 表格列按 `mode` 分流（明细 / 分组是两种完全不同的形状），而不是按「哪些字段非空」——
 * 分组行的 label 在明细里没有对应列，硬合一套表会串行。
 */
export function PgResultBlock({ result }: { result: PgPayload }) {
  const [manual, setManual] = useState<boolean | null>(null);
  const expanded = manual ?? true;

  const stats = Object.entries(result.extra);
  const filters = Object.entries(result.filters);
  const isGroup = result.mode === "group";
  const unit = isGroup ? "组" : "条";
  // 明细里列很多，只展示真正有值的那几列（这一批数据里 代理机构 88% 为空，但没进 rows）
  const detailCols = result.rows.some((r) => r.purchaser || r.supplier);

  return (
    <div className="mb-3 overflow-hidden rounded-2xl border border-line bg-sidebar">
      <button
        type="button"
        onClick={() => setManual(!expanded)}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left transition-colors hover:bg-hover"
      >
        <Database className={clsx("h-3.5 w-3.5", result.ok ? "text-brand" : "text-amber-500")} />
        <span className="shrink-0 text-[13px] font-medium text-ink">采购数据库查询</span>
        <span className="truncate text-[12px] text-muted">{result.query_label}</span>
        {result.ok && result.total > 0 && (
          <span className="shrink-0 rounded-md bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand">
            共 {int(result.total)} {unit}
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
              {result.note ?? "数据库查询失败。本次回答只依据知识库资料。"}
            </p>
          ) : (
            <>
              {filters.length > 0 ? (
                <p className="text-[12px] text-muted">
                  筛选：
                  <span className="text-ink">
                    {filters.map(([k, v]) => `${k}=${v}`).join("，")}
                  </span>
                </p>
              ) : (
                <p className="text-[12px] text-muted">
                  未应用筛选条件（按{result.mode === "group" ? "分组统计" : "排序"}取前若干{unit}）
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
                isGroup ? (
                  <table className="mt-2 w-full text-[12.5px]">
                    <thead>
                      <tr className="text-[11.5px] text-faint">
                        <th className="w-6 py-1 text-left font-normal">#</th>
                        <th className="py-1 text-left font-normal">分组</th>
                        <th className="w-16 py-1 text-right font-normal">记录数</th>
                        <th className="w-28 py-1 text-right font-normal">金额合计</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, i) => (
                        <tr key={`${row.label}-${i}`} className="border-t border-line">
                          <td className="py-1 text-faint">{i + 1}</td>
                          <td className="break-all py-1 pr-2 text-ink">{row.label}</td>
                          <td className="py-1 text-right tabular-nums text-muted">
                            {int(row.count ?? 0)}
                          </td>
                          <td className="py-1 text-right tabular-nums text-muted">
                            {row.amount == null ? "—" : money(row.amount)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <ul className="mt-2 space-y-1.5">
                    {result.rows.map((row, i) => (
                      <li key={`${row.project}-${i}`} className="border-t border-line pt-1.5">
                        <p className="text-[12.5px] text-ink">
                          <span className="mr-1.5 text-faint">{i + 1}</span>
                          {row.project || row.title || "（无标题）"}
                        </p>
                        <p className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11.5px] text-muted">
                          {row.purchaser && <span>采购人：{row.purchaser}</span>}
                          {detailCols && row.supplier && <span>中标人：{row.supplier}</span>}
                          {row.amount != null && (
                            <span className="tabular-nums">金额：{money(row.amount)}</span>
                          )}
                          {row.published && <span>{row.published}</span>}
                          {row.region && <span>{row.region}</span>}
                        </p>
                      </li>
                    ))}
                  </ul>
                )
              ) : (
                <p className="mt-1.5 text-[12.5px] text-muted">数据库中没有匹配的记录。</p>
              )}

              {result.truncated && (
                <p className="mt-1.5 text-[11.5px] text-faint">
                  共 {int(result.total)} {unit}，此处只展示前 {result.rows.length} {unit}。
                </p>
              )}

              {result.note && result.rows.length > 0 && (
                <p className="mt-1.5 text-[11.5px] text-faint">{result.note}</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
