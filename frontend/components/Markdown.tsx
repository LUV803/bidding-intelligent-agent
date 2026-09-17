"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { repairCrammedTables } from "../lib/markdown-tables";

/**
 * 把正文中的 [1] [2] 引用标记转换成可点击的角标。
 * 借助 markdown 链接语法中转，避免直接操作 AST：
 *   [1] -> [1](#cite-1)
 * 已在 markdown 链接里出现的 [text](url) 不会被误伤（负向先行断言 (?!\()）。
 */
const CITATION_RE = /\[(\d{1,2})\](?!\()/g;

export function Markdown({
  content,
  onCitation,
}: {
  content: string;
  onCitation?: (index: number) => void;
}) {
  const processed = repairCrammedTables(content.replace(CITATION_RE, "[$1](#cite-$1)"));

  return (
    <div className="md-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ href, children, ...props }) {
            const match = /^#cite-(\d+)$/.exec(href ?? "");
            if (match) {
              const index = Number(match[1]);
              return (
                <button
                  type="button"
                  onClick={() => onCitation?.(index)}
                  title={`查看来源 ${index}`}
                  className="mx-[3px] inline-flex h-[19px] min-w-[19px] translate-y-[-2px] items-center justify-center rounded-md bg-brand-soft px-1.5 text-[11px] font-semibold text-brand transition-colors hover:bg-brand hover:text-white"
                >
                  {index}
                </button>
              );
            }
            return (
              <a href={href} target="_blank" rel="noreferrer" {...props}>
                {children}
              </a>
            );
          },
          table({ children }) {
            return (
              <div className="overflow-x-auto">
                <table>{children}</table>
              </div>
            );
          },
        }}
      >
        {processed}
      </ReactMarkdown>
    </div>
  );
}
