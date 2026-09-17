"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  deleteAllConversationsRemote,
  deleteConversationRemote,
  deleteFeedback,
  getConversation,
  listConversations,
  putConversation,
  putFeedback,
  streamChat,
} from "@/lib/api";
import { imageContextText, withImageContext } from "@/lib/types";
import type { Conversation, Message, SearchJump, SendOverrides } from "@/lib/types";

/**
 * 待办的定位：跳到**哪条对话**的哪条消息、高亮什么词。
 *
 * 比 `SearchJump` 多一个 `conversationId` —— 消费者（`page.tsx`）得先确认
 * 跳的是当前这条，才不会在用户又切走之后把视口拽回去。
 */
type PendingJump = SearchJump & { conversationId: string };

const STORAGE_KEY = "bidding-qa-conversations";
const MAX_CONVERSATIONS = 50;
/** 送入模型的历史轮数（与后端 HISTORY_ROUNDS 保持一致） */
const HISTORY_ROUNDS = 3;

function uid() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function createConversation(): Conversation {
  const now = Date.now();
  return { id: uid(), title: "新对话", messages: [], createdAt: now, updatedAt: now };
}

function deriveTitle(question: string) {
  const clean = question.replace(/\s+/g, " ").trim();
  return clean.length > 22 ? `${clean.slice(0, 22)}…` : clean || "新对话";
}

/**
 * 把服务端列表并进本地列表：**只增不删**，两边都有的**以本地那份为准**。
 *
 * 启动时用一次，此后不再调用。所以次序是：**刷新后按最近活跃排**（服务端的
 * `updated_at DESC`，本地只在一边的按各自的 `updatedAt` 参与排序），
 * **本次会话内顺序稳定**（新建的照旧前插）。
 * ⚠ 会话进行中**不做客户端排序**：`patchMessage` 每秒会把 `updatedAt` 撞几十次，
 *   渲染期排序会让列表在流式输出时抖动。
 *
 * ⚠ 必须是**纯函数**：它作为 `setConversations` 的 updater 传入，而 `reactStrictMode`
 *   下 updater 会被调用两次（重复调用幂等：服务端已有的不会被追加第二次、排序稳定）。
 *
 * ⚠ 服务端更新（另一个浏览器写过）的那种情况**刻意不处理**。单用户共享模型下
 *   「不做并发协调」是既定取舍；而「以服务端为准」得先把正文拉回来，在拉回来之前
 *   本地这份就先被 `remote` 的空数组顶掉了 —— 风险比收益大得多。
 */
function mergeServerList(local: Conversation[], serverList: Conversation[]): Conversation[] {
  const seen = new Set(local.map((c) => c.id));
  const merged = [...local, ...serverList.filter((c) => !seen.has(c.id))];
  return merged.sort((a, b) => b.updatedAt - a.updatedAt);
}

export function useChat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [hybrid, setHybrid] = useState(true);
  // 与后端 RERANK_ENABLED 的默认值（false）有意不同：前端是给人看的，
  // 默认开启才能直接看到重排得分与位次升降；后端默认关，是避免 API 调用方
  // 在不知情的情况下多付几秒重排开销。前端每次请求都会显式带上这个开关。
  const [rerank, setRerank] = useState(true);
  // ⚠ 这里原先是 `const [agent, setAgent] = useState(false)`（「Agent 模式」开关）。
  //   2026-09-20 按要求去掉了切换按钮，**整条链路固定走 `/api/agent`**：
  //   state、setter、localStorage 三样一起删了，端点选择现在硬编码在 `lib/api.ts`。
  //
  // ⚠ **别把 `bidding-qa-agent` 那个 localStorage 键读回来**：老用户在开关关着的时候
  //   被写进去过 `false`，读回来会把他**永久钉在「直接 RAG」上**——而开关已经没了，
  //   他再也拨不回来，且界面上看不出任何异常。那个键现在没人读也没人写，让它烂着。
  //
  // 代价（用户知情）：每轮问答都多若干秒模型往返，比 /api/chat 那条写死流水线慢。
  // ⚠ `/api/chat` 本身没有删，后端两个端点都还在；只是**这个界面上再也打不到它了**。
  // 联网搜索：开了就**每轮都查**——后端先检索本地知识库，再用原问题搜一次 Tavily，
  // 两份结果一起进【资料】作答（`/api/chat` 与 `/api/agent` 都生效）。
  // 默认关的理由有两层：① **每次调用花一个 credit**，而且本地答得出时也照花；
  // ② 网页是本系统的**不可信输入**，要用户明确点头才引入。
  // ⚠ 开关关着时后端行为与不传这个字段逐字节相同（工具不注册、提示词不提、也不联网）。
  // ⚠ 服务端没配 TAVILY_API_KEY 时开关点了也无效（工具不注册、提示词不提），
  //   这一点靠 `ChatInput` 里联网按钮的 tooltip 如实说明（`webConfigured === false`
  //   时才追加那句）——别在这里按 health 置灰，同 vision 的先例。
  //
  // ⚠⚠ **这个开关刻意不做 localStorage 持久化**（2026-09-20 改，与另两个开关不同，别「统一」回去）。
  //   它是四个开关里**唯一花钱**的那个：每次提问 1 个 Tavily credit，免费档 1000 次就用完。
  //   持久化会让它变成粘性的——拨开一次忘了关，之后每次提问都静默扣额度，而界面上
  //   看不出任何异常（本地答得出也照搜）。所以每次打开页面都从「关」开始，要联网就当场拨。
  //   ⚠ **别把 `bidding-qa-web-search` 那个键读回来**：用户机器上可能躺着 `true`（旧版本
  //   写进去的），读回来等于把上面那条粘性开启又请回来，而且他再也关不掉了（关掉后刷新又开）。
  //   那个键现在没人读也没人写，让它烂着——与 `bidding-qa-agent` 是同一条处置。
  const [webSearch, setWebSearch] = useState(false);
  // 这次提问用哪家文本大模型。**空串 = 服务端默认**（`.env` 的 `LLM_PROVIDER`），
  // 此时请求体里带的空串会被后端当作「不指定」，全链路与加这个字段之前逐字相同。
  //
  // 可选值**不写死在前端** —— 取自 `/api/health` 的 `llm_providers`（后端只列真配了
  // Key 的那几家）。所以这台机器上只有一把 Key 时，那个胶囊根本不渲染。
  //
  // ⚠ 与 `webSearch` 的持久化处置**相反**：这个**要存**（`bidding-qa-llm-provider`）。
  //   联网那个不存的理由是「它是唯一花钱的开关，粘性开启会静默扣额度」；而换个模型
  //   不构成那种陷阱 —— 每次提问本来就要花一次模型调用，不存在「多花的钱看不出来」。
  //   不存的话用户切到 GLM 后刷新一下又变回 DeepSeek，他多半会以为是 bug。
  const [llmProvider, setLlmProvider] = useState("");
  const [hydrated, setHydrated] = useState(false);

  // 对话历史是否同步到服务端（PostgreSQL）。**两个值分工不同，别合并**：
  //
  // - `serverMode`：**要不要同步**。只在启动那次 `listConversations()` 成功后置 true，
  //   此后**不再翻回 false**。它守的是「同步写入器」与 `remote` 的处理——一次 PUT 失败
  //   （服务端抖一下）不该让整个会话从此不再保存；失败的那条会一直留在脏集合里，
  //   下一轮提交重试，而真正不可达时 `conv_store` 有 30 秒冷却，重试是廉价的。
  // - `historySynced`：**对用户如实报告的状态**，三态。除了启动那次列表调用，
  //   PUT 的成败也会翻它 —— 让「存不进数据库了」这件事在下一次刷新之前就看得见。
  //
  // ⚠ `historySynced` 是**三态**（`null` = 还没结论），与 `webConfigured` 同一条约定：
  //   `null` 时**一个字都不该说**。收成 boolean 会让侧栏在启动的头几十毫秒谎报「未同步」。
  const [serverMode, setServerMode] = useState(false);
  const [historySynced, setHistorySynced] = useState<boolean | null>(null);
  /**
   * 「点搜索结果 → 滚到命中那条消息并高亮关键词」的待办。
   *
   * ⚠ 它是个**跨异步边界的信使**，不是即时动作：点结果时目标对话可能还没拉正文
   *   （`remote`，见 `openConversation`），消息要过一会儿才进 DOM。
   *   所以这里只**记下意图**，真正的滚动与高亮由 `page.tsx` 在消息出现之后完成 ——
   *   本 hook 从头到尾不碰 DOM（同 `activeIdRef` / `conversationsRef` 的分工）。
   */
  const [pendingJump, setPendingJump] = useState<PendingJump | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;
  const conversationsRef = useRef<Conversation[]>([]);
  conversationsRef.current = conversations;
  /**
   * 导航令牌：每发起一次「切到某条对话」就自增。异步取正文回来时若令牌已经变了
   * （用户又点了别的、或新建了对话），这次结果直接丢弃 —— 否则「点 A 马上点 B」
   * 会让 A 的正文姗姗来迟地把界面切回 A。
   */
  const navRef = useRef(0);
  /**
   * 已成功上传的那一份（id → 那次上传用的**对象引用**）。
   *
   * ⚠ 用**对象身份**而不是 `updatedAt` 做「变了没有」的判据，是因为后者会漏：
   *   `regenerate` 的截断（`messages.slice(0, cutIndex + 1)`）与 `run()` 开头的占位符追加
   *   都**不更新** `updatedAt`，那两类改动会永远同步不上去。而 `patchMessage` / `send` /
   *   `regenerate` 每次都是新构造的对象 —— 身份变了就是内容变了。
   */
  const syncedRef = useRef<Map<string, Conversation>>(new Map());
  /** 待上传的 id（脏集合）。用 Set 去重，插入序即处理序 */
  const pendingRef = useRef<Set<string>>(new Set());
  /** 单飞标志：同一时刻只允许一个 PUT 在途（见 flush 的注释） */
  const flushingRef = useRef(false);
  /**
   * 「清空全部」进行中：**冻住所有写服务端的路径**（见 `deleteAllConversations`）。
   *
   * ⚠⚠ 没有它就会这样：`stop()` 之后 `run()` 的 `finally` 换掉了对话对象的**身份**
   *   （`patchMessage` 每次都是新构造的对象），于是同步 effect 判定「内容变了」把每一条
   *   都标脏并 flush —— 那些 PUT 与我们刚发出的 DELETE **并发**，谁先落库没有保证。
   *   结果是「清空之后又被写回来」，而**只有下次刷新才看得见**（本地列表是干净的空）。
   */
  const bulkDeletingRef = useRef(false);
  /**
   * 在途那一发 PUT 的 promise（`flush` 是单飞的，所以最多一个）。
   *
   * ⚠ 清空**必须先等它落地**：它是唯一一个「在冻结之前就已经发出去」的写，
   *   冻结挡不住它。`flush` 自带 `CONV_TIMEOUT_MS` 超时，所以这个等待是有界的。
   */
  const inflightFlushRef = useRef<Promise<void> | null>(null);
  /**
   * 启动合并的代际。清空成功时自增，用来**作废在途的那一次 `listConversations()`**。
   *
   * ⚠⚠ 那次列表请求可能在清空**之前**就发出去了，回来时带着「删除前」的世界。
   *   直接合并会在侧栏留下几条幽灵（点开 404、静默停在原地，刷新后自愈 ——
   *   不盯着看根本发现不了）。
   */
  const mergeGenRef = useRef(0);

  // ---------- 本地持久化 ----------
  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as Conversation[];
        if (Array.isArray(parsed) && parsed.length) {
          // 刷新后不应残留"生成中"状态
          const cleaned = parsed.map((c) => ({
            ...c,
            messages: c.messages.map((m) => ({ ...m, streaming: false })),
          }));
          setConversations(cleaned);
          setActiveId(cleaned[0].id);
        }
      }
      const savedHybrid = localStorage.getItem("bidding-qa-hybrid");
      if (savedHybrid !== null) setHybrid(savedHybrid === "true");
      const savedRerank = localStorage.getItem("bidding-qa-rerank");
      if (savedRerank !== null) setRerank(savedRerank === "true");
      // 存的是供应商**名**（`deepseek` / `zai`），不是显示用的 label。
      // ⚠ 存的名字后来在服务端不存在了（改了 `_PROVIDERS`、或那家的 Key 被拿掉）时
      //   **不在这里校验** —— 这里读不到 health。后端 `resolve_provider` 会降级到默认
      //   并记 WARNING，前端那个胶囊也会按 health 的列表回落到第一个可用项，两边都不会炸。
      const savedProvider = localStorage.getItem("bidding-qa-llm-provider");
      if (savedProvider) setLlmProvider(savedProvider);
      // ⚠ 这里原先读 `bidding-qa-agent`。**别再读回来**——见文件上方那段注释。
      // ⚠ 这里也**不再读** `bidding-qa-web-search`：联网开关刻意不持久化
      //   （理由见上面 `webSearch` 那段）。用户机器上可能还躺着旧的 `true`，
      //   读回来会让它每次都自动开启、而且关不掉。那个键让它烂着。
    } catch {
      /* 忽略损坏的本地数据 */
    }
    setHydrated(true);
  }, []);

  // ---------- 启动：把服务端那一份并进来 ----------
  // ⚠ 刻意**放在上面那个 effect 之后、且不 await**：首屏必须先由 localStorage 立刻画出来
  //   （那是「历史秒出」的来源，也是服务端不可达时的全部内容）。等这个网络请求回来再画，
  //   刷新时就会先空一下 —— 而 localStorage 版本本来是秒出的，那是明确的退步。
  useEffect(() => {
    void (async () => {
      // 代际快照。⚠ 必须在 `await` **之前**取：清空可能在这次请求发出之后才成功
      // （见下面那个 return）。
      const gen = mergeGenRef.current;
      let serverList: Conversation[];
      try {
        serverList = await listConversations();
      } catch {
        // 连不上（或超时）：保留 localStorage 那份，侧栏如实提示。
        // ⚠ **不改 activeId**（null 就 null，显示欢迎页）—— 本地那份已经挂上了。
        setHistorySynced(false);
        return;
      }
      setServerMode(true);
      setHistorySynced(true);
      // ⚠⚠ 清空在**这次请求发出之后**成功了 ⇒ 手上这份列表是「删除前」的世界，
      //   合并进去会在侧栏留下几条幽灵（点开 404、静默停在原地，只有刷新才自愈）。
      //   ⚠ 只作废**列表合并**这一段：上面那两行状态照置 —— 它们证明的是「库是通的」，
      //     与这份快照新不新无关，删成功也同样是那条连接的功劳。
      //   ⚠ 别让单条 `deleteConversation` 去自增它：那会把整个启动合并白丢一次。
      if (mergeGenRef.current !== gen) return;
      // 两边都有的：以**本地**那份为准，并先标记成「已同步」——否则下面那个扫描
      // 会因为对象身份对不上而把它们整个重传一遍（最多 50 条 × 几百 KB，纯浪费）。
      // ⚠ 这里读的是 ref 而不是 state：`listConversations()` 回来时，上面 hydration
      //   那次 setState 早已提交，ref 是最新的。万一它还是空的（理论上不该发生），
      //   后果只是「多传一遍」——幂等，无损。
      const localById = new Map(conversationsRef.current.map((c) => [c.id, c]));
      for (const server of serverList) {
        const local = localById.get(server.id);
        if (!local) continue;
        // ⚠ 标记成「已同步」的作用是**抑制重传**（本地那份就是刚 PUT 上去的那一份，
        //   内容一致，重传是 50 条 × 几百 KB 的纯浪费）。但它**不能无条件打**：
        //   本地**更新**（服务端不可达期间问过一轮）时打上，下面那个扫描就会跳过它，
        //   那一轮**永远补不进数据库** —— 计划里写的「启动并集合并即自愈」就成了一句
        //   空话（原先正是 `if (local)` 无条件标记，那条自愈路径实际不存在）。
        // ⚠ 本地**更旧**时照旧打上：那时再传一次等于拿旧正文覆盖服务端较新的那一份，
        //   是实打实的数据销毁。旧的那一份由点开时的 `refreshConversation()` 拉回来。
        if (local.updatedAt <= server.updatedAt) syncedRef.current.set(server.id, local);
      }
      setConversations((prev) => mergeServerList(prev, serverList));
    })();
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      // ⚠⚠ 必须把 `remote` 的滤掉。它们只有元数据、`messages` 是空的 ——
      //   落盘等于**用空正文覆盖掉上一轮会话存下来的那份好数据**，
      //   而降级回退路径（服务端不可达时读的就是这一份）就在那一刻失效了。
      //   而且直到用户真的点开那条对话之前，这件事都看不出来。
      const trimmed = conversations.filter((c) => !c.remote).slice(0, MAX_CONVERSATIONS);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
    } catch {
      /* 超出配额时忽略 */
    }
  }, [conversations, hydrated]);

  useEffect(() => {
    if (hydrated) localStorage.setItem("bidding-qa-hybrid", String(hybrid));
  }, [hybrid, hydrated]);

  useEffect(() => {
    if (hydrated) localStorage.setItem("bidding-qa-rerank", String(rerank));
  }, [rerank, hydrated]);

  // ⚠ 空串（= 没选、跟服务端默认）**也要写**：不写的话上次选过的 GLM 会一直留在
  //   localStorage 里，用户「切回默认」这个动作就存不下来，刷新后又被读回来。
  useEffect(() => {
    if (hydrated) localStorage.setItem("bidding-qa-llm-provider", llmProvider);
  }, [llmProvider, hydrated]);

  // 「Agent 模式」的 persist effect 也随开关一起删了（原先写 `bidding-qa-agent`）。
  // 联网搜索的 persist effect 同样在 2026-09-20 删了（原先写 `bidding-qa-web-search`）
  // ——它刻意不持久化，理由见上面 `webSearch` 那段。
  //
  // ⚠ 持久化的 **hybrid / rerank / llmProvider 三个**，hydrate（上面那个挂载 effect）
  //   与 persist（下面各一个 effect）**仍然是两处**，漏一处就是「刷新后回默认值」。
  //   别因为联网那边去掉了持久化，就把这几个也顺手删了。

  // ---------- 消息更新辅助 ----------
  /**
   * `bump=false` 时**不动 `updatedAt`**（现有调用点全用默认值，一行没改）。
   *
   * 只有评分用它：`updatedAt` 的语义是「对话**内容**什么时候变的」，而评价是关于内容的
   * metadata。bump 了的话，给一条上周的旧对话点个 👍，下次刷新它会因为启动合并按
   * `updatedAt DESC` 排序而**跳到侧栏最上面**——看起来像历史记录自己乱了。
   * ⚠ 不 bump **不影响同步**：脏判定是**对象身份**（见 `syncedRef` 的注释），
   *   这里每次都新构造了 conv/message 对象，照样会被 PUT 上去。
   */
  const patchMessage = useCallback(
    (
      convId: string,
      msgId: string,
      patch: Partial<Message> | ((m: Message) => Partial<Message>),
      bump = true,
    ) => {
      setConversations((prev) =>
        prev.map((conv) =>
          conv.id !== convId
            ? conv
            : {
                ...conv,
                updatedAt: bump ? Date.now() : conv.updatedAt,
                messages: conv.messages.map((msg) =>
                  msg.id !== msgId
                    ? msg
                    : { ...msg, ...(typeof patch === "function" ? patch(msg) : patch) },
                ),
              },
        ),
      );
    },
    [],
  );

  // ---------- 服务端同步 ----------
  /**
   * 把脏集合里的对话逐条 PUT 上去（**单飞**：同一时刻只有一个请求在途）。
   *
   * ⚠ 单飞不是优化，是**正确性**要求：两个 PUT 同时在途时落地顺序没有保证 ——
   *   它们是两次独立的 fetch，从连接池发出、由 uvicorn 分派到线程池。
   *   带半截答案的那一发后落库，就会把完整的那份**永久**覆盖掉
   *   （页面上看不出来，只有刷新后才少一段）。
   *   循环里每次重新读 `conversationsRef`，所以排队期间又变过的会取到最新那份。
   *
   * ⚠⚠ **返回「这一发」的 promise（早退时 `null`），不是 `async` 函数** ——
   *   `deleteAllConversations` 要 await 在途那一发。若改成在**调用点**写
   *   `inflightFlushRef.current = flush()`，早退路径返回的是一句**立刻 resolve** 的
   *   promise，会覆盖掉真正在途的那一发，清空于是跳过等待直接发 DELETE。
   *   所以赋值必须在守卫**之后**、在函数内部完成。
   */
  const flush = useCallback((): Promise<void> | null => {
    if (flushingRef.current || bulkDeletingRef.current) return null;
    flushingRef.current = true;
    const run = (async () => {
      try {
        while (pendingRef.current.size) {
          // 清空进行中：立刻收工。剩在队列里的由 deleteAllConversations 的 finally 处理
          // （成功路径上 `pendingRef` 已被 clear，这里是失败路径的兜底）。
          if (bulkDeletingRef.current) break;
          const id = pendingRef.current.values().next().value as string;
          pendingRef.current.delete(id);
          // 标脏之后它可能已经被删掉、或被换成 `remote`（服务端那份）了 —— 两个守卫再判一次
          const conv = conversationsRef.current.find((c) => c.id === id);
          if (!conv || conv.remote || conv.messages.length === 0) continue;
          try {
            await putConversation(conv);
            // ⚠ 竞态兜底：这个 `await` 是「PUT 在途期间这条对话被删掉了」的唯一窗口。
            //   不补下面这一发的话，服务端那一行会被后端这次 PUT 重新建出来 ——
            //   而删除请求是在 PUT **之前**发出的，谁先落库没有保证。
            //   （`deleteConversation` 里也删了 pending/synced，那挡的是「还没轮到」的情况。）
            //   幂等：删一个不存在的 id 是空操作。
            if (!conversationsRef.current.some((c) => c.id === id)) {
              void deleteConversationRemote(id).catch(() => {});
              continue;
            }
            syncedRef.current.set(id, conv);
            setHistorySynced(true);
          } catch {
            // 失败就**不记 `syncedRef`**：它在下一轮提交时会被重新标脏并重试，
            // 下次启动的并集合并也会兜住它 —— 所以这里不需要任何重试逻辑。
            // ⚠ 但**不翻 `serverMode`**：一次网络抖动不该让整个会话从此不再保存。
            setHistorySynced(false);
            // 本次到此为止：多半是数据库不可达，接着发只会串起一串超时。
            return;
          }
        }
      } finally {
        flushingRef.current = false;
      }
    })();
    inflightFlushRef.current = run;
    return run;
  }, []);

  /**
   * 同步边界 = **一轮回答结束**。
   *
   * ⚠⚠ 这是本节最容易被「优化」掉的一处，改之前先读完这段。
   *   `run()` 里每个 `delta` / `reasoning` 分片都会 `patchMessage`（各一次提交），
   *   **一轮回答就是几百到几千次**。若在这里跟着每次提交走，一轮问答会打出上千个 PUT，
   *   每条还带着几百 KB 的正文。
   *
   *   为什么现在这样恰好是「一轮一次」：流式期间 `streaming === true`，这个 effect
   *   每次都**早退**；而 `run()` 的 `finally` 把 `setStreaming(false)` 与最后一次
   *   `patchMessage` 合并在**同一次提交**里（React 18 的自动批处理，`stop()` 触发的那条
   *   路径也一样），于是 effect 恰好在轮末跑一次，拿到的就是终态。
   *   ⚠ 别加防抖／节流来「实时同步」—— 那正是这里要避免的事。
   */
  useEffect(() => {
    if (!hydrated || !serverMode || streaming) return;

    let dirty = false;
    for (const conv of conversationsRef.current) {
      // `remote`：正文还没拉下来，`messages: []` **不是**「没有消息」。放它过去就是用
      //   空数组覆盖服务端已有的正文 —— 静默的数据销毁，且要等下次刷新才发现。
      // 空对话：点一下「开启新对话」就多一条空记录，纯噪音。
      if (conv.remote || conv.messages.length === 0) continue;
      if (syncedRef.current.get(conv.id) === conv) continue; // 身份相同 = 内容没变过
      pendingRef.current.add(conv.id);
      dirty = true;
    }
    // 顺手把本地有、服务端没有的补上去 —— 这就是「并集合并」，也是某轮 PUT 失败后
    // 下一次启动自动补上的自愈路径（它幂等：跑过一次之后再跑就是空集）。
    if (dirty) void flush();
  }, [conversations, streaming, hydrated, serverMode, flush]);

  // ---------- 核心：发送并消费流式回答 ----------
  const run = useCallback(
    async (
      convId: string,
      question: string,
      history: Message[],
      /**
       * 仅本次请求的附加数据（`SendOverrides`）。
       *
       * 现在是**给附件用的**：选图后立刻发送时，本轮闭包里的附件状态是旧值，
       * `overrides.image` 就是为这个竞态存在的。**别把它删成「看着冗余」的形参。**
       *
       * ⚠ 原先它还有第二个用途——首屏「图谱」示例卡片要把 Agent 开关一边拨开一边立刻发请求。
       *   2026-09-20 去掉 Agent 开关后那条链路没了（整条链路固定走 /api/agent），
       *   `SendOverrides.agent` 也一并删掉了。
       */
      overrides?: SendOverrides,
    ) => {
      const assistantId = uid();
      const placeholder: Message = { id: assistantId, role: "assistant", content: "", streaming: true };

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === convId ? { ...conv, messages: [...conv.messages, placeholder] } : conv,
        ),
      );
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;
      let failed = false;

      try {
        const payloadHistory = history
          .filter((m) => m.content.trim())
          .slice(-HISTORY_ROUNDS * 2)
          // 带图的那几轮补上【图片内容】前缀。**不补的话追问第二轮就丢了图片上下文**
          // ——模型看不到上一轮问的是哪个项目，「那工期呢？」就成了没头没脑的一句。
          // ⚠ 只有历史是这样拼；本轮请求发的是**原问题 + image_context 裸文本**，
          //   由后端 rag.with_image_context() 统一拼（模板在 types.ts 里有一份对应的）。
          .map((m) => ({ role: m.role, content: withImageContext(m.content, m.image) }));

        for await (const event of streamChat(
          {
            question,
            session_id: convId,
            history: payloadHistory,
            hybrid,
            rerank,
            // 联网搜索开关。两个端点都认；**开着时后端会在生成之前无条件联网搜一次**
            //（本地答得出也照搜），所以一轮请求最多花一个 credit。
            // ⚠ 延迟增量比「联网本身多慢」大：同一问题 A/B 实测首个可见 token 1.1s→3.0s、
            //   首个**正文**分片 1.9s→7.3s，而 Tavily 往返只占约 1.8s——剩下的是
            //   「提示词里多了 5 条网页资料 → 模型思考更久」（见 CLAUDE.md §联网搜索）。
            // 关着时后端行为与不传这个字段逐字节相同（工具不注册、提示词不提、不联网）。
            web_search: webSearch,
            // 本轮附带的图片。为空串时后端行为与不传这个字段逐字节相同。
            // 后端只拿它构建提示词，**检索用的仍是原问题**——别在这里把图片拼进 question。
            image_context: imageContextText(overrides?.image),
            // 这次提问用哪家文本大模型（工具条上那个胶囊）。**空串 = 用服务端默认**，
            // 此时整条链路与不加这个字段逐字节相同。
            // ⚠ 它是**每请求**的，与 `web_search` 同级——切了只影响之后的提问，已有消息
            //   仍是旧模型产出的（历史只回传 {role, content}，不带任何供应商痕迹，
            //   所以一个对话里混用两家是安全的）。
            llm_provider: llmProvider,
          },
          controller.signal,
        )) {
          if (event.type === "sources") {
            patchMessage(convId, assistantId, {
              sources: event.sources,
              latency: event.latency_ms,
            });
          } else if (event.type === "reasoning") {
            patchMessage(convId, assistantId, (msg) => ({
              reasoning: (msg.reasoning ?? "") + event.content,
            }));
          } else if (event.type === "delta") {
            patchMessage(convId, assistantId, (msg) => ({
              content: msg.content + event.content,
            }));
          } else if (event.type === "answer_reset") {
            // 模型在调用工具前吐的那句「我先查一下」不是最终回答。必须丢掉：
            // content 会被渲染、存进 localStorage，还会作为下一轮的历史回传给模型。
            // ⚠ 只有 `/api/agent` 会发这个事件（`types.ts` 里也标着「仅 /api/agent 产生」）。
            //   `/api/chat` 是写死的流水线，联网发生在**任何正文流出之前**，
            //   没有「先说一句再补搜」这种前导正文可作废，所以它一个都不发。
            patchMessage(convId, assistantId, { content: "" });
          } else if (event.type === "tool_call") {
            patchMessage(convId, assistantId, (msg) => ({
              steps: [
                ...(msg.steps ?? []),
                { id: event.id, name: event.name, query: event.query, status: "running" as const },
              ],
            }));
          } else if (event.type === "tool_result") {
            patchMessage(convId, assistantId, (msg) => ({
              steps: (msg.steps ?? []).map((step) =>
                step.id === event.id
                  ? {
                      ...step,
                      status: (event.ok ? "done" : "error") as "done" | "error",
                      // 图谱/数据库/技能步骤没有「新增资料」的概念，留 undefined 让
                      // ToolCallBlock 走各自的分支；给了 0 会显示成「没有新增资料（与已有资料重复）」。
                      // ⚠ 这里必须把**所有**非检索工具都列上：漏一个，那个工具的步骤就会
                      // 显示成「新增 0 条资料」——一个看起来合理但完全错误的说法。
                      // ⚠ 判据是 `step.name`（不是事件字段）：`new_indices` 只有检索工具才填，
                      //   别的工具恒为空数组，光看载荷分不出「没新增」和「根本没有这个概念」。
                      newCount:
                        event.graph || event.pg || step.name === "load_skill"
                          ? undefined
                          : event.new_indices.length,
                      // JSON 里没有 undefined，服务端发的是 null，这里一并收敛成 undefined
                      graph: event.graph ?? undefined,
                      pg: event.pg ?? undefined,
                      error: event.error ?? undefined,
                    }
                  : step,
              ),
            }));
          } else if (event.type === "error") {
            failed = true;
            patchMessage(convId, assistantId, { error: event.message });
          } else if (event.type === "done") {
            patchMessage(convId, assistantId, (msg) => ({
              latency: { ...(msg.latency ?? {}), total_ms: event.total_ms },
            }));
          }
        }
      } catch (error) {
        const aborted = error instanceof DOMException && error.name === "AbortError";
        if (!aborted) {
          failed = true;
          patchMessage(convId, assistantId, {
            error: error instanceof Error ? error.message : "请求失败，请检查后端服务是否已启动",
          });
        }
      } finally {
        abortRef.current = null;
        setStreaming(false);
        patchMessage(convId, assistantId, (msg) => ({
          streaming: false,
          // 完全没有产出时给一句兜底提示，避免出现空气泡
          content: msg.content || (failed ? "" : "（未获得回答）"),
        }));
      }
    },
    // ⚠ 开关漏一个在这里 = 点了不生效（闭包里拿到的是旧值），不是报错。
    // `agent` 随开关一起从这里删了——它已经不是状态了。
    [hybrid, rerank, webSearch, llmProvider, patchMessage],
  );

  const send = useCallback(
    (question: string, overrides?: SendOverrides) => {
      const text = question.trim();
      // ⚠ 判据是**文字**而非「文字或图片」：图片是「这次提问的上下文」，不是一条独立消息。
      //   纯图无文字就发，后端 question 为空会直接 400（发送按钮也是按文字置灰的）。
      if (!text || streaming) return;

      const current = conversations.find((c) => c.id === activeIdRef.current);
      // ⚠ 正文还没从服务端拉下来（`remote`）的对话**不能提问**：历史会被当成空数组
      //   送给模型（用户前几轮凭空消失），界面上也只剩这一条新消息，而且因为同步那边的
      //   `remote` 守卫它**永远不会被上传**。
      //   按 `selectConversation`「先拉完再切」的写法这条路正常到不了，留着是防御。
      if (current?.remote) return;
      if (!current) {
        const conv = createConversation();
        conv.title = deriveTitle(text);
        conv.messages = [{ id: uid(), role: "user", content: text, image: overrides?.image }];
        setConversations((prev) => [conv, ...prev]);
        setActiveId(conv.id);
        void run(conv.id, text, [], overrides);
        return;
      }

      const userMessage: Message = {
        id: uid(),
        role: "user",
        content: text,
        image: overrides?.image,
      };
      const history = current.messages;
      setConversations((prev) =>
        prev.map((conv) =>
          conv.id !== current.id
            ? conv
            : {
                ...conv,
                title: conv.messages.length === 0 ? deriveTitle(text) : conv.title,
                updatedAt: Date.now(),
                messages: [...conv.messages, userMessage],
              },
        ),
      );
      void run(current.id, text, history, overrides);
    },
    [conversations, run, streaming],
  );

  const regenerate = useCallback(() => {
    if (streaming) return;
    const conv = conversations.find((c) => c.id === activeIdRef.current);
    // 同 `send()`：正文未加载的对话没有「最后一条提问」可言，而且这一跑会把截断后的
    // 空历史当成真相 —— 守卫理由与上面完全一致。
    if (!conv || conv.remote) return;

    // 找到最后一条用户提问，丢弃其后的回答重新生成
    const lastUserIndex = [...conv.messages].reverse().findIndex((m) => m.role === "user");
    if (lastUserIndex === -1) return;
    const cutIndex = conv.messages.length - 1 - lastUserIndex;
    const question = conv.messages[cutIndex].content;
    const history = conv.messages.slice(0, cutIndex);
    // 那条提问若带过图，重新生成时要把解析结果一并带上——否则重生成出来的回答
    // 会突然「不记得」图片里的项目。
    const image = conv.messages[cutIndex].image;

    setConversations((prev) =>
      prev.map((c) => (c.id === conv.id ? { ...c, messages: c.messages.slice(0, cutIndex + 1) } : c)),
    );
    void run(conv.id, question, history, image ? { image } : undefined);
  }, [conversations, run, streaming]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    // ⚠ 这里**不能**再 `setStreaming(false)`（2026-09 修，为了服务端同步）。
    //   `streaming` 变 false 是上面那个同步 effect 的触发条件，在这里提前置 false 会
    //   **多触发一次 PUT**；而 abort 随后一定会落到 `run()` 的 `finally`，那里也置 false
    //   —— 于是同一轮问答有**两个 PUT 同时在途**，落地顺序没有保证（理由见 flush 的注释），
    //   带半截答案的那一发后落库就把完整的覆盖掉了，而且是永久的。
    //   交给 `finally` 一处置即可：被 abort 的 fetch 会在微任务里立刻 reject，
    //   用户完全感知不到这一帧的差别。
  }, []);

  const newConversation = useCallback(() => {
    stop();
    navRef.current += 1; // 作废还在途中的取正文
    const conv = createConversation();
    setConversations((prev) => [conv, ...prev]);
    setActiveId(conv.id);
  }, [stop]);

  /**
   * 从服务端拉一条对话的**正文**，成功后才切过去。
   *
   * ⚠ **所有 `remote` 的对话都必须走这里**。直接 `setActiveId` 会切到一个
   * `messages: []` 的对话，而 `page.tsx` 按 `messageCount === 0` 分支渲染欢迎页 ——
   * 用户点了历史却看到「开始新对话」，以为记录丢了。
   */
  const openConversation = useCallback((id: string) => {
    const token = ++navRef.current;
    void (async () => {
      try {
        const full = await getConversation(id);
        if (navRef.current !== token) return; // 期间又点了别的 / 新建了对话 —— 这次作废
        // 刚拉下来的这一份**就是**服务端的内容，标记成已同步 —— 否则下面那个扫描会把它
        // 原样整个重传一遍（几百 KB，纯浪费）。此后任何一次 `patchMessage` 都会换掉对象身份，
        // 该传的照样会传。
        syncedRef.current.set(id, full);
        setConversations((prev) =>
          // 不在列表里就插入（搜索结果可能命中一条本次启动之后才在别处新建的对话）
          prev.some((c) => c.id === id) ? prev.map((c) => (c.id === id ? full : c)) : [full, ...prev],
        );
        setActiveId(id);
      } catch {
        // ⚠ **不要**在这里 setHistorySynced(false)：失败可能是 404（这条对话在别处被删了），
        //   那与「数据库连不上」是两回事 —— 一并降级会让侧栏莫名其妙显示「未同步」。
        //   可达性的权威判据**只有启动那一次 listConversations**。
        //   这里只是「不切过去」，停留原地。刷新后列表来自服务端，自愈。
      }
    })();
  }, []);

  /**
   * 本地已有正文、**但服务端也有一份**的对话：切过去的**同时**向服务端核对一次
   * （stale-while-revalidate）。界面不等它 —— 本地那份先画出来，离线也照用。
   *
   * ⚠⚠ 这一段修的是一个真实故障，症状正是「点开历史对话，同一句提问出现两次」：
   *   启动合并之后每条对话都成了「本地」的（`mergeServerList` 保留本地对象、把
   *   `remote` 抹掉了），于是 `selectConversation` 的快路径**永远不再拉服务端**。
   *   本地那份一旦被污染（重复的用户消息、截断的正文）或过期，**界面永远显示那一份，
   *   刷新也不会好** —— 而服务端那份干净数据一次都没被读过。用户看到的「多点一次
   *   就多一条」其实是「点开看到的始终是本地那份坏数据」。
   *
   * ⚠ 判据是 `updatedAt`，不能用「拉回来就替换」：
   *   · 本地更新（**正在流式输出**、或离线期间问过）⇒ **不替换**，否则会把在途的
   *     回答整个抹掉。这一条同时就是流式安全的守卫，不需要额外的 `streamingRef`。
   *   · 相等 ⇒ 内容本该一致，替换是空操作；顺手把「时间戳没变但内容被污染」
   *     那种情况一起修掉（重复追加若不 bump `updatedAt` 就是这副形状）。
   *   · 服务端更新 ⇒ 替换，并把新那份标成已同步（不再重传）。
   *   时间戳能这么用，是因为 PUT 把客户端的 ms 原样存进 `timestamptz`、读回来
   *   `round()` 还原为**逐毫秒相等**（`probe_conv_store.py` ⑧ 钉着这条）。
   *
   * ⚠ 失败（404 / 连不上）**什么都不做**：本地那份照旧，也不动 `serverMode`
   *   —— 与 `openConversation` 的 catch 同一条理由（404 ≠ 数据库挂了）。
   */
  const refreshConversation = useCallback((id: string) => {
    void (async () => {
      let full: Conversation;
      try {
        full = await getConversation(id);
      } catch {
        return;
      }
      // ⚠ 读 ref 判断、在 updater **外面**写 `syncedRef`：updater 必须是纯函数
      //   （`reactStrictMode` 下会被调用两次），不能在它里面塞副作用。
      const local = conversationsRef.current.find((c) => c.id === id);
      if (!local || full.updatedAt < local.updatedAt) return;
      syncedRef.current.set(id, full);
      setConversations((prev) => prev.map((c) => (c.id === id ? full : c)));
    })();
  }, []);

  /**
   * 收口一次定位。⚠ 由 `page.tsx` 在**滚动完之后**调用（找不到元素时给一个超时兜底）——
   * 只要它没被调，`page.tsx` 的自动滚到底就一直在让位，表现是「新消息不再自动跟到最新」。
   */
  const clearPendingJump = useCallback(() => setPendingJump(null), []);

  const selectConversation = useCallback(
    (id: string, jump?: SearchJump) => {
      // ⚠⚠ 定位信息必须落在下面那个早退**之前**。搜的恰好是当前这条对话时
      //   `id === activeIdRef.current` 直接 return，写在后面的话这次跳转**整个丢掉** ——
      //   而它的表现只是「点了结果没反应」，看不出是这一行的问题。
      // ⚠ `jump` 为空时**显式清空**：点一条普通列表行应当取消上一次的残留高亮。
      setPendingJump(jump ? { conversationId: id, ...jump } : null);
      if (id === activeIdRef.current) return;
      stop();
      navRef.current += 1;
      const known = conversationsRef.current.find((c) => c.id === id);
      // 本地已有正文 → 直接切，零延迟；否则（`remote`，或根本不在列表里）先拉正文
      if (known && !known.remote) {
        setActiveId(id);
        // 切过去之后**再**跟服务端核对一遍（不阻塞这次切换）—— 见上面的理由
        refreshConversation(id);
        return;
      }
      openConversation(id);
    },
    [stop, openConversation, refreshConversation],
  );

  const deleteConversation = useCallback(
    (id: string) => {
      const wasActive = id === activeIdRef.current;
      if (wasActive) stop();
      // 先把脏集合里那条摘掉：否则一次排队中的 PUT 会在删除之后把它写回去
      pendingRef.current.delete(id);
      syncedRef.current.delete(id);
      setConversations((prev) => prev.filter((c) => c.id !== id));
      // 服务端那一份：本地立刻消失，UI 不等网络。⚠ 失败也不提示 —— 见下面的已知局限。
      // 服务端删除是**幂等**的（删不存在的 id 是空操作），所以本地新建、从未上传过的
      // 对话发这一发也无害。
      // ⚠ 已知局限（有意接受，与「不做多标签页并发协调」同一条取舍）：服务端不可达时
      //   这一发静默失败，本地删掉了、服务端还在，下次启动并集合并后它会**重新出现**。
      //   并集合并只增不删，且「本地有/服务端没有」与「本地删了/服务端还有」在数据上
      //   无法区分（要分开就得再引一张墓碑表）。触发条件苛刻，代价是再删一次。
      void deleteConversationRemote(id).catch(() => {});
      if (!wasActive) return;

      // 删掉的是当前这条 —— 把 activeId 挪到列表首条（即最近创建的）。
      // ⚠ 读 ref 而不是在 updater 里算：updater 必须是纯函数（StrictMode 下会跑两次）。
      const fallback = conversationsRef.current.find((c) => c.id !== id) ?? null;
      navRef.current += 1;
      if (!fallback) {
        setActiveId(null);
        return;
      }
      // 下一条可能也是 `remote` —— 直接 setActiveId 会切到空对话，同 `openConversation`
      if (fallback.remote) openConversation(fallback.id);
      else setActiveId(fallback.id);
    },
    [stop, openConversation],
  );

  /**
   * 清空**全部**对话。⚠⚠ **先删服务端，成功才清本地** —— 与 `deleteConversation` 的
   * 乐观路径**刻意相反**，改之前先读完这段。
   *
   * 为什么不能乐观：`mergeServerList` 是**并集合并、只增不删**（见文件上方）。服务端那份
   * 没删掉的话，下次启动它会**整批复活** —— 用户以为清空了，刷新后又全回来了。
   * 而单条删除可以乐观，是因为它的代价只是「再删一次」；这里代价是**整份历史**。
   *
   * 所以：服务端那一发失败时不碰任何本地状态，把异常**抛给调用方**（侧栏就地显示 + 重试）。
   */
  const deleteAllConversations = useCallback(async () => {
    // 并发第二发直接忽略。⚠ 不只是防抖：两发之间会有一次「解冻 → 解冻后补发 PUT」
    // 的窗口（见 finally），第二发 DELETE 可能夹在那中间。
    if (bulkDeletingRef.current) return;

    bulkDeletingRef.current = true;
    try {
      // ① 等在途那一发 PUT 落地。它是**唯一**可能在 DELETE 之后落库的写 ——
      //    冻结挡不住它（它在冻结之前就发出去了）。单飞保证最多一个，且自带超时。
      const inflight = inflightFlushRef.current;
      if (inflight) await inflight.catch(() => {});

      // ② 服务端。失败直接抛出 ⇒ 本地一个字都还没动。
      await deleteAllConversationsRemote();

      // ③ 到这里服务端已经空了。下面全是本地收尾，**没有 await**，
      //    所以不会再有异步结果插进来（本地收尾要么全跑要么不跑）。
      //    ⚠ `stop()` 放在**成功后**而不是请求前：失败路径上 abort 掉用户正在看的回答
      //      是一次**本地破坏** —— 库连不上时他白白丢了一轮回答，却什么都没删成。
      stop();
      navRef.current += 1; // 作废在途的取正文
      // ⚠ 必清，不是打扫卫生：挂在**已删**对话上的待办会被 `page.tsx` 的定位 effect
      //   直接 return 掉（`conversationId !== activeId`），连那个 1.5 秒兜底都不挂 ——
      //   于是它**永久**压着「自动滚到底」，症状是「新消息再也不自动跟到最新」。
      setPendingJump(null);
      // ⚠ 显式 remove，不指望下面那个落盘 effect 写 `"[]"`：那条路依赖 `setItem` 成功，
      //   而超配额 / 隐私模式下它会抛、**旧值原样留着** → 下次启动本地那份复活 →
      //   自愈扫描把整批又 PUT 回服务端。（落盘 effect 随后照常写 `"[]"`，两者不冲突。）
      try {
        localStorage.removeItem(STORAGE_KEY);
      } catch {
        /* 隐私模式下 removeItem 也会抛 */
      }
      pendingRef.current.clear();
      syncedRef.current.clear();
      mergeGenRef.current += 1; // 作废在途的启动合并（见那个 effect）
      setConversations([]);
      setActiveId(null); // page.tsx 落到欢迎页分支
    } catch (error) {
      // 本地未动 —— 交给调用方如实显示，列表还是原来那个世界
      throw error;
    } finally {
      bulkDeletingRef.current = false;
      // 失败路径上队列里可能还冻着几条（循环里那个 `break`）—— 解冻后补发，
      // 否则它们要等到**下一次状态变化**才重试（表现是「刚问的那一轮没进数据库」）。
      // 成功路径是空操作（`pendingRef` 已被 clear）。
      if (pendingRef.current.size) void flush();
    }
  }, [stop, flush]);

  /**
   * 给一条回答打 👍/👎（`value` 是 1 或 -1）。**再点同一个 = 取消评价**。
   *
   * 同一个事实存两处，**互不替代**：
   * - **哪个拇指亮着** → `Message.feedback`，随 `payload jsonb` 走，刷新后自动回来
   *   （后端为零改动地存下它，见 `types.ts` 那个字段的注释）；
   * - **这次评价是什么** → `feedback` 表，在下面显式 POST 一份快照过去（供分析取数）。
   *
   * ⚠ **本地先改、不等网络**（照 `deleteConversation` 的成例）：POST 失败也**不回滚**。
   *   两者会漂移（拇指亮着但没入库），这是**有意接受**的——回滚会让用户看着自己刚点的
   *   👍 自己弹回去，比少写一行分析数据糟得多。对应地，调用点一律 `.catch(() => {})`。
   */
  const setFeedback = useCallback(
    (convId: string, msgId: string, value: 1 | -1) => {
      const conv = conversationsRef.current.find((c) => c.id === convId);
      // 三个守卫：① `remote` 的对话正文还没拉下来，拿它算快照会存一份**空答案**上去
      // （与 `send` / PUT / localStorage 那三处是同一批守卫）；② 消息正在流式输出，
      // 评价的是哪一版正文说不清；③ 找不到对话或消息（刚被删掉）就什么都别做。
      if (!conv || conv.remote) return;
      const index = conv.messages.findIndex((m) => m.id === msgId);
      if (index < 0) return;
      const msg = conv.messages[index];
      if (msg.streaming) return;

      const next = msg.feedback === value ? undefined : value;
      // 本地立刻生效。`undefined` 在 JSON 序列化时会被丢掉，所以「取消」是真的没有这个字段。
      patchMessage(convId, msgId, { feedback: next }, false);

      if (next === undefined) {
        void deleteFeedback(convId, msgId).catch(() => {});
        return;
      }
      // 快照三件套。问题取**往前最近的一条用户消息**：中间可能隔着若干工具轮，
      // 但那些都是这条回答的步骤、不是新提问。
      let question = "";
      for (let i = index - 1; i >= 0; i -= 1) {
        if (conv.messages[i].role === "user") {
          question = conv.messages[i].content;
          break;
        }
      }
      void putFeedback(convId, msgId, {
        rating: next,
        question,
        answer: msg.content,
        // 网页条目的 doc_id 带 `web:` 前缀，与知识库条目天然分得开，不用额外标记
        doc_ids: (msg.sources ?? []).map((s) => s.doc_id),
      }).catch(() => {});
    },
    [patchMessage],
  );

  const activeConversation = conversations.find((c) => c.id === activeId) ?? null;

  return {
    conversations,
    activeConversation,
    activeId,
    streaming,
    hybrid,
    rerank,
    webSearch,
    /** 这次提问用哪家文本大模型；**空串 = 跟服务端默认**（胶囊没选时就是它） */
    llmProvider,
    hydrated,
    /**
     * 历史是否已同步到数据库。**三态**：`null` = 启动中、还没有结论，界面上一个字都不说。
     * （`serverMode` 是内部实现，不对外暴露 —— 外面只该关心「能不能如实说已同步」。）
     */
    historySynced,
    setHybrid,
    setRerank,
    setWebSearch,
    setLlmProvider,
    send,
    stop,
    regenerate,
    newConversation,
    selectConversation,
    deleteConversation,
    /**
     * 清空**全部**对话。⚠ 与 `deleteConversation` 的签名**形状不同**：它是 `async`
     * 且**会抛**（服务端删成功才清本地，见实现里的注释）。调用方必须 `await` + `catch`，
     * 不能像单条删除那样 fire-and-forget —— 吞掉失败等于「本地清了、服务端还在」。
     */
    deleteAllConversations,
    setFeedback,
    /**
     * 待办的「跳到命中消息」。`page.tsx` 消费它：消息进了 DOM 就滚动 + 高亮，
     * 然后调 `clearPendingJump()` 收口。⚠ 消费者**必须**收口 ——
     * 一直挂着的话 `page.tsx` 那条「自动滚到底」会永远让位给它（见那边的 effect）。
     */
    pendingJump,
    clearPendingJump,
  };
}
