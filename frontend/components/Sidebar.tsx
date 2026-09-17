"use client";

import {
  CloudOff,
  Loader2,
  MessageSquare,
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
  Scale,
  Search,
  Sun,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import clsx from "clsx";

import { searchConversations } from "@/lib/api";
import type { Conversation, ConversationHit, SearchJump } from "@/lib/types";

/** 搜索防抖（毫秒）。太短会在中文输入法组合期间打出一串请求，太长则显得迟钝 */
const SEARCH_DEBOUNCE_MS = 250;

/**
 * 行尾「⋯」菜单的盒子尺寸（px）。
 *
 * ⚠ 菜单是 `position: fixed` + 按按钮的 `getBoundingClientRect()` 算坐标，**不是**
 *   `absolute` 挂在行上 —— 行在一个 `overflow-y-auto` 容器里，`absolute` 会被容器裁掉
 *   下半截（列表滚到底时最后一行正好贴着下沿，那个菜单会**只露出一半**）。
 *   两轴都要：`MENU_H` 用来判断「下面放不下就向上翻」，`MENU_W` 用来让菜单**右对齐**按钮。
 *   数值改了菜单就要跟着改样式（`py-1` + 一项 `py-1.5` 的一行 ≈ 38）。
 */
const MENU_W = 104;
const MENU_H = 38;
/** 菜单与按钮之间的间距，也是距视口边缘的最小留白 */
const MENU_GAP = 4;

/**
 * 本机兜底搜索：**标题 或 已加载的正文**命中。
 *
 * ⚠ 正文也要搜。只按标题过滤是个**静默的错误答案**——用户搜一个只出现在回答里的词，
 *   拿到「没有结果」，而他分不出这是「真的没有」还是「降级版本搜不到」。
 * ⚠ 局限如实说：`remote` 的对话正文还没拉下来（`messages: []`），只能按标题匹配 ——
 *   所以降级时列表顶上会有一行「仅在本机记录中查找」。
 */
function localSearch(conversations: Conversation[], query: string): ConversationHit[] {
  const needle = query.toLowerCase();
  const hits: ConversationHit[] = [];
  for (const conv of conversations) {
    const message = conv.messages.find((m) => m.content.toLowerCase().includes(needle));
    if (!message && !conv.title.toLowerCase().includes(needle)) continue;
    hits.push({
      id: conv.id,
      title: conv.title,
      updatedAt: conv.updatedAt,
      snippet: message ? snippetOf(message.content, needle) : "",
      snippetRole: message?.role ?? "",
      // 降级路径也要给，否则「服务端搜不到时点结果不跳转」——两条路行为不一致，
      // 而用户完全看不出自己走的是哪条（降级提示只有一行小字）。
      hitMessageId: message?.id ?? "",
    });
  }
  return hits;
}

/** 取命中位置附近的片段（服务端切好的那份也是这个形状）。命中不在正文里时给开头一段 */
function snippetOf(text: string, lowerNeedle: string, pad = 40): string {
  const flat = text.replace(/\s+/g, " ").trim();
  const at = flat.toLowerCase().indexOf(lowerNeedle);
  if (at < 0) return flat.slice(0, pad * 2);
  const start = Math.max(0, at - pad);
  const end = Math.min(flat.length, at + lowerNeedle.length + pad);
  return `${start > 0 ? "…" : ""}${flat.slice(start, end)}${end < flat.length ? "…" : ""}`;
}

/**
 * ⚠ 这里原先有一整块 `StatusLine`（知识库就绪 / 向量·BM25 计数 / 重排模型 / 大模型 /
 * 知识图谱 / 采购记录库 / 图片解析 / 联网搜索 共 7 行），2026-09-20 按要求整块移除 ——
 * 那是**面向开发者的运维信息**，不是给提问的人看的。
 *
 * 移除时只丢了一样有功能价值的东西：**联网搜索那行是「开关打开但服务端没配
 * TAVILY_API_KEY」的唯一反馈通道**（那条路径上不发 error、按钮不置灰，正文里也分不出
 * 「没搜到」和「压根没搜」）。这句提示已挪到 `ChatInput` 里「联网搜索」按钮的 tooltip 上
 * （`webConfigured === false` 时才追加），**别再往侧栏加回去**。
 *
 * ⚠ 后端健康检查本身没有随这块一起删，`page.tsx` 还在拿它做别的事：顶栏那颗
 * 「服务初始化中／未连接后端」胶囊、发送按钮的 `disabled`、首屏图谱/数据库示例卡片的
 * 置灰、以及上面那个联网 tooltip。
 *
 * ⚠ 与那块**无关**的是下面这行「未同步」提示：它来自 `useChat.historySynced`，
 * 说的是「对话历史存不进数据库」（**问答链路完全不受影响**，界面上再无别的信号），
 * 而状态块说的是后端各项能力。别因为它长得像就把两者合并 —— 判据完全不同。
 */
export function Sidebar({
  conversations,
  activeId,
  historySynced,
  onNew,
  onSelect,
  onDelete,
  onDeleteAll,
  onClose,
  theme,
  onToggleTheme,
}: {
  conversations: Conversation[];
  activeId: string | null;
  /**
   * 历史有没有真的存进数据库。**三态**：`null` = 启动中、还没结论 ——
   * 那时**一个字都不该说**（同 `page.tsx` 里 `webConfigured` 的约定）。
   */
  historySynced: boolean | null;
  onNew: () => void;
  /**
   * 切到某条对话。`jump` 只在**点搜索结果、且命中落在正文里**时给 ——
   * 用来滚到那条消息并高亮关键词（消费方是 `page.tsx` 的定位 effect）。
   */
  onSelect: (id: string, jump?: SearchJump) => void;
  onDelete: (id: string) => void;
  /**
   * 清空**全部**对话。
   *
   * ⚠⚠ 与 `onDelete` 的签名**形状不同**，别照抄成同步的：它是 `async` 且**会抛**。
   *   `useChat.deleteAllConversations` 的策略是「**服务端删成功才清本地**」（与单条删除的
   *   乐观路径刻意相反 —— 服务端那份没删掉的话，下次启动并集合并会让整个列表复活）。
   *   所以调用点必须 `await` + `catch`，把后端那句中文显示出来。
   */
  onDeleteAll: () => Promise<void>;
  onClose: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}) {
  const [query, setQuery] = useState("");
  /** 搜索结果；`null` = 没在搜索，显示完整列表。搜索结果**原地替换**列表 */
  const [hits, setHits] = useState<ConversationHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  /** 服务端搜索失败、已退回本机匹配 */
  const [localFallback, setLocalFallback] = useState(false);
  /**
   * 输入法**正在组合**（拼音还没选字）。
   *
   * ⚠ 这个标志不可省。中文输入法组合期间每次按键都会触发 `onChange`，值是 `gong`、
   *   `gong'ying` 这样的**拼音中间态**——不挡住的话每敲一个字母就打一次服务端搜索，
   *   而那个词 99% 命中 0 条 → 侧栏当场翻成「没有找到匹配的对话。」，
   *   用户看到的是一块**在打字过程中自己闪空**的列表（实测后端日志里就有 `q=gong`、
   *   `q=gong'ying` 两条真实请求）。事后还留着残值：只要搜索框非空，列表就一直是
   *   搜索结果的快照，**之后新问的对话一条都不会出现**，看起来就像「历史对话永远空的」。
   *   `compositionend` 之后 effect 会带着**最终选定的那个词**再跑一次。
   */
  const [composing, setComposing] = useState(false);
  /**
   * 展开中的「⋯」菜单：**同一时刻只开一个**，所以是一份组件级状态而不是每行一个。
   *
   * `top` / `left` 在打开那一刻由按钮的 `getBoundingClientRect()` 算出来（见 `MENU_W` 那段），
   * 之后不再跟随 —— 滚动、缩放、点别处都会把它关掉，不会出现一个漂在原地的孤儿菜单。
   */
  const [menu, setMenu] = useState<{ id: string; top: number; left: number } | null>(null);
  /**
   * 「清空全部」的原地二次确认。`null` = 没在确认（那一行显示「清空」按钮）。
   *
   * ⚠ 合成**一个对象**而不是三个 boolean：`busy` 与 `error` 不可能同时成立，
   *   拆开就得靠「不可能状态」活着 —— 那是 bug 温床。
   * ⚠ 刻意**不引模态框**：仓库里 `Modal` / `Dialog` / `window.confirm` 命中数为 0，
   *   为一次确认引入一套浮层组件（z-index、焦点陷阱、Esc、点外部关闭…）不划算。
   *   也刻意**不加 Esc 关闭**：仓库里 Esc 只服务于「⋯」浮层，那里没有取消按钮；
   *   这里按钮齐了，再加一条键盘通路只会多一个状态源。
   */
  const [clear, setClear] = useState<
    { phase: "confirm" } | { phase: "busy" } | { phase: "error"; message: string } | null
  >(null);

  // ⚠ 本机兜底要用 `conversations`，但它**不能进下面那个 effect 的依赖数组**：
  //   `patchMessage` 每个 SSE 分片都会换掉这个数组，流式输出时一轮就是几百上千次 ——
  //   进了依赖数组，防抖计时器会被反复重置，**正在输入的那次搜索永远排不上号**。
  //   用 ref 读最新值即可（与 useChat 里 activeIdRef 是同一个成例）。
  const conversationsRef = useRef(conversations);
  conversationsRef.current = conversations;

  useEffect(() => {
    const needle = query.trim();
    if (!needle) {
      setHits(null);
      setSearching(false);
      setLocalFallback(false);
      return;
    }

    // ⚠ 组合期间**一个请求都不发**，也**不动 `hits`**：`gong` 这种拼音中间态搜出来的是
    //   0 条，会让列表在用户打字过程中闪空（而他要搜的词还没敲完）。
    //   保持现状即可 —— `compositionend` 会把这个 effect 再触发一次，那时才是真正的查询词。
    //   ⚠ 别在这里 `setHits(null)`「恢复完整列表」：那会让列表在每次组合开始时整块闪一下。
    if (composing) {
      setSearching(false);
      return;
    }

    // 请求回来时如果输入框已经变了，这次结果作废（否则打字快时会出现「结果对不上输入」）
    let alive = true;
    setSearching(true);
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const rows = await searchConversations(needle);
          if (!alive) return;
          setHits(rows);
          setLocalFallback(false);
        } catch {
          if (!alive) return;
          // 服务端搜索不可用（数据库连不上 / 超时）→ **降级到本机那份**，
          // 与全项目一致的「降级而非报错」。数据就在 props 里，一行都不用等网络。
          setHits(localSearch(conversationsRef.current, needle));
          setLocalFallback(true);
        } finally {
          if (alive) setSearching(false);
        }
      })();
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      alive = false;
      clearTimeout(timer);
    };
    // ⚠ `composing` 必须在依赖里：组合结束时靠它把 effect 再触发一次，
    //   带着用户**最终选定的那个词**去搜。`conversations` 仍然**不能**放进来（见上）。
  }, [query, composing]);

  const clearSearch = () => setQuery("");

  /**
   * 打开 / 收起某一行的「⋯」菜单，并把坐标算好（向上翻、右对齐、夹在视口内）。
   */
  const openMenu = (event: ReactMouseEvent<HTMLButtonElement>, id: string) => {
    // ⚠ 这两件事都靠它：
    //   ① 这一行本身挂着「点一下切过去」的 onClick —— 不挡住的话，点「⋯」会**顺带切到
    //      那条对话**（正要去删它，结果先跳过去）；
    //   ② 下面那个「点别处关闭」的 document 监听 —— 原生的 click 会一路冒泡到 document，
    //      而 effect 注册监听发生在这次事件之后。不挡的话菜单会**开了就立刻被自己关掉**。
    event.stopPropagation();
    if (menu?.id === id) {
      setMenu(null); // 再点一次收起（同一个按钮的切换语义）
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const below = rect.bottom + MENU_GAP;
    setMenu({
      id,
      // 下方放不下就向上翻 —— 列表滚到底时，最后一行的按钮是贴着视口下沿的
      top: below + MENU_H > window.innerHeight - MENU_GAP ? rect.top - MENU_GAP - MENU_H : below,
      // 右对齐按钮，同时不许越过屏幕左右边缘
      left: Math.max(
        MENU_GAP,
        Math.min(rect.right - MENU_W, window.innerWidth - MENU_W - MENU_GAP),
      ),
    });
  };

  // 点别处 / Esc / 滚动 / 缩放都关掉菜单。
  // ⚠ 少了这个 effect，菜单就是个**关不掉的浮层**，而且它盖住的是列表本身 —— 用户会
  //   以为侧栏卡死了。四个来源缺一不可：列表是在内层容器里滚的，冒泡阶段收不到那个
  //   scroll 事件，所以那个必须用 capture 注册。
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenu(null);
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [menu]);

  const removing = (id: string) => {
    onDelete(id);
    // 搜索结果是一份独立的快照，本地列表删了它不会跟着变 —— 不在这儿摘掉的话，
    // 那条已经删掉的对话会**留在搜索结果里**，点它还什么都打不开。
    setHits((prev) => (prev ? prev.filter((hit) => hit.id !== id) : prev));
  };

  /**
   * 确认清空 → 调 `onDeleteAll`。⚠ **服务端删成功才会走到下面那几行**：
   * `onDeleteAll` 出错时本地一个字都没动（列表、`hits`、activeId 全部原样），
   * 所以 catch 里也**不能**去清 `hits` —— 那会把「原来那个世界」的列表提前抹掉。
   */
  const runClear = async () => {
    setClear({ phase: "busy" });
    try {
      await onDeleteAll();
      // ⚠ 搜索态那份 `hits` 是**独立快照**，必须四处一起清。现成的 `clearSearch()`
      //   **只是 `setQuery("")`**，不够 —— 那样还得让上面那个 effect 转一圈
      //   （`needle` 为空才 `setHits(null)`），中间有一帧渲染的是「旧 hits + 空 query」
      //   = 一屏点不动的幽灵行。
      setQuery("");
      setHits(null);
      setSearching(false);
      setLocalFallback(false);
      setClear(null);
    } catch (error) {
      // 就地显示，**不挪到侧栏底部**：失败时列表一条没动，用户的视线就在这块上；
      // 支开他去找提示，而「重试」按钮只有这一处有意义。
      // 兜底文案与 `useChat.run()` 的 catch 逐字一致。
      setClear({
        phase: "error",
        message:
          error instanceof Error ? error.message : "请求失败，请检查后端服务是否已启动",
      });
    }
  };

  const rows: ConversationHit[] =
    hits ??
    conversations.map((conv) => ({
      id: conv.id,
      title: conv.title,
      updatedAt: conv.updatedAt,
      snippet: "",
      snippetRole: "",
      hitMessageId: "",
    }));

  return (
    <aside className="flex h-full w-[260px] shrink-0 flex-col border-r border-line bg-sidebar">
      {/* 品牌区 */}
      <div className="flex items-center gap-2 px-4 py-4">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand text-white">
          <Scale className="h-4 w-4" />
        </div>
        <span className="text-[15px] font-semibold tracking-tight text-ink">
          招投标智能问答
        </span>
        <button
          type="button"
          onClick={onClose}
          title="收起侧边栏"
          className="ml-auto rounded-lg p-1.5 text-muted transition-colors hover:bg-hover hover:text-ink"
        >
          <PanelLeftClose className="h-4 w-4" />
        </button>
      </div>

      {/* 新建对话 */}
      <div className="px-3">
        <button
          type="button"
          onClick={onNew}
          className="flex w-full items-center gap-2 rounded-xl border border-line bg-surface px-3 py-2.5 text-[13.5px] font-medium text-ink shadow-sm transition-colors hover:border-brand/40 hover:bg-brand-soft hover:text-brand"
        >
          <Plus className="h-4 w-4" />
          开启新对话
        </button>
      </div>

      {/* 会话列表 */}
      <div className="mt-5 min-h-0 flex-1 overflow-y-auto px-3 pb-3 scroll-thin">
        <div className="flex items-center justify-between px-2 pb-1.5">
          <p className="text-[11.5px] font-medium text-faint">历史对话</p>
          {/* ⚠ 只在**有东西可删**时渲染：一条都没有还摆个「清空」，点了要么无事发生、
              要么在服务端不可达时报一句关于「删除」的错，两种都莫名其妙。
              ⚠ 计数用 `conversations.length` 而不是 `rows.length` —— 后者是**搜索结果**
              的长度（搜到一个词时它只有几条），拿它算「全部」会给出一个**看起来完全
              合理的错误数字**。 */}
          {conversations.length > 0 && clear === null && (
            <button
              type="button"
              onClick={() => setClear({ phase: "confirm" })}
              className="rounded px-1 text-[11.5px] text-faint transition-colors hover:bg-hover hover:text-red-500"
            >
              清空
            </button>
          )}
        </div>

        {/* 原地二次确认 / 失败重试。⚠ 位置就在「清空」按钮正下方 —— 用户刚点的地方。
            ⚠ 刻意**不做 `sticky`**：这块在一个 `overflow-y-auto` 容器里，半透明的红底
              会压着列表行透出来。 */}
        {clear !== null && (
          <div className="mx-2 mb-2 rounded-lg bg-red-500/10 px-2 py-1.5 text-[11.5px] leading-relaxed text-red-600 dark:text-red-400">
            {clear.phase === "error" ? (
              <>
                <p>删除失败：{clear.message}</p>
                <p className="mt-1 text-red-600/80 dark:text-red-400/80">
                  本地对话一条都没动，可以重试。
                </p>
              </>
            ) : (
              <p>确定删除全部 {conversations.length} 条对话？删除后无法恢复。</p>
            )}
            <div className="mt-1.5 flex items-center gap-2">
              {/* ⚠ busy 时两个按钮都要 disable：双击「删除」会发两发 DELETE，而两发之间
                  有一次「解冻 → 解冻后补发 PUT」的窗口，第二发可能夹在那中间。 */}
              <button
                type="button"
                onClick={() => void runClear()}
                disabled={clear.phase === "busy"}
                className="flex items-center gap-1 rounded bg-red-500 px-2 py-0.5 font-medium text-white transition-colors hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {clear.phase === "busy" && <Loader2 className="h-3 w-3 animate-spin" />}
                {clear.phase === "error" ? "重试" : "删除"}
              </button>
              <button
                type="button"
                onClick={() => setClear(null)}
                disabled={clear.phase === "busy"}
                className="rounded px-1.5 py-0.5 text-red-600/80 transition-colors hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-60 dark:text-red-400/80"
              >
                取消
              </button>
            </div>
          </div>
        )}

        {/* ⚠ 这行提示**不是装饰**：数据库连不上时问答链路完全不受影响，界面上再无别的
            信号，用户会以为历史已经存进去了。`null`（启动中）时不显示 —— 那时说
            「未同步」是错的，与 `webConfigured` 是同一条三态约定。 */}
        {historySynced === false && (
          <p className="mx-2 mb-2 flex items-start gap-1.5 rounded-lg bg-amber-500/10 px-2 py-1.5 text-[11.5px] leading-relaxed text-amber-600 dark:text-amber-400">
            <CloudOff className="mt-[2px] h-3.5 w-3.5 shrink-0" />
            未同步到数据库，历史只保存在本机浏览器。
          </p>
        )}

        {/* 搜索框。样式沿用现有约定（`border-line bg-surface focus:border-brand/60`）
            ⚠ 别加 `focus:ring-*` —— 见 ChatInput 那段注释：项目里的阴影是任意值
            `shadow-[…]`，与 Tailwind 的 ring 合成变量互斥，写了会**静默不生效**。 */}
        <div className="relative mb-1.5">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            // ⚠ 中文输入法组合期不搜（理由见 `composing` 那段）。
            //   两个事件都要挂：`compositionend` 只负责把标志翻回来，
            //   真正的查询由上面那个 effect 在依赖变化时发出。
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={() => setComposing(false)}
            placeholder="搜索历史对话…"
            className="w-full rounded-lg border border-line bg-surface py-1.5 pl-8 pr-8 text-[12.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-brand/60"
          />
          <div className="absolute right-2 top-1/2 -translate-y-1/2">
            {searching ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-faint" />
            ) : query ? (
              <button
                type="button"
                onClick={clearSearch}
                title="清空搜索"
                className="rounded p-0.5 text-faint transition-colors hover:text-ink"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            ) : null}
          </div>
        </div>

        {localFallback && (
          <p className="px-2 pb-1.5 text-[11px] leading-relaxed text-faint">
            搜索服务不可用，仅在本机记录中查找。
          </p>
        )}

        {rows.length === 0 && (
          <p className="px-2 py-3 text-[12.5px] leading-relaxed text-faint">
            {hits ? "没有找到匹配的对话。" : "暂无历史对话，开始提问后会自动保存。"}
          </p>
        )}

        <ul className="space-y-0.5">
          {rows.map((row) => (
            <li key={row.id}>
              <div
                onClick={() => {
                  // ⚠ 关键词必须在**这一行**读 `query`，不能等 `clearSearch()` 之后 ——
                  //   搜索态一清，高亮该标哪个词就没处可查了。
                  //   标题命中（`hitMessageId` 为空）时**不给 jump**：没有可跳的位置，
                  //   按老行为切过去即可（见 `ConversationHit.hitMessageId`）。
                  onSelect(
                    row.id,
                    row.hitMessageId
                      ? { messageId: row.hitMessageId, keyword: query.trim() }
                      : undefined,
                  );
                  // 点完结果就退出搜索态：留在搜索结果里会让人以为聊天记录只剩这几条
                  clearSearch();
                }}
                className={clsx(
                  "group flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-[13px] transition-colors",
                  row.id === activeId
                    ? "bg-brand-soft text-brand"
                    : "text-muted hover:bg-hover hover:text-ink",
                )}
              >
                <MessageSquare className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{row.title}</span>
                {/* 行尾「⋯」。⚠ 这里原先是个**一点就删**的垃圾桶图标，2026-09-20 换成
                    两点式的菜单（点「⋯」→ 点「删除」）—— 破坏性操作不该藏在悬停里一击生效。 */}
                <button
                  type="button"
                  title="更多操作"
                  aria-haspopup="menu"
                  aria-expanded={menu?.id === row.id}
                  onClick={(event) => openMenu(event, row.id)}
                  className={clsx(
                    "ml-auto shrink-0 rounded p-1 text-faint transition-colors hover:bg-hover hover:text-ink",
                    // 平时隐形、悬停本行才露出；菜单开着的那一行**一直可见** ——
                    // 鼠标移到菜单上时已经离开这一行，否则按钮会在菜单还开着时先消失。
                    // ⚠ 用 `invisible` 而不是 `hidden`：后者是从布局里摘掉，悬停时整行文字会抖一下。
                    menu?.id === row.id ? "visible" : "invisible group-hover:visible",
                  )}
                >
                  <MoreHorizontal className="h-3.5 w-3.5" />
                </button>
              </div>
              {/* 命中片段。`pl-8` 是为了与上面的标题对齐（行内边距 10px + 图标 14px + 间距 8px） */}
              {row.snippet && (
                <p className="line-clamp-2 pl-8 pr-2 pb-1 text-[11.5px] leading-relaxed text-faint">
                  {row.snippetRole === "user" ? "问：" : row.snippetRole ? "答：" : ""}
                  {row.snippet}
                </p>
              )}
            </li>
          ))}
        </ul>
      </div>

      {/* 底部：主题 */}
      <div className="border-t border-line px-4 py-3">
        <button
          type="button"
          onClick={onToggleTheme}
          className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-[13px] text-muted transition-colors hover:bg-hover hover:text-ink"
        >
          {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          {theme === "dark" ? "切换浅色模式" : "切换深色模式"}
        </button>
      </div>

      {/* 「⋯」菜单本体。⚠ 挂在 `aside` 下、用 `position: fixed`（理由见 `MENU_W` 那段）：
          既不会被上面那个 `overflow-y-auto` 的列表容器裁掉下半截，也**不在**那一行的 DOM 里
          —— 否则点「删除」会一路冒泡到行的「切过去」onClick 上，删完还跳过去一次。 */}
      {menu && (
        <div
          role="menu"
          style={{ top: menu.top, left: menu.left, width: MENU_W }}
          className="fixed z-50 overflow-hidden rounded-lg border border-line bg-surface py-1 shadow-lg"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              // 顺序无所谓（两个都是 setState）：`removing` 会把这条从搜索结果快照里也摘掉，
              // 不摘的话它**留在搜索结果里，点它还什么都打不开**。
              removing(menu.id);
              setMenu(null);
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-ink transition-colors hover:bg-red-500/10 hover:text-red-500"
          >
            <Trash2 className="h-3.5 w-3.5" />
            删除
          </button>
        </div>
      )}
    </aside>
  );
}
