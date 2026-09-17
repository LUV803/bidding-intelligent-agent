import type {
  Conversation,
  ConversationHit,
  HealthInfo,
  Message,
  ServerConversationFull,
  ServerConversationHit,
  ServerConversationMeta,
  Source,
  StreamEvent,
  VisionPayload,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || "http://localhost:8000";

/** 解析 SSE 流：按 \n\n 分帧，逐条产出事件 */
export async function* streamChat(
  payload: {
    question: string;
    session_id?: string;
    history?: Pick<Message, "role" | "content">[];
    top_k?: number;
    hybrid?: boolean;
    /** 是否对初筛结果做 CrossEncoder 二次精排 */
    rerank?: boolean;
    /**
     * 联网搜索（Tavily）：**开了就每轮都查**——后端先检索本地知识库，再用原问题搜一次网，
     * 两份结果一起进【资料】作答。（2026-09 之前是「答不出才搜」的兜底语义，已改。）
     *
     * ⚠ 它**原样进请求体**（这个函数不再剥任何字段，见下面端点那行）。
     */
    web_search?: boolean;
    /**
     * 图片解析结果（纯文本），作为**本次提问**的上下文。
     *
     * ⚠ 后端拿它去构建提示词，**不参与检索**；为空串时全链路行为与不传这个字段完全一致。
     * 前端的 `imageContextText()` 负责把 VisionPayload 变成这个字符串。
     */
    image_context?: string;
    /**
     * 这次提问用哪家文本大模型（`deepseek` / `zai`）。
     *
     * ⚠ **空串或不传 = 用服务端默认**（`.env` 的 `LLM_PROVIDER`），此时整条链路与
     * 不加这个字段完全一致。供应商名认不出来时后端降级到默认 + 记一行 WARNING，不报错。
     */
    llm_provider?: string;
  },
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  // ⚠ **固定走 `/api/agent`**：2026-09-20 去掉了「直接 RAG / Agent 模式」切换按钮，
  //   Agent 模式现在是唯一路径（端点选择原来也在这个函数里，由 `agent` 字段分派）。
  //
  // `/api/chat` 那条写死流水线**后端还在**（探针仍钉着它的 SSE 契约），只是这个界面上
  // 再也打不到它了。要恢复切换，在这里按参数分派端点、并在 useChat 里把状态加回来即可。
  const response = await fetch(`${API_BASE}/api/agent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });

  if (!response.ok) {
    throw new Error(await readError(response));
  }
  if (!response.body) {
    throw new Error("服务端未返回数据流");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const raw = line.slice(5).trim();
      if (!raw) continue;
      try {
        yield JSON.parse(raw) as StreamEvent;
      } catch {
        // 忽略无法解析的帧，保证流不中断
      }
    }
  }
}

export async function fetchHealth(): Promise<HealthInfo> {
  const response = await fetch(`${API_BASE}/api/health`, { cache: "no-store" });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

/** 纯检索接口，便于调试混合检索效果 */
export async function search(
  query: string,
  mode: "hybrid" | "vector" | "bm25" = "hybrid",
  topK = 5,
  rerank?: boolean,
): Promise<{ sources: Source[]; latency_ms: Record<string, number> }> {
  const response = await fetch(`${API_BASE}/api/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, mode, top_k: topK, rerank }),
  });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

/**
 * 上传一张图片请后端解析（后端调智谱视觉模型）。
 *
 * **不走 streamChat**：那个是绑定 `response.body` 的 SSE 帧解析器，而这个接口是普通 JSON
 * ——解析是一次性的，不需要流式（前端在附件 chip 上显示「解析中…」即可）。
 *
 * `imageBase64` 必须是**裸 base64**：`data:image/png;base64,` 前缀要在调用方切掉。
 * 后端对这种形态仍然兼容（只记一条警告），但两边的约定是不带前缀。
 */
export async function analyzeImage(
  imageBase64: string,
  mediaType: string,
): Promise<VisionPayload> {
  const response = await fetch(`${API_BASE}/api/vision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_base64: imageBase64, media_type: mediaType }),
  });
  // 未配 Key → 503、图片过大 → 413、请求体为空 → 400，三种都从这里抛出中文 detail
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

// ---------- 对话历史（服务端持久化） ----------
//
// 这八个函数是**唯一的 snake_case ↔ camelCase 边界**：后端 `schemas.py` 一律 snake_case，
// 组件一律 camelCase，转换只在这里做，别让它漏出去（也别把 camelCase 塞进请求体）。
//
// ⚠ 全部带超时。这些调用都不在「用户盯着转圈的等待路径」上（列表在启动时、正文在切换
//   对话时），所以一个被黑洞吞掉的连接不会有任何报错——只会让侧栏永远停在同步中、
//   点历史对话毫无反应，且**永远不降级**（降级的触发条件就是「这次调用失败了」）。
//   超时后如实抛错 → 调用方退回 localStorage。**降级而非报错**，与 reranker / graph / pg 同形。

/** 单次请求超时。本地库是毫秒级的，15 秒只用来兜「连接挂死」这一种情况 */
const CONV_TIMEOUT_MS = 15000;

/** 服务端元数据 → 前端 `Conversation`。`messages: []` 是「尚未拉取」，由 `remote` 标记 */
function toConversation(row: ServerConversationMeta): Conversation {
  return {
    id: row.id,
    title: row.title,
    messages: [],
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    remote: true,
  };
}

/**
 * 拉取全部对话的**元数据**（不含正文），按最近活跃倒序（服务端 `ORDER BY updated_at DESC`）。
 *
 * ⚠ 正文不在这里。单条对话可达几百 KB（12 条 `sources`、`steps[].pg` 整行原文、
 * `reasoning` 全文），首屏把几十条全拉下来，「历史秒出」这个卖点就没了。
 * 正文由 `getConversation` 按需取。
 */
export async function listConversations(): Promise<Conversation[]> {
  const response = await fetch(`${API_BASE}/api/conversations`, {
    cache: "no-store",
    signal: AbortSignal.timeout(CONV_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(await readError(response));
  const rows = (await response.json()) as ServerConversationMeta[];
  return rows.map(toConversation);
}

/** 拉取一条对话的完整正文。**这条对话不存在时抛错**（404），与「连不上」是两回事 */
export async function getConversation(id: string): Promise<Conversation> {
  const response = await fetch(`${API_BASE}/api/conversations/${encodeURIComponent(id)}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(CONV_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(await readError(response));
  const row = (await response.json()) as ServerConversationFull;
  return {
    ...toConversation(row),
    remote: false,
    // ⚠ 规范化 `streaming`：库里若躺着一条 `streaming: true` 的消息（旧版本写进去的、
    //   或某次 PUT 恰好赶在收尾之前），渲染出来是一个**永远转不完的圈**。
    //   刷新页面本该清理掉所有「生成中」状态，从服务端加载时同理。
    messages: row.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
  };
}

/**
 * 整体替换式保存（服务端单事务：upsert 对话 → 删光消息 → 重插）。
 *
 * **整体替换而不是增量**：`regenerate` 会把消息数组截断，增量 API 得让前端告诉服务端
 * 删了哪几条；整体替换天然幂等、**重试安全**，前端也不需要 diff 逻辑。
 *
 * ⚠ 调用方**必须**保证 `conv.messages` 是完整正文——空数组会把服务端的正文覆盖成空。
 *   这正是 `Conversation.remote` 存在的首要理由，守卫在 `useChat` 的同步 effect 里。
 */
export async function putConversation(conv: Conversation): Promise<void> {
  const response = await fetch(`${API_BASE}/api/conversations/${encodeURIComponent(conv.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: conv.title,
      created_at: conv.createdAt,
      updated_at: conv.updatedAt,
      // 原样送（后端 jsonb 整存整取，不解释内部结构）。`remote` 是前端专有标志，
      // 不进请求体——它不是这条对话的内容，只是「正文还没拉下来」的本地状态。
      messages: conv.messages,
    }),
    // ⚠ 这条必须有超时：它在「一轮回答结束」的同步点被调用，若挂死，
    //   队列里后续的保存会一直被堵住（`useChat` 的写入器是串行的）。
    signal: AbortSignal.timeout(CONV_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(await readError(response));
}

/** 删除一条对话（级联删消息）。**服务端是幂等的**，删一个不存在的 id 不报错 */
export async function deleteConversationRemote(id: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/conversations/${encodeURIComponent(id)}`, {
    method: "DELETE",
    signal: AbortSignal.timeout(CONV_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(await readError(response));
}

/**
 * 清空**全部**对话（服务端消息随外键级联；`feedback` 一行不动）。
 *
 * ⚠⚠ 与上面 `deleteConversationRemote` 的失败处置**刻意相反**，别「统一」：
 *   单条删除是 **fire-and-forget**（调用点 `.catch(() => {})`，本地先消失），
 *   而这条**必须把失败抛出去** —— `useChat.deleteAllConversations` 的
 *   「服务端删成功才清本地」策略全靠这个判据。吞掉失败 = 本地清了、服务端还在，
 *   下次启动并集合并（只增不删）会把整个列表**原地复活**：用户以为清空了，刷新后又全回来。
 *
 * ⚠ `readError` 优先取后端 `detail`，所以 503 时抛出的就是 `conv_store` 那句中文
 *   （「对话历史数据库暂时不可用，未删除任何对话。」），侧栏直接显示它。
 */
export async function deleteAllConversationsRemote(): Promise<void> {
  const response = await fetch(`${API_BASE}/api/conversations`, {
    method: "DELETE",
    signal: AbortSignal.timeout(CONV_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(await readError(response));
}

/**
 * 搜索历史对话（标题 **或** 正文命中）。
 *
 * ⚠ `q` 必须 `encodeURIComponent`：查询词是中文，还可能带 `&` / `#` / `%`
 *   ——直接拼进 query string 会让参数被截断，而失败形态是**静默的空结果**。
 *
 * ⚠ 服务端用的是 `ILIKE '%…%'` 而**不是全文检索**：本机 PostgreSQL 没装 zhparser /
 *   pg_jieba，中文没法分词，`to_tsvector` 根本用不了。LIKE 的通配符由服务端转义。
 */
export async function searchConversations(query: string, limit = 20): Promise<ConversationHit[]> {
  const response = await fetch(
    `${API_BASE}/api/conversations/search?q=${encodeURIComponent(query)}&limit=${limit}`,
    { cache: "no-store", signal: AbortSignal.timeout(CONV_TIMEOUT_MS) },
  );
  // 空 q → 400（服务端拒绝，不返回全表——那是个看起来完全合理的错误答案）
  if (!response.ok) throw new Error(await readError(response));
  const rows = (await response.json()) as ServerConversationHit[];
  return rows.map((row) => ({
    id: row.id,
    title: row.title,
    updatedAt: row.updated_at,
    snippet: row.snippet,
    snippetRole: row.snippet_role,
    hitMessageId: row.hit_message_id,
  }));
}

// ---------- 回答反馈（👍/👎） ----------
//
// 两条路由本身无 snake_case 转换要做：请求体字段名与前端一致（rating/question/answer/
// doc_ids 全是小写单词），响应体前端只看 `response.ok`。
//
// ⚠ 与历史那八个函数不同的一点：这两个**全部是 fire-and-forget**（`useChat.setFeedback`
//   调用点带 `.catch(() => {})`）。本地状态在调用前就已经改好了，失败不回滚——
//   让用户看着自己刚点的 👍 自己弹回去，比少写一行分析数据糟得多。

/** 记一条评价。同一条消息再评一次 = 覆盖（服务端 upsert）。 */
export async function putFeedback(
  conversationId: string,
  messageId: string,
  body: { rating: 1 | -1; question: string; answer: string; doc_ids: string[] },
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/feedback/${encodeURIComponent(conversationId)}/${encodeURIComponent(messageId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(CONV_TIMEOUT_MS),
    },
  );
  if (!response.ok) throw new Error(await readError(response));
}

/** 取消一条评价。**服务端幂等**，删一个不存在的 key 也不报错 */
export async function deleteFeedback(
  conversationId: string,
  messageId: string,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/feedback/${encodeURIComponent(conversationId)}/${encodeURIComponent(messageId)}`,
    { method: "DELETE", signal: AbortSignal.timeout(CONV_TIMEOUT_MS) },
  );
  if (!response.ok) throw new Error(await readError(response));
}

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json();
    if (typeof data?.detail === "string") return data.detail;
    return JSON.stringify(data);
  } catch {
    return `请求失败（HTTP ${response.status}）`;
  }
}
