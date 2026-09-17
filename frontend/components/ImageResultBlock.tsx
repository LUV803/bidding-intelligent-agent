"use client";

import { ChevronDown, FileImage } from "lucide-react";
import { Fragment, useState } from "react";
import clsx from "clsx";

import type { VisionPayload } from "@/lib/types";

/**
 * 一张图片的解析结果块（智谱视觉模型抽出来的结构化字段）。
 *
 * **刻意独立于 SourcePanel**（与 GraphResultBlock / PgResultBlock 同一条理由）：来源面板
 * 是带 [n] 编号的知识库问答对，会被正文角标联动；图片是**用户自己提供的材料**，
 * 既不参与引用编号、也不该和「资料」混在一起。提示词里也明写了不要给它标 [n]。
 *
 * 结构化字段而不是让模型自由输出 markdown，是为了让「它到底读出了什么」一眼可见——
 * 与 PgResultBlock 把 filters 摆出来是同一个道理：模型把「预算金额」读成了「中标金额」，
 * 不摆出来就没人看得出来。
 *
 * ⚠ 这里**不渲染原图**：原图 base64 用完即弃，没进 Message（塞进 localStorage 会撑爆
 * 5MB 配额，而且是静默失败）。见 types.ts 里 VisionPayload 的注释。
 */
export function ImageResultBlock({ result }: { result: VisionPayload }) {
  const [manual, setManual] = useState<boolean | null>(null);
  const expanded = manual ?? true;
  const [rawOpen, setRawOpen] = useState(false);

  const fields = Object.entries(result.fields);

  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-sidebar">
      <button
        type="button"
        onClick={() => setManual(!expanded)}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left transition-colors hover:bg-hover"
      >
        <FileImage className={clsx("h-3.5 w-3.5", result.ok ? "text-brand" : "text-amber-500")} />
        <span className="shrink-0 text-[13px] font-medium text-ink">图片解析</span>
        <span className="truncate text-[12px] text-muted">
          {result.ok ? result.doc_type : "解析失败"}
        </span>
        {result.ok && fields.length > 0 && (
          <span className="shrink-0 rounded-md bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand">
            {fields.length} 个字段
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
              {result.note ?? "图片解析失败。"}
            </p>
          ) : (
            <>
              {result.summary && (
                <p className="text-[12.5px] leading-relaxed text-muted">{result.summary}</p>
              )}

              {fields.length > 0 ? (
                // 两列键值网格：键右对齐成一条竖线，值左对齐。顺序就是后端的字段表顺序，
                // **不要排序**（同一个模型两次解析同一张图，卡片长得一样靠的就是这个）。
                <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12.5px]">
                  {fields.map(([key, value]) => (
                    // 不用 subgrid（Tailwind 3.4 才有）：dt/dd 直接做网格子项，
                    // Fragment 不产生 DOM 节点，两列自动对齐
                    <Fragment key={key}>
                      <dt className="whitespace-nowrap pt-[1px] text-faint">{key}</dt>
                      <dd className="whitespace-pre-wrap break-all leading-relaxed text-ink">
                        {value}
                      </dd>
                    </Fragment>
                  ))}
                </dl>
              ) : (
                !result.summary && <p className="text-[12.5px] text-muted">没有提取到结构化字段。</p>
              )}

              {result.full_text && (
                <>
                  <button
                    type="button"
                    onClick={() => setRawOpen((v) => !v)}
                    className="mt-2 flex items-center gap-1 text-[11.5px] text-muted transition-colors hover:text-ink"
                  >
                    <ChevronDown
                      className={clsx("h-3 w-3 transition-transform", rawOpen && "rotate-180")}
                    />
                    查看识别全文（{result.full_text.length} 字）
                  </button>
                  {rawOpen && (
                    <pre className="scroll-thin mt-1.5 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-canvas px-2.5 py-2 text-[12px] leading-relaxed text-muted">
                      {result.full_text}
                    </pre>
                  )}
                </>
              )}

              {/* 降级说明（例如模型没按 JSON 格式返回）。不命中就不出现 */}
              {result.note && <p className="mt-1.5 text-[11.5px] text-faint">{result.note}</p>}

              <p className="mt-1.5 text-[11.5px] text-faint">
                已作为本次提问的上下文提供给模型，耗时 {(result.elapsed_ms / 1000).toFixed(1)}s
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
