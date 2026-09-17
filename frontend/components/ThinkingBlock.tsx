"use client";

import { Brain, ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";

/**
 * 「深度思考」折叠区。
 * 思考中自动展开并跟随滚动；一旦开始输出正式回答就自动收起（用户手动操作后不再自动干预）。
 */
export function ThinkingBlock({
  text,
  streaming,
  hasAnswer,
}: {
  text: string;
  streaming: boolean;
  hasAnswer: boolean;
}) {
  const [manual, setManual] = useState<boolean | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  const thinking = streaming && !hasAnswer;
  const expanded = manual ?? thinking;

  useEffect(() => {
    if (expanded && thinking && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [text, expanded, thinking]);

  return (
    <div className="mb-3 overflow-hidden rounded-2xl border border-line bg-sidebar">
      <button
        type="button"
        onClick={() => setManual(!expanded)}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left transition-colors hover:bg-hover"
      >
        <Brain
          className={clsx("h-3.5 w-3.5", thinking ? "animate-blink text-brand" : "text-muted")}
        />
        <span className="text-[13px] font-medium text-ink">
          {thinking ? "深度思考中" : "深度思考"}
        </span>
        {!thinking && (
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
        <div
          ref={bodyRef}
          className="max-h-[260px] overflow-y-auto border-t border-line px-3.5 py-3 text-[13px] leading-relaxed text-muted scroll-thin"
        >
          <p className="whitespace-pre-wrap">{text}</p>
          {thinking && (
            <span className="ml-0.5 inline-block h-[13px] w-[6px] translate-y-[2px] animate-blink rounded-sm bg-muted" />
          )}
        </div>
      )}
    </div>
  );
}
