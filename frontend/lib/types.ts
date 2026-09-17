export type Role = "user" | "assistant";

/** 一条参考来源（对应知识库中的问答对） */
export interface Source {
  index: number;
  doc_id: string;
  question: string;
  answer: string;
  snippet: string;
  highlights: string[];
  /** 归一化后的融合得分 0~1 */
  score: number;
  rrf_score: number;
  vector_score: number | null;
  bm25_score: number | null;
  /** 重排模型给出的相关性概率 0~1；未启用重排时为 null */
  rerank_score: number | null;
  rank_vector: number | null;
  rank_bm25: number | null;
  /** 重排前的融合名次，与 index 对比可看出位次升降 */
  rank_fused: number | null;
  matched_by: string[];
  /**
   * 网页原文链接；知识库条目恒为 null。
   *
   * 与 `origin` 一起是「联网搜索并入参考来源面板」的必付代价：网页结果套不进
   * 「问答对」这个形状，只能挂两个字段上去区分。⚠ 这两个键**恒定存在**，
   * 所以 `sources` 事件的 JSON 相比加联网之前会多出 `"url": null, "origin": "kb"`。
   */
  url?: string | null;
  /** 来源类型：知识库问答对 kb / 联网搜索到的网页 web */
  origin?: "kb" | "web";
  /** 网页的发布时间（原样字符串，可能是空串）；知识库条目恒为空串 */
  published_date?: string;
}

/** 知识图谱查询结果的一行：一个实体，以及它在本次查询下的统计 */
export interface GraphRow {
  /** 实体名（供应商 / 标的物 / 地区…） */
  label: string;
  /** 涉及该实体的交易笔数 */
  count: number;
  /** 涉及金额（元）；非金额类查询为 null */
  amount: number | null;
}

/**
 * search_knowledge_graph 的结构化结果 → 独立的「知识图谱查询」块。
 *
 * 与 Source 刻意分开：Source 是知识库问答对、带 [n] 引用编号；图谱结果是
 * 「实体 + 统计」，形状和读者预期都不一样，所以不进来源面板、不参与引用编号。
 */
export interface GraphPayload {
  query_type: string;
  /** 服务端算好的中文语义，直接显示这句 */
  query_label: string;
  ok: boolean;
  /** 模型给出的原始关键词 */
  entity: string;
  /** 模糊匹配后实际命中的实体名；与 entity 不同 = 发生了自动纠名 */
  resolved: string | null;
  /** 其他候选实体（命中多个时列出，供模型换名重查） */
  candidates: string[];
  total: number;
  /** total 只是下限（取数撞上了服务端硬上限） */
  total_capped: boolean;
  rows: GraphRow[];
  truncated: boolean;
  /** 「没找到」「关键词太宽泛」等说明 */
  note: string | null;
  /** 附加数值统计，如 { 交易笔数: 107, 成交总额: 973172 } */
  extra: Record<string, number>;
  elapsed_ms: number;
}

/**
 * 采购记录查询结果的一行。
 *
 * 一个形状覆盖两种模式（明细 / 分组），看 `PgPayload.mode` 决定该渲染哪一半：
 * 分组的行用 label/count/amount，明细的行用其余字段。与 GraphRow 同理——
 * 与 Source 刻意分开，不进来源面板、不参与 [n] 引用编号。
 */
export interface PgRow {
  /** 分组名（mode="group" 时有值） */
  label?: string;
  count?: number | null;
  amount?: number | null;
  title?: string;
  project?: string;
  purchaser?: string;
  supplier?: string;
  published?: string;
  /** 省份+市区+县城 拼成的地区串 */
  region?: string;
  subject?: string;
  category?: string;
  source?: string;
}

/**
 * search_procurement_records 的结构化结果 → 独立的「采购数据库查询」块。
 */
export interface PgPayload {
  /** 服务端算好的中文标题，直接显示这句 */
  query_label: string;
  ok: boolean;
  mode: "detail" | "group";
  /**
   * 实际生效的筛选条件。
   *
   * **这是这个块最有价值的部分**：模型把「安徽」理解成省份筛选、把「过亿」理解成
   * 金额下限，这些理解偏差必须让用户看得见，否则错答案看起来和好答案一模一样。
   */
  filters: Record<string, string>;
  /** count(*) 精确总数，不是下限 */
  total: number;
  rows: PgRow[];
  truncated: boolean;
  note: string | null;
  /** 附加数值统计，如 { 命中记录数: 8751, 中标金额合计: 21856725663.93 } */
  extra: Record<string, number>;
  elapsed_ms: number;
}

/**
 * 一张图片的解析结果（`POST /api/vision` → 后端 `VisionResult`）。
 *
 * ⚠ **这里只存解析结果，绝不存原图 base64。** useChat 会把整个 conversations 写进
 * localStorage（约 5MB 配额），而 base64 动辄几 MB —— 塞进去是**静默**失败（写不进去，
 * 也不报错，用户只会发现历史对话莫名其妙没了）。原图用完即弃。
 */
export interface VisionPayload {
  ok: boolean;
  /** 文档类型，如「招标公告」；渲染成卡片右上角的徽章 */
  doc_type: string;
  /**
   * 结构化字段。**顺序即展示顺序**（后端按固定字段表取值，不是模型的键序），
   * 所以前端直接 Object.entries 遍历即可，不要排序。
   */
  fields: Record<string, string>;
  /** 一两句话的摘要，用于输入框旁的附件 chip */
  summary: string;
  /** 识别出的全文，折叠区展示 */
  full_text: string;
  elapsed_ms: number;
  /** 失败原因或降级说明 */
  note: string | null;
}

/**
 * `send()` 的本次请求附加数据。
 *
 * 存在的理由见 useChat.run 的注释：setState 之后本轮的闭包里状态还是旧值，
 * 附件（选完图立刻发送）要靠 `image` 绕开这个竞态。
 *
 * ⚠ 原先这里还有个 `agent?: boolean`（首屏「图谱」卡片要一边拨 Agent 开关一边发请求）。
 *   2026-09-20 去掉 Agent 开关后，整条链路固定走 `/api/agent`，那个字段没有存在意义了。
 */
export type SendOverrides = { image?: VisionPayload };

/**
 * VisionPayload → 给模型看的纯文本。
 *
 * 解析失败（`ok=false`）时返回空串：那时没有任何可信内容，宁可什么都不带。
 */
export function imageContextText(image?: VisionPayload | null): string {
  if (!image?.ok) return "";
  const lines: string[] = [];
  if (image.doc_type) lines.push(`文档类型：${image.doc_type}`);
  for (const [key, value] of Object.entries(image.fields)) lines.push(`${key}：${value}`);
  if (image.full_text) lines.push(`全文：\n${image.full_text}`);
  return lines.join("\n");
}

/**
 * 把图片解析结果附在问题前。
 *
 * ⚠ **这个模板在后端 `rag.with_image_context()` 里有一份一模一样的**，改一边要改另一边。
 * 分工是：**本轮**请求由后端拼（前端只发 `image_context` 裸文本 + 原问题），
 * **历史**轮次由这里拼 —— 不拼的话，追问第二轮时模型就看不到上一轮问的是哪个项目了
 * （`history_rounds=3`，图片上下文会在后续三轮里一直跟着）。
 *
 * ⚠ 拼出来的文本**只给模型看，不参与检索**：后端拿它去构建提示词，检索用的永远是原问题。
 * 详见 `src/bidding_qa/rag.py` 里 `with_image_context` 的 docstring。
 */
export function withImageContext(question: string, image?: VisionPayload | null): string {
  const ctx = imageContextText(image);
  if (!ctx) return question;
  return `【图片内容】\n${ctx}\n\n【用户提问】\n${question}`;
}

/** Agent 模式下模型调用一次工具的过程 */
export interface ToolStep {
  id: string;
  name: string;
  /** 展示用文本：检索工具是改写后的检索词，图谱工具是「查询类型：实体」 */
  query: string;
  status: "running" | "done" | "error";
  /**
   * 本次检索新增了几条资料（与已有资料重复的不计）。
   * 图谱步骤**恒为 undefined**——图谱结果不是「资料」，没有新增/重复的说法。
   */
  newCount?: number;
  /** 图谱工具的结构化结果（仅 search_knowledge_graph 产生） */
  graph?: GraphPayload | null;
  /** 采购记录工具的结构化结果（仅 search_procurement_records 产生） */
  pg?: PgPayload | null;
  error?: string | null;
}

export interface Message {
  id: string;
  role: Role;
  content: string;
  /** 模型思维链（「深度思考」折叠区内容） */
  reasoning?: string;
  /** 模型调用工具的过程（走 `/api/agent`，界面上现在只有这一条链路，所以总可能产生） */
  steps?: ToolStep[];
  /**
   * 这条提问附带的图片解析结果（仅用户消息可能有）。
   *
   * 存的是 **VisionPayload 而不是 base64** —— 见 VisionPayload 的注释。
   * `content` 保持用户原样输入的文字，图片内容不拼进去（渲染时由 ImageResultBlock
   * 单独呈现，回传历史时才由 withImageContext 拼）。
   */
  image?: VisionPayload;
  sources?: Source[];
  latency?: Record<string, number>;
  /**
   * 这条回答收到的评价：1 = 👍，-1 = 👎，没评过则无此字段（undefined）。
   *
   * ⚠ **后端为零改动地存下它**：`ConversationUpsert.messages` 是 `list[dict]`，
   *   `messages` 表只从 jsonb 里生成 `role` / `content` 两列 —— 这个字段原样进
   *   `payload`、原样回来。所以「哪个拇指亮着」不需要任何额外存储。
   * ⚠ 它**不是**分析数据本身：分析用的是 `feedback` 表（带 question/answer/doc_ids
   *   快照），由 `useChat.setFeedback` 显式 POST 过去。两者在 POST 失败时会漂移
   *   （拇指亮着但没入库），这是**有意接受**的——见 `conv_store.py` 的 feedback DDL 注释。
   */
  feedback?: 1 | -1;
  /** 正在流式输出 */
  streaming?: boolean;
  /** 生成出错时的提示 */
  error?: string;
}

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  createdAt: number;
  updatedAt: number;
  /**
   * 这条对话来自服务端、**正文尚未拉取**（`messages` 是空数组，不是「真的没有消息」）。
   *
   * `GET /api/conversations` 只回元数据（正文可达几百 KB，首屏全拉下来「历史秒出」就没了），
   * 正文由 `selectConversation` 按需拉。因此这个标志**同时是三个守卫的判据**，缺一个都是
   * 静默的数据销毁：
   *
   * 1. **同步（PUT）**：`remote` 的对话绝不能上传——`messages: []` 会把服务端的正文覆盖成空。
   * 2. **落 localStorage**：也要跳过。否则一个只拉到元数据的对话会以空正文落盘，
   *    **把上一轮会话存下来的那份好数据覆盖掉**，降级回退路径就在那一刻失效了。
   * 3. **`send()`**：不能在一个未加载的对话上提问——历史会被当成空数组送给模型
   *    （用户前几轮凭空消失），而且因为守卫 1 它**永远不会被同步**。
   *
   * ⚠ 它**不进 localStorage**（与守卫 2 是同一件事的两面）：刷新后本地那份没有 `remote`，
   *   服务端那份由启动时的 `listConversations` 重新标上。
   */
  remote?: boolean;
}

/**
 * 服务端 `GET /api/conversations` 返回的一条**元数据**（不含正文）。
 *
 * 键名是原样的 snake_case —— 后端 `schemas.py` 一律 snake_case，转换**只发生在
 * `lib/api.ts` 的边界函数里**，别把 camelCase 漏到后端、也别让 snake_case 漏进组件。
 */
export interface ServerConversationMeta {
  id: string;
  title: string;
  /** epoch 毫秒（与前端 `Conversation.createdAt` 同一口径） */
  created_at: number;
  updated_at: number;
  message_count: number;
}

/** `GET /api/conversations/{id}`：元数据 + 完整正文 */
export interface ServerConversationFull extends ServerConversationMeta {
  /**
   * 前端 `Message` 对象的**原样数组**。
   *
   * ⚠ 后端不解释它内部的结构（jsonb 整存整取），所以这里直接断言成 `Message[]`。
   *   将来 `Message` 加了字段，后端不用改、这里也不用改。
   */
  messages: Message[];
}

/** `GET /api/conversations/search` 的一条：元数据 + 命中片段 */
export interface ServerConversationHit extends ServerConversationMeta {
  snippet: string;
  /** 片段来自哪一侧（user / assistant）；只在标题里命中时为空串 */
  snippet_role: string;
  /** 命中的那条消息 id；只在标题里命中时为空串 */
  hit_message_id: string;
}

/** 搜索结果（已转成前端命名）。侧栏「原地替换列表」时渲染的就是它 */
export interface ConversationHit {
  id: string;
  title: string;
  updatedAt: number;
  /** 命中位置附近的正文片段，服务端已切好；只在标题里命中时为空串 */
  snippet: string;
  /** 片段来自哪一侧，用来在片段前标注「问：」还是「答：」 */
  snippetRole: string;
  /**
   * 命中的那条消息 id —— 点搜索结果后滚到它并高亮关键词。
   *
   * ⚠ 空串 = **命中在标题里**（没有「哪一条消息」可言），此时**不跳转**，
   *   只按老行为切过去。别退化成「跳到第一条消息」：那是猜。
   * ⚠ 降级路径（本机兜底搜索）也给这个字段，所以两条路的行为一致。
   */
  hitMessageId: string;
}

/**
 * 点搜索结果时带上的「跳到哪一条、高亮什么词」。
 *
 * ⚠ 只在 `hitMessageId` 非空时构造 —— 标题命中没有可跳的位置（见上）。
 */
export interface SearchJump {
  messageId: string;
  /** 要标黄的关键词。就是搜索框里那个词（点击时搜索态会被清掉，所以必须随行带上） */
  keyword: string;
}

export type StreamEvent =
  | { type: "start"; session_id?: string }
  | { type: "sources"; sources: Source[]; latency_ms: Record<string, number> }
  | { type: "reasoning"; content: string }
  | { type: "delta"; content: string }
  | { type: "done"; total_ms: number }
  | { type: "error"; message: string }
  // 以下三类仅 /api/agent 产生
  /** 模型在调用工具前已吐出的前导正文需作废（它不属于最终回答） */
  | { type: "answer_reset"; round: number }
  | { type: "tool_call"; round: number; id: string; name: string; query: string }
  | {
      type: "tool_result";
      round: number;
      id: string;
      ok: boolean;
      new_indices: number[];
      known_indices: number[];
      elapsed_ms: number;
      error?: string | null;
      /** 仅 search_knowledge_graph 返回：图谱结构化结果 */
      graph?: GraphPayload | null;
      /** 仅 search_procurement_records 返回：采购记录结构化结果 */
      pg?: PgPayload | null;
    };

/**
 * 一家可选的文本大模型供应商（后端 `available_providers()` 的一项）。
 *
 * ⚠ 列表里**只有真配了 API Key 的**（后端过滤过）。名字合法但没配 Key 的供应商
 * 点下去必然失败，摆出来是骗人 —— 所以正常路径上点不到那种。
 */
export interface LlmProviderOption {
  /** 归一化后的供应商名；进请求体 `llm_provider` 的就是它（`deepseek` / `zai`） */
  name: string;
  /** 胶囊上显示的短名（`DeepSeek` / `GLM`） */
  label: string;
  /** 真实模型名，只用于 tooltip（`deepseek-v4-pro` / `glm-4.7-flashx`） */
  model: string;
}

export interface HealthInfo {
  status: string;
  collection: string;
  qdrant_mode: string;
  qdrant_points: number;
  bm25_docs: number;
  embedding_model: string;
  vector_size: number;
  /**
   * **服务端默认**那家的模型名（`.env` 的 `LLM_PROVIDER`）。
   *
   * ⚠ 它**不是**「这次提问实际用了哪家」——那个由用户在胶囊上选，只存在于请求体里
   * （`ChatRequest.llm_provider`），健康检查看不见、也不该看见。与 `web_configured`
   * 那条限制同源。
   */
  llm_model: string;
  llm_ready: boolean;
  /** 服务端默认的供应商名（已归一化，如 `deepseek`）。胶囊没选时显示的就是它 */
  llm_provider: string;
  /**
   * 可选供应商列表，**只含真配了 Key 的**（后端 `available_providers()` 过滤过）。
   *
   * 少于 2 个时前端**不渲染**那个胶囊 —— 只有一个选项时没有可选的，
   * 不摆一个点不动的死按钮。
   */
  llm_providers: LlmProviderOption[];
  rerank_enabled: boolean;
  rerank_model: string;
  /** 是否配置了 NEO4J_URI（配了才谈得上可用不可用） */
  graph_configured: boolean;
  /** 图谱当前是否可用——为 false 只影响 Agent 模式的图谱工具，问答不受影响 */
  graph_available: boolean;
  /** 库名，或不可用的原因 */
  graph_detail: string;
  /** 是否配置了 POSTGRES_HOST */
  pg_configured: boolean;
  /** 采购记录库当前是否可用——为 false 只影响 Agent 模式的数据库工具，问答不受影响 */
  pg_available: boolean;
  /** 库名，或不可用的原因 */
  pg_detail: string;
  /** 是否配置了 ZHIPU_API_KEY（配了才谈得上可用不可用） */
  vision_configured: boolean;
  /**
   * 图片解析当前是否可用。
   *
   * ⚠ 它**不是网络探测的结果**：健康检查每 30 秒轮询一次，不可能每次真调一次视觉 API。
   * 服务端读的是「配了 Key + 装了 SDK」以及最近一次真实调用的成败，所以刚起服务时是乐观的。
   */
  vision_available: boolean;
  /** 模型名，或不可用的原因 */
  vision_detail: string;
  vision_model: string;
  /**
   * 是否配置了 TAVILY_API_KEY（配了才谈得上可用不可用）。
   *
   * ⚠ 这里只能报告**服务端配没配 Key**。用户这次开没开前端那个「联网搜索」开关
   * 只存在于请求体里（`ChatRequest.web_search`），健康检查看不见，也不该看见。
   */
  web_configured: boolean;
  /**
   * 联网搜索当前是否可用。
   *
   * ⚠ 同 `vision_available`：**不是网络探测的结果**（健康检查每 30 秒轮询一次，
   * 不可能每次真调一次付费搜索）。服务端读的是「配了 Key」以及最近一次真实调用的
   * 成败，所以刚起服务时是乐观的。附件按钮因此不按它置灰，联网开关同理。
   */
  web_available: boolean;
  /** 「已配置」或不可用的原因（未配 Key / 最近一次失败 / 冷却中） */
  web_detail: string;
  /** 是否配置了 POSTGRES_HOST（对话历史与采购记录库共用这一个开关） */
  history_configured: boolean;
  /**
   * 对话历史当前是否可用。
   *
   * ⚠⚠ **前端的「未同步」提示不要读这三个字段**，要读 `listConversations()` 那一次调用的
   * 成败（`useChat` 的 `serverMode` / `historySynced`）。理由：健康检查在服务初始化期间是
   * **503**，而**那时历史其实是好的**——对话历史刻意不被 `_require_ready()` 挡住，正是为了
   * 让首屏历史在索引加载的那几十秒里也能出。按 health 判断会让侧栏在那几十秒里谎报
   * 「未同步」，而它是**每 30 秒**轮询一次的，恢复得还比真实情况慢。
   * 这三个字段只用来解释「为什么降级了」（配没配、什么错），不参与判定。
   */
  history_available: boolean;
  /** 库名，或不可用的原因 */
  history_detail: string;
}
