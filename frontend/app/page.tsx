"use client";

import { Loader2, PanelLeft } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";

import { ChatInput } from "@/components/ChatInput";
import { ChatMessage } from "@/components/ChatMessage";
import { Sidebar } from "@/components/Sidebar";
import { WelcomeScreen } from "@/components/WelcomeScreen";
import { useChat } from "@/hooks/useChat";
import { fetchHealth } from "@/lib/api";
import { clearKeywordHighlight, highlightKeyword } from "@/lib/highlight";
import type { HealthInfo } from "@/lib/types";

/**
 * 「点搜索结果 → 跳到命中消息」最多等多久（毫秒）。
 *
 * 等的是**目标对话的正文**（`remote` 对话按需拉取，见 `useChat.openConversation`）。
 * 超了就放弃跳转、把那条待办清掉 —— ⚠ 清掉比一直挂着重要得多：挂着的待办会让
 * 自动滚到底永远让位（见下面 `jumpRef` 那段），症状是「新消息不再自动跟到最新」。
 * 1.5 秒远大于一次本地 PostgreSQL 往返（毫秒级），够用又不至于让人察觉卡顿。
 */
const JUMP_GIVEUP_MS = 1500;

export default function Page() {
  const chat = useChat();
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // ---------- 主题 ----------
  useEffect(() => {
    const isDark = document.documentElement.classList.contains("dark");
    setTheme(isDark ? "dark" : "light");
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    setSidebarOpen(window.innerWidth >= 1024);
    const onChange = (event: MediaQueryListEvent) => {
      if (!localStorage.getItem("bidding-qa-theme")) {
        setTheme(event.matches ? "dark" : "light");
      }
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      document.documentElement.classList.toggle("dark", next === "dark");
      localStorage.setItem("bidding-qa-theme", next);
      return next;
    });
  }, []);

  // ---------- 后端状态轮询 ----------
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const info = await fetchHealth();
        if (alive) setHealth(info);
      } catch {
        if (alive) setHealth(null);
      }
    };
    void tick();
    const timer = setInterval(tick, health?.status === "ok" ? 30000 : 4000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [health?.status]);

  // ---------- 新消息自动滚动到底部 ----------
  const messages = chat.activeConversation?.messages ?? [];
  const messageCount = messages.length;
  // 打分要往哪条对话上记。用 id 而不是把 `setFeedback` 收窄成单参 —— 后者会让
  // `ChatMessage` 也去关心「当前是哪条对话」，而它只该关心自己这一条消息。
  const activeId = chat.activeConversation?.id;
  const lastLength = messages[messageCount - 1]?.content.length ?? 0;

  // ---------- 点搜索结果：滚到命中的那条消息 + 标黄关键词 ----------
  const pendingJump = chat.pendingJump;
  const clearPendingJump = chat.clearPendingJump;
  /**
   * ⚠⚠ 这个 ref 不是缓存优化，是**给下面那条「自动滚到底」让路用的**。
   *
   * 点搜索结果时目标对话的正文可能刚进 DOM（`remote` 对话是异步拉的），于是两条 effect
   * 会在**同一次 commit** 里同时触发、都去滚同一个容器 —— 谁后跑谁赢，而结果取决于
   * effect 的声明顺序，表现就是「点了结果还是被甩到对话底部」。
   *
   * ⚠ 用 ref 而不是把 `pendingJump` 塞进那条 effect 的依赖数组：进了依赖的话，
   *   `clearPendingJump()` 会让它**再跑一次**，把视口又拽回底部 —— 前功尽弃。
   */
  const jumpRef = useRef(pendingJump);
  jumpRef.current = pendingJump;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 定位优先：命中通常在对话中间，把用户甩到底部等于这次跳转白点了
    if (jumpRef.current) return;
    // 真滚了才清上一次的高亮 —— 上面那个 return 之后的语句在跳转时**不执行**，
    // 否则会把下面定位 effect 刚标好的词抹掉
    clearKeywordHighlight();
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messageCount, lastLength]);

  // 切走之后不该还留着上一次的高亮：它标的是**另一条**对话里的词。
  // ⚠ 必须声明在下面那条定位 effect **之前** —— 两条在同一次 commit 里都跑时按声明顺序
  //   执行，反过来的话「先标黄、再被清掉」，而表现只是「跳过去了但没标黄」。
  useEffect(() => {
    clearKeywordHighlight();
  }, [activeId]);

  useEffect(() => {
    if (!pendingJump) return;
    // 跳的**不是当前这条**：用户点了结果又立刻切走。不等这一条的话，会在错误的对话里
    // 找一个同名的 id（找不到，白等一轮）。
    if (pendingJump.conversationId !== activeId) return;

    const el = document.getElementById(`msg-${pendingJump.messageId}`);
    if (!el) {
      // 消息还没进 DOM —— `remote` 对话的正文要等 `openConversation()` 拉回来，
      // 这一趟必然落空，靠 `messageCount` 变化把本 effect 再触发一次。
      // ⚠⚠ 但**不能无限等**：拉不回来时它会一直压着上面那条「自动滚到底」，
      //   表现是「之后新消息再也不自动跟到最新」—— 一个与搜索毫无关系的怪症状。
      const timer = setTimeout(clearPendingJump, JUMP_GIVEUP_MS);
      return () => clearTimeout(timer);
    }
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    // 关键词可能已经被 Markdown 拆开（`履约**保证**金`）而标不到 —— 那是这条 API 的
    // 固有代价，返回值这里用不上，**滚动本身仍然生效**（降级而非报错）。
    highlightKeyword(el, pendingJump.keyword);
    clearPendingJump();
  }, [pendingJump, clearPendingJump, activeId, messageCount]);

  const ready = health?.status === "ok";
  /**
   * 服务端到底配没配 TAVILY_API_KEY —— 给「联网搜索」按钮的 tooltip 用。
   *
   * ⚠ **三态，别收敛成 boolean**：`null` 表示健康检查还没回来（**还不知道**），
   *   此时说「未配置」是错的。只有确认为 `false` 时才提示，因为那是那条路径上
   *   唯一的反馈通道 —— 开关打开而服务端没 Key 时，不发 error、按钮不置灰，
   *   用户只会得到一次**没有网页条目**的普通回答，分不出「没搜到」和「压根没搜」。
   *   （这段提示原先是 Sidebar 底部状态块里的一行，2026-09-20 随状态块一起挪过来的。）
   */
  const webConfigured = health ? health.web_configured : null;
  /**
   * 可选的文本大模型（只含服务端**真配了 Key** 的那几家），交给工具条上那个循环胶囊。
   *
   * ⚠ 健康检查还没回来时它就是空数组，而 `ChatInput` 只在**两个以上**时才渲染胶囊
   *   —— 「还不知道」与「没得选」在这里天然是同一个分支，不需要像 `webConfigured`
   *   那样单独维护一个 `null` 三态。
   */
  const llmProviders = health?.llm_providers ?? [];

  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      {/* 侧边栏：移动端浮层，桌面端固定 */}
      {sidebarOpen && (
        <>
          <div
            className="fixed inset-0 z-30 bg-black/30 lg:hidden"
            onClick={() => setSidebarOpen(false)}
          />
          <div className="fixed inset-y-0 left-0 z-40 lg:static lg:z-auto">
            <Sidebar
              conversations={chat.conversations}
              activeId={chat.activeId}
              onNew={() => {
                chat.newConversation();
                if (window.innerWidth < 1024) setSidebarOpen(false);
              }}
              // ⚠⚠ `jump` 必须**原样往下传**。写成 `(id) => …` 漏掉它，TypeScript 不会报错
              //   （形参少一个对函数类型是合法的赋值），`tsc` 一片绿，而运行时这个参数
              //   直接被丢掉 —— 表现就是「点了搜索结果只切对话、不跳到命中那条」，
              //   且**看不出**是哪一层吃掉的。`Sidebar` 里那次 `onSelect(row.id, jump)`
              //   只有传到这里才算数。
              onSelect={(id, jump) => {
                chat.selectConversation(id, jump);
                if (window.innerWidth < 1024) setSidebarOpen(false);
              }}
              onDelete={chat.deleteConversation}
              /* 清空全部。⚠ 与 `onDelete` 的签名不同：它是 `async` 且**会抛** ——
                 侧栏的 `runClear` 靠这个 catch 把后端那句中文显示出来。见 `Sidebar` 的 prop 注释。 */
              onDeleteAll={chat.deleteAllConversations}
              /* 三态：`null` = 还没结论，侧栏一个字都不说（同 `webConfigured` 的约定）。
                 服务端不可达时它是 `false`，侧栏据此如实提示「只存在本机浏览器」——
                 ⚠ 那行提示**不是装饰**：问答链路完全不受影响，界面上再无别的信号。 */
              historySynced={chat.historySynced}
              onClose={() => setSidebarOpen(false)}
              theme={theme}
              onToggleTheme={toggleTheme}
            />
          </div>
        </>
      )}

      <main className="flex min-w-0 flex-1 flex-col">
        {/* 顶栏 */}
        <header className="flex h-14 shrink-0 items-center gap-2 border-b border-line px-4">
          {!sidebarOpen && (
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              title="展开侧边栏"
              className="rounded-lg p-2 text-muted transition-colors hover:bg-hover hover:text-ink"
            >
              <PanelLeft className="h-4 w-4" />
            </button>
          )}
          <h2 className="truncate text-[14px] font-medium text-ink">
            {chat.activeConversation?.title ?? "新对话"}
          </h2>
          <div className="ml-auto flex items-center gap-2">
            {!ready && (
              <span className="flex items-center gap-1.5 rounded-full border border-line px-2.5 py-1 text-[11.5px] text-muted">
                <span className="h-1.5 w-1.5 animate-blink rounded-full bg-amber-500" />
                {health ? "服务初始化中" : "未连接后端"}
              </span>
            )}
            {ready && (
              <span className="hidden items-center gap-1.5 rounded-full border border-line px-2.5 py-1 text-[11.5px] text-muted sm:flex">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                {health?.qdrant_mode === "cloud" ? "Qdrant 云" : "Qdrant 本地"} ·{" "}
                {health?.llm_model}
              </span>
            )}
          </div>
        </header>

        {/* 主体 */}
        {chat.activeConversation?.remote ? (
          /* 正文还在从服务端拉。⚠ **这个分支必须排在 `messageCount === 0` 前面**：
             `remote` 的对话 `messages` 本来就是空的，落到下面那个分支会渲染**欢迎页**
             （还带着示例卡片），用户点了历史却看到「开始新对话」——看起来就像记录丢了。
             当前 `selectConversation` 是「先拉完再切」，所以这里正常是兜底；
             它也顺手挡住了「删掉当前对话、下一条来自服务端」那几十毫秒的空窗。 */
          <div className="flex min-h-0 flex-1 items-center justify-center">
            <span className="flex items-center gap-2 text-[13px] text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              正在加载历史对话…
            </span>
          </div>
        ) : messageCount === 0 ? (
          <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
            <WelcomeScreen
              onSend={chat.send}
              onStop={chat.stop}
              streaming={chat.streaming}
              disabled={!ready}
              graphAvailable={health?.graph_available === true}
              dbAvailable={health?.pg_available === true}
              hybrid={chat.hybrid}
              onToggleHybrid={chat.setHybrid}
              rerank={chat.rerank}
              onToggleRerank={chat.setRerank}
              webSearch={chat.webSearch}
              onToggleWebSearch={chat.setWebSearch}
              webConfigured={webConfigured}
              llmProvider={chat.llmProvider}
              llmDefaultProvider={health?.llm_provider ?? ""}
              llmProviders={llmProviders}
              onSelectLlmProvider={chat.setLlmProvider}
            />
          </div>
        ) : (
          <>
            <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto scroll-thin">
              <div className="mx-auto w-full max-w-chat space-y-6 px-4 py-6">
                {messages.map((message, index) => (
                  <ChatMessage
                    key={message.id}
                    message={message}
                    isLast={index === messageCount - 1}
                    onRegenerate={chat.regenerate}
                    // 传 `undefined` 时 ChatMessage 不渲染两个拇指（没活跃对话就没有可评的对象）
                    onFeedback={
                      activeId
                        ? (value) => chat.setFeedback(activeId, message.id, value)
                        : undefined
                    }
                  />
                ))}
              </div>
            </div>
            <div className="shrink-0 px-4 pb-4">
              <div className="mx-auto w-full max-w-chat">
                <ChatInput
                  onSend={chat.send}
                  onStop={chat.stop}
                  streaming={chat.streaming}
                  disabled={!ready}
                  hybrid={chat.hybrid}
                  onToggleHybrid={chat.setHybrid}
                  rerank={chat.rerank}
                  onToggleRerank={chat.setRerank}
                  webSearch={chat.webSearch}
                  onToggleWebSearch={chat.setWebSearch}
                  webConfigured={webConfigured}
                  llmProvider={chat.llmProvider}
                  llmDefaultProvider={health?.llm_provider ?? ""}
                  llmProviders={llmProviders}
                  onSelectLlmProvider={chat.setLlmProvider}
                />
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
