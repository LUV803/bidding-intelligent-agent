"use client";

import { ChevronDown, Database, ExternalLink, FileText, Globe, Layers, Sparkles } from "lucide-react";
import { useState } from "react";
import clsx from "clsx";

import type { Source } from "@/lib/types";

/**
 * 从 URL 里取域名做展示（顺手去掉 www. 前缀）。
 *
 * 联网条目的来源标识没法靠 `matched_by`（那套是「语义/关键词」，网页没有检索通道），
 * 所以直接摆域名——**用户的判断依据在这里**：网页结果不是知识库条目，是否可信得他自己看。
 */
function domainOf(url?: string | null): string {
  if (!url) return "网页";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "网页";
  }
}

/** 把命中的关键词包成高亮标记 */
function highlight(text: string, keywords: string[]) {
  const terms = keywords.filter((k) => k && k.length >= 2);
  if (!terms.length) return text;

  const escaped = terms
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .sort((a, b) => b.length - a.length);
  const re = new RegExp(`(${escaped.join("|")})`, "g");

  return text.split(re).map((part, i) =>
    terms.includes(part) ? (
      <mark key={i} className="kw-mark">
        {part}
      </mark>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

export function SourcePanel({
  sources,
  idPrefix,
  open,
  onToggle,
  activeIndex,
  onSelect,
}: {
  sources: Source[];
  idPrefix: string;
  open: boolean;
  onToggle: () => void;
  activeIndex?: number | null;
  onSelect?: (index: number) => void;
}) {
  // 只要有一条带重排分，就说明这次检索走了重排链路
  const isReranked = sources.some((s) => s.rerank_score !== null);

  if (!sources.length) {
    return (
      <div className="mt-3 flex items-center gap-2 rounded-xl border border-dashed border-line px-3.5 py-2.5 text-[13px] text-muted">
        <Database className="h-3.5 w-3.5" />
        知识库中未匹配到相关条目
      </div>
    );
  }

  return (
    <div className="mt-3 overflow-hidden rounded-2xl border border-line bg-surface">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left text-[13px] text-muted transition-colors hover:bg-hover"
      >
        <Layers className="h-3.5 w-3.5 text-brand" />
        <span className="font-medium text-ink">参考来源</span>
        <span className="rounded-full bg-brand-soft px-1.5 py-0.5 text-[11px] font-medium text-brand">
          {sources.length}
        </span>
        {isReranked && (
          <span
            title="已用 CrossEncoder 重排模型对初筛结果二次精排"
            className="flex items-center gap-0.5 rounded-full border border-brand/30 px-1.5 py-0.5 text-[10.5px] text-brand"
          >
            <Sparkles className="h-2.5 w-2.5" />
            已重排
          </span>
        )}
        <span className="ml-auto text-[12px] text-faint">
          {open ? "收起" : "展开"}
        </span>
        <ChevronDown
          className={clsx("h-4 w-4 transition-transform", open && "rotate-180")}
        />
      </button>

      {open && (
        <div className="animate-fade-up border-t border-line">
          {sources.map((source) => (
            <SourceCard
              key={source.doc_id}
              idPrefix={idPrefix}
              source={source}
              active={activeIndex === source.index}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function SourceCard({
  source,
  idPrefix,
  active,
  onSelect,
}: {
  source: Source;
  idPrefix: string;
  active?: boolean;
  onSelect?: (index: number) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  // 联网条目与知识库条目的字段口径完全不同：分数是 Tavily 的「相关性」而不是融合分，
  // 没有检索通道、没有重排位次，原文在站外。整张卡片按这个开关分叉。
  const isWeb = source.origin === "web";

  // 重排带来的位次变化：rank_fused 是重排前的名次，index 是重排后的名次。
  // 联网条目的 rank_fused 恒为 null ⇒ shift 恒为 0 ⇒ 天然不显示位次徽章。
  const shift = source.rank_fused === null ? 0 : source.rank_fused - source.index;

  return (
    <div
      id={`source-${idPrefix}-${source.index}`}
      onClick={() => onSelect?.(source.index)}
      className={clsx(
        "cursor-pointer border-b border-line px-3.5 py-3 transition-colors last:border-b-0",
        active ? "bg-brand-soft/60" : "hover:bg-hover",
      )}
    >
      <div className="flex items-start gap-2.5">
        <span className="mt-0.5 flex h-[20px] min-w-[20px] items-center justify-center rounded-md bg-brand text-[11px] font-semibold text-white">
          {source.index}
        </span>
        {shift !== 0 && (
          <span
            title={`重排前位于第 ${source.rank_fused} 位`}
            className={clsx(
              "mt-0.5 rounded px-1 py-px text-[10.5px] font-medium",
              shift > 0
                ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                : "bg-amber-500/10 text-amber-600 dark:text-amber-500",
            )}
          >
            {shift > 0 ? `↑${shift}` : `↓${-shift}`}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <p className="text-[13.5px] font-medium leading-snug text-ink">
            {source.question}
          </p>

          <p className="mt-1.5 text-[13px] leading-relaxed text-muted">
            {highlight(source.snippet, source.highlights)}
          </p>

          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-faint">
            {isWeb ? (
              <>
                {/*
                  ⚠ 这里**必须**与知识库那条分开渲染，不能只靠 origin 换个词：
                  下面那句「融合得分」原本是**无条件**渲染的，而它的高亮条件恰好是
                  `rerank_score === null` —— 网页条目正好命中，会显示成
                  「融合得分 87%」并且被**高亮成 brand 色**，比不显示更误导。
                */}
                <span
                  title="Tavily 给出的相关性评分（0~1），越高越贴题"
                  className="font-medium text-brand"
                >
                  相关性 {(source.score * 100).toFixed(0)}%
                </span>
                <span className="flex items-center gap-1" title={source.url ?? undefined}>
                  <Globe className="h-3 w-3" />
                  {domainOf(source.url)}
                </span>
                {source.published_date && <span>发布 {source.published_date}</span>}
              </>
            ) : (
              <>
                {source.rerank_score !== null && (
                  <span
                    title="CrossEncoder 重排模型对「提问 + 该问答对」的联合打分，决定最终名次"
                    className="font-medium text-brand"
                  >
                    重排得分 {(source.rerank_score * 100).toFixed(0)}%
                  </span>
                )}
                <span className={clsx(source.rerank_score === null && "font-medium text-brand")}>
                  融合得分 {(source.score * 100).toFixed(0)}%
                </span>
                {source.vector_score !== null && (
                  <span>向量 {(source.vector_score * 100).toFixed(0)}%</span>
                )}
                {source.bm25_score !== null && (
                  <span>BM25 {source.bm25_score.toFixed(2)}</span>
                )}
                {/* matched_by 只有 vector/bm25 两种值，而这是个**二值**渲染
                    （不是 vector 就一律显示「关键词」）——网页条目走这条路会显示成
                    「关键词」，一个看起来完全合理的错误来源标注。所以只给知识库条目渲染。 */}
                <span className="flex items-center gap-1">
                  {source.matched_by.map((channel) => (
                    <em
                      key={channel}
                      className="not-italic rounded border border-line px-1 py-px text-[10.5px]"
                    >
                      {channel === "vector" ? "语义" : "关键词"}
                    </em>
                  ))}
                </span>
              </>
            )}
          </div>

          {isWeb ? (
            // 网页条目的正文就是 Tavily 的 content 片段（已在后端截断），没有「原文」可展开，
            // 「展开」只会把同一段文字再显示一遍。真正的原文在站外，直接给链接。
            //
            // ⚠ 链接文字是**完整 URL**，不是「打开原文」四个字：上面那行的域名只够让人
            //   一眼看出「哪个站」，而网页结果未经知识库校验，用户要核对来源就得看到完整
            //   地址（答辩要求里「联网结果要包含 URL 来源」指的也是这个）。
            //   `break-all` 是必需的——政府站点的 URL 动辄上百字符，不换行会把卡片撑破。
            <a
              href={source.url ?? "#"}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(event) => event.stopPropagation()}
              title={source.url ?? undefined}
              className="mt-2 flex items-start gap-1 text-[12px] text-brand hover:underline"
            >
              <ExternalLink className="mt-[2px] h-3 w-3 shrink-0" />
              <span className="break-all">{source.url ?? "（无链接）"}</span>
            </a>
          ) : (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                setExpanded((v) => !v);
              }}
              className="mt-2 flex items-center gap-1 text-[12px] text-brand hover:underline"
            >
              <FileText className="h-3 w-3" />
              {expanded ? "收起原文" : "查看知识库原文"}
            </button>
          )}

          {!isWeb && expanded && (
            <p className="mt-2 whitespace-pre-wrap rounded-xl border border-line bg-canvas p-3 text-[13px] leading-relaxed text-muted">
              {source.answer}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
