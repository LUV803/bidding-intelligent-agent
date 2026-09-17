"use client";

import { Check, Copy, RefreshCw, Scale, ThumbsDown, ThumbsUp } from "lucide-react";
import { Fragment, useState } from "react";
import clsx from "clsx";

import { GraphResultBlock } from "./GraphResultBlock";
import { ImageResultBlock } from "./ImageResultBlock";
import { Markdown } from "./Markdown";
import { PgResultBlock } from "./PgResultBlock";
import { SourcePanel } from "./SourcePanel";
import { ThinkingBlock } from "./ThinkingBlock";
import { ToolCallBlock } from "./ToolCallBlock";
import type { Message } from "@/lib/types";

export function ChatMessage({
  message,
  isLast,
  onRegenerate,
  onFeedback,
}: {
  message: Message;
  isLast?: boolean;
  onRegenerate?: () => void;
  /**
   * 给这条回答打分（1 = 👍，-1 = 👎；**再点同一个 = 取消**）。
   *
   * 不传就**不渲染**两个拇指 —— `page.tsx` 只在有活跃对话时传（`activeConversation?.id`）。
   */
  onFeedback?: (value: 1 | -1) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [sourceOpen, setSourceOpen] = useState(false);
  const [activeSource, setActiveSource] = useState<number | null>(null);

  /** 点击正文引用角标：展开来源区并滚动定位到对应条目 */
  const handleCitation = (index: number) => {
    setSourceOpen(true);
    setActiveSource(index);
    window.setTimeout(() => {
      document
        .getElementById(`source-${message.id}-${index}`)
        ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 80);
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* 剪贴板不可用时静默失败 */
    }
  };

  if (message.role === "user") {
    return (
      // 附件挂在气泡**上方**（列方向、右对齐），与提问自己的气泡区分开：
      // 卡片是「模型读出了什么」，气泡是「我问了什么」。content 保持用户原样输入的文字
      // ——图片内容不拼进去（拼了会让气泡里出现一整段公告，且历史回传会重复拼）。
      // ⚠ `msg-<id>` 是**点搜索结果跳过来时的落点**（`page.tsx` 的定位 effect 按它
      //   `getElementById`），与下面来源面板的 `source-<id>-<n>` 同一套命名。别改名。
      <div id={`msg-${message.id}`} className="flex animate-fade-up flex-col items-end gap-2">
        {message.image && (
          <div className="w-full max-w-[85%]">
            <ImageResultBlock result={message.image} />
          </div>
        )}
        {message.content && (
          <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-bubble px-4 py-2.5 text-[15px] leading-relaxed text-ink">
            {message.content}
          </div>
        )}
      </div>
    );
  }

  return (
    <div id={`msg-${message.id}`} className="flex animate-fade-up gap-3">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand text-white">
        <Scale className="h-4 w-4" />
      </div>

      <div className="min-w-0 flex-1">
        {message.reasoning && (
          <ThinkingBlock
            text={message.reasoning}
            streaming={Boolean(message.streaming)}
            hasAnswer={Boolean(message.content)}
          />
        )}

        {message.steps && message.steps.length > 0 && <ToolCallBlock steps={message.steps} />}

        {/* 图谱/数据库结果各自独立成块：它们都不进来源面板、不参与正文的 [n] 引用编号 */}
        {message.steps?.map((step) => (
          <Fragment key={step.id}>
            {step.graph && <GraphResultBlock result={step.graph} />}
            {step.pg && <PgResultBlock result={step.pg} />}
          </Fragment>
        ))}

        {message.content ? (
          <Markdown content={message.content} onCitation={handleCitation} />
        ) : (
          // 已有工具步骤时由 ToolCallBlock 承担「正在忙」的提示，避免两个加载态打架
          !message.reasoning && !message.steps?.length && <ThinkingDots />
        )}

        {message.streaming && message.content && <Caret />}

        {message.error && (
          <p className="mt-2 rounded-xl border border-red-300/60 bg-red-50 px-3 py-2 text-[13px] text-red-600 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-400">
            {message.error}
          </p>
        )}

        {message.sources && message.sources.length > 0 && (
          <SourcePanel
            sources={message.sources}
            idPrefix={message.id}
            open={sourceOpen}
            onToggle={() => setSourceOpen((v) => !v)}
            activeIndex={activeSource}
            onSelect={setActiveSource}
          />
        )}

        {!message.streaming && message.content && (
          // ⚠ `flex-wrap gap-y-1`：原先最多三格，加上两个拇指后在 375px 上挤不下 ——
          //   不换行会被父列的 `min-w-0` 裁掉（按钮还在，只是看不见，很难发现）。
          <div className="mt-2.5 flex flex-wrap items-center gap-1 text-faint">
            <ActionButton label={copied ? "已复制" : "复制"} onClick={copy}>
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            </ActionButton>
            {isLast && onRegenerate && (
              <ActionButton label="重新生成" onClick={onRegenerate}>
                <RefreshCw className="h-3.5 w-3.5" />
              </ActionButton>
            )}
            {/*
              两个拇指**不受 `isLast` 限制**：每条答完的回答都该能评，不只是最后一条。
              `title` 按是否已选分叉 —— 「再点一次 = 取消」这个行为否则在界面上不可见。
            */}
            {onFeedback && (
              <>
                <ActionButton
                  label={message.feedback === 1 ? "已赞" : "有帮助"}
                  onClick={() => onFeedback(1)}
                  active={message.feedback === 1}
                >
                  <ThumbsUp className="h-3.5 w-3.5" />
                </ActionButton>
                <ActionButton
                  label={message.feedback === -1 ? "已踩" : "没帮助"}
                  onClick={() => onFeedback(-1)}
                  active={message.feedback === -1}
                >
                  <ThumbsDown className="h-3.5 w-3.5" />
                </ActionButton>
              </>
            )}
            {message.latency?.total_ms !== undefined && (
              <span className="ml-1 text-[11.5px] text-faint">
                耗时 {(message.latency.total_ms / 1000).toFixed(1)}s
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ActionButton({
  children,
  label,
  onClick,
  active,
}: {
  children: React.ReactNode;
  label: string;
  onClick: () => void;
  /** 选中态（目前只有两个拇指用）。亮色沿用工具条那四个胶囊的开关态约定 */
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      className={clsx(
        "flex items-center gap-1 rounded-lg px-2 py-1 text-[12px] transition-colors",
        // ⚠ 选中态要把 hover 一并覆盖掉：只写 `active && "bg-brand-soft"` 的话，
        //   鼠标一移上去就被后面的 `hover:bg-hover` 盖回灰底 —— 选中的那个反而变暗。
        active
          ? "bg-brand-soft text-brand hover:bg-brand-soft hover:text-brand"
          : "hover:bg-hover hover:text-ink",
      )}
    >
      {children}
      <span>{label}</span>
    </button>
  );
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1.5 py-1.5">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1.5 w-1.5 animate-blink rounded-full bg-brand"
          style={{ animationDelay: `${i * 0.18}s` }}
        />
      ))}
      {/* ⚠ 别写死「正在检索知识库…」：开着联网开关时，这段等待里有一大半是 Tavily 的
          往返（1~3 秒，超时上限 8 秒），写「知识库」会让用户以为它卡住了。
          「资料」两种模式下都成立（知识库条目与网页条目后端都叫【资料】）。 */}
      <span className="ml-1.5 text-[13px] text-muted">正在检索资料…</span>
    </div>
  );
}

function Caret() {
  return (
    <span
      className={clsx(
        "ml-0.5 inline-block h-[15px] w-[7px] translate-y-[2px] rounded-sm bg-brand",
        "animate-blink",
      )}
    />
  );
}
