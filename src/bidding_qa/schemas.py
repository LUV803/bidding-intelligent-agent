"""API 请求 / 响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str


class Source(BaseModel):
    """一条参考来源。

    绝大多数来自**知识库**（一个问答对）；开了「联网搜索」时也可能是**一条网页结果**
    （见 `origin` / `url`）。两种来源在正文里共用同一套 [n] 编号、同一个「参考来源」面板，
    所以合成一个模型——它们对模型和用户而言确实是同一种东西：【资料】。

    ⚠ 新增的 `url` / `origin` / `published_date` **恒定出现在 `sources` 事件载荷里**
    （知识库条目是 `null` / `"kb"` / `""`）。因此「不传新字段时事件逐字节相同」这条不变量
    要按「除这三个恒定新增字段外，index/顺序/分数逐字节相同」来理解。
    """

    index: int = Field(description="引用序号，正文中以 [index] 标注")
    doc_id: str
    question: str = Field(description="知识库中的原始问题")
    answer: str = Field(description="知识库中的完整答案")
    snippet: str = Field(description="与提问最相关的答案片段")
    highlights: list[str] = Field(default_factory=list, description="片段中命中的关键词")
    score: float = Field(description="归一化后的融合得分 (0~1)")
    rrf_score: float = 0.0
    vector_score: float | None = None
    bm25_score: float | None = None
    rerank_score: float | None = Field(default=None, description="重排模型给出的相关性概率 (0~1)")
    rank_vector: int | None = None
    rank_bm25: int | None = None
    rank_fused: int | None = Field(default=None, description="重排前的融合名次，用于对比重排带来的位次变化")
    matched_by: list[str] = Field(default_factory=list, description="命中该来源的检索通道")
    # ---- 联网搜索结果专用（知识库条目为默认值）----
    url: str | None = Field(default=None, description="网页原文链接；知识库条目为 None")
    origin: Literal["kb", "web"] = Field(
        default="kb",
        description="来源种类：kb=知识库问答对，web=联网网页结果。前端据此切换文案与字段",
    )
    # ⚠ 保持 str：Tavily 回的就是字符串，**别当 date 处理**——`date` 不是 JSON 可序列化的，
    #   会在 api.py 那句 json.dumps 处直接炸（PgResult 的 Decimal/date 是同一个坑，来回三次了）。
    published_date: str = Field(default="", description="网页的发布时间；知识库条目为空串")


class GraphRow(BaseModel):
    """图谱查询结果里的一行：一个实体，以及它在本次查询下的统计。"""

    label: str = Field(description="实体名称（供应商名 / 标的物名 / 地区名…）")
    count: int = Field(default=0, description="涉及该实体的交易笔数")
    amount: float | None = Field(default=None, description="涉及金额；非金额类查询为 None")


class GraphResult(BaseModel):
    """一次知识图谱查询的结构化结果。

    **刻意不走 Source 那套**：Source 是问答对形状（question/answer/snippet），图谱结果
    是「实体 + 统计」。硬塞进 SourceRegistry 会得到一个语义别扭的假问答对，所以图谱结果
    只回传给模型 + 在「知识图谱查询」块里单独渲染，**不参与正文的 [n] 引用编号**。

    用 pydantic 而非 dataclass 是硬要求：这个对象要经 api.py 的 json.dumps 上线，
    dataclass 会在那一步直接抛 TypeError。
    """

    query_type: str = Field(description="查询类型标识")
    query_label: str = Field(description="查询类型的中文语义，前端直接显示这句")
    ok: bool = Field(default=True, description="查询本身是否成功；False 时 note 里是失败原因")
    elapsed_ms: float = Field(default=0.0, description="本次图谱查询耗时")

    entity: str = Field(default="", description="模型给出的原始关键词")
    resolved: str | None = Field(default=None, description="模糊匹配后实际命中的实体名")
    candidates: list[str] = Field(
        default_factory=list, description="其他候选实体；命中多个时列出，供模型换个更精确的名字重查"
    )
    total: int = Field(default=0, description="命中总数，可能大于 len(rows)")
    total_capped: bool = Field(
        default=False,
        description="total 只是下限（取数撞上了服务端硬上限）。为真时不能说「一共 N 条」",
    )
    rows: list[GraphRow] = Field(default_factory=list, description="明细，已按 graph_max_rows 截断")
    truncated: bool = Field(default=False, description="明细是否因超过上限被截断")
    note: str | None = Field(default=None, description="「没找到」「关键词太宽泛」等说明")
    # 地区分布等不适合塞进 rows 的附加统计，原样带给前端和模型
    extra: dict[str, float] = Field(default_factory=dict, description="附加数值统计")


class PgRow(BaseModel):
    """采购记录查询结果里的一行。

    **一个形状覆盖两种模式**（明细 / 分组），需要哪个就看 `PgResult.mode`：
    分组的行填 label/count/amount，明细的行填其余字段。合成一个模型是为了让前端沿用
    GraphResultBlock 那套「哪些字段非空就显示哪些」的渲染策略，不用写两套表。
    """

    # 分组模式（mode="group"）
    label: str = Field(default="", description="分组名（城市 / 类别 / 采购人名…）")
    count: int | None = Field(default=None, description="该组的记录数")
    amount: float | None = Field(default=None, description="该组的中标金额合计（元）")

    # 明细模式（mode="detail"）
    title: str = ""
    project: str = ""
    purchaser: str = ""
    supplier: str = ""
    published: str = Field(default="", description="发布时间，ISO 字符串")
    region: str = Field(default="", description="省份+市区+县城 拼成的地区串")
    subject: str = Field(default="", description="标的物")
    category: str = ""
    source: str = Field(default="", description="来源：政府采购 / 企业采购 / 招标采购")


class PgResult(BaseModel):
    """一次采购记录查询的结构化结果。

    与 GraphResult 一样**刻意不走 Source 那套**：这里是「项目 + 金额 + 时间」的形状，
    塞进 SourceRegistry 会得到一个语义别扭的假问答对，也会被正文的 [n] 误引。
    因此只回传给模型 + 在「采购数据库查询」块里单独渲染。

    用 pydantic 是硬要求（同 GraphResult）：对象要经 api.py 的 json.dumps 上线。
    ⚠ 更要紧的是**字段类型**：`numeric` 取回来是 Decimal、`date` 是 datetime.date，
    两者都不是 JSON 可序列化的，会在 json.dumps 那一步抛 TypeError。
    pg_store 在构造本对象时就把它们转成 float / ISO 字符串了。
    """

    query_label: str = Field(description="服务端算好的中文标题，前端直接显示这句")
    ok: bool = Field(default=True, description="查询本身是否成功；False 时 note 里是失败原因")
    elapsed_ms: float = Field(default=0.0, description="本次查询耗时")
    mode: Literal["detail", "group"] = Field(
        default="detail", description="指明 rows 里哪些字段有值"
    )
    filters: dict[str, str] = Field(
        default_factory=dict,
        description="实际生效的筛选条件。给用户看「它到底查了什么」——模型把「安徽」理解成"
        "省份筛选、把「过亿」理解成金额下限，这些理解偏差必须暴露出来",
    )
    total: int = Field(default=0, description="count(*) 精确总数，不是下限")
    rows: list[PgRow] = Field(default_factory=list, description="明细，已按 pg_max_rows 截断")
    truncated: bool = Field(default=False, description="rows 是否因超过展示上限被截断")
    note: str | None = Field(default=None, description="「没找到」、非法参数、失败原因等说明")
    extra: dict[str, float] = Field(default_factory=dict, description="附加数值统计")


class WebRow(BaseModel):
    """Tavily 返回的一条网页结果。

    ⚠ 字段类型是照 Tavily 实际返回定的，别想当然：`score` 是 float，而
    `published_date` 是**字符串**（可能为空串），不是 date。若当 date 处理，
    会在 api.py 的 json.dumps 处炸成 TypeError——与 PgResult 那条是同一个坑。
    """

    title: str = ""
    url: str = ""
    content: str = Field(default="", description="Tavily 抽取的相关片段（已按 web_content_max_chars 截断）")
    score: float = Field(default=0.0, description="Tavily 的相关性评分，results 已按它降序")
    published_date: str = Field(default="", description="发布日期的字符串形式，可能为空")


class WebResult(BaseModel):
    """一次联网搜索的结构化结果。

    与 GraphResult / PgResult 不同，**它不单独渲染成块**——网页结果已经变成带 [n] 编号的
    `Source` 并入了「参考来源」面板，再画一遍就是同一份数据展示两次。这个模型只用于
    工具回执（tool_result）与探针断言。

    用 pydantic 是硬要求（同 GraphResult / PgResult）：对象要经 api.py 的 json.dumps 上线。
    """

    ok: bool = Field(default=True, description="搜索本身是否成功；False 时 note 里是失败原因")
    query: str = Field(default="", description="实际发出的查询词")
    rows: list[WebRow] = Field(default_factory=list)
    total: int = Field(default=0, description="本次返回的结果条数")
    elapsed_ms: float = 0.0
    note: str | None = Field(default=None, description="失败原因或降级说明")


class VisionRequest(BaseModel):
    """图片解析请求。图片以 **裸 base64** 传输（不带 `data:image/...;base64,` 前缀）。

    为什么走 JSON 而不是 multipart：这样前端与后端各自都不用引入新依赖
    （`python-multipart` 是为 `UploadFile` 准备的前置），且与项目其余接口一致。
    前缀这一层歧义**在前后端边界上就消掉**：前端 `FileReader` 拿到 data URI 后先切掉前缀
    再发。实测智谱 SDK 自己也会剥前缀（见 `vision.py` 注释），所以两种形态其实都能跑；
    `/api/vision` 对带前缀的形态**仍然兼容**，只是会记一条 WARNING——为一个纯装饰性的
    差异返回 400，对手搓请求的人毫无帮助。
    """

    image_base64: str = Field(description="裸 base64 字符串，不含 data URI 前缀")
    media_type: str = Field(default="image/png", description="MIME 类型，仅作日志与提示用")


class VisionResult(BaseModel):
    """图片解析结果。

    `fields` 用字典而不是一堆具名属性：字段集由 `vision.VISION_FIELDS` 的提示词定义、
    会随需求变，具名属性每加一个就要同时改后端模型 / `types.ts` / 前端渲染块三处。
    前端按插入顺序渲染成键值网格，所以**后端必须保证顺序稳定**。

    ⚠ 这里的值**必须全是 `str`**：模型有可能把金额返回成数字，`vision.py` 会统一
    转成字符串。理由与 `PgResult` 那条一样——`api.py` 的事件载荷走 `json.dumps`，
    非基本类型会在那里炸。
    """

    ok: bool = True
    doc_type: str = Field(default="", description="文档类型，如「招标公告」；认不出时为「其他」")
    fields: dict[str, str] = Field(default_factory=dict, description="结构化字段，顺序即展示顺序")
    summary: str = Field(default="", description="一两句话的摘要，用于输入框旁的附件 chip")
    full_text: str = Field(default="", description="识别出的全文，折叠区展示")
    elapsed_ms: float = 0.0
    note: str | None = Field(default=None, description="失败原因或降级说明")


class ConversationMeta(BaseModel):
    """一条对话的**元数据**（不含消息正文）。

    列表与搜索都回这个形状。⚠ **只回元数据是刻意的**：单条对话的正文可达几百 KB
    （12 条 `sources`、`steps[].pg` 整行原文、`reasoning` 全文、`image.full_text`），
    而侧栏只需要标题和时间。首屏把正文全拉下来，「历史秒出」这个卖点就没了。
    正文由 `GET /api/conversations/{id}` 按需取。

    时间戳是 **epoch 毫秒**，与前端 `Conversation.createdAt` / `updatedAt` 同一口径 ——
    转换（`timestamptz` ↔ 毫秒）全部发生在 `conv_store` 内部，**别让它漏到路由层**。
    """

    id: str
    title: str = ""
    created_at: int = Field(description="epoch 毫秒")
    updated_at: int = Field(description="epoch 毫秒")
    message_count: int = Field(default=0, description="消息条数，侧栏用来判断这条对话是否为空")


class ConversationFull(ConversationMeta):
    """一条对话的完整内容。

    `messages` 是前端 `Message` 对象的**原样数组**，后端**不解释**它内部的结构 ——
    理由见 `ConversationUpsert.messages`。
    """

    messages: list[dict[str, Any]] = Field(default_factory=list)


class ConversationHit(ConversationMeta):
    """一条搜索结果：元数据 + 命中片段。"""

    snippet: str = Field(default="", description="命中位置附近的正文片段，服务端已切好")
    snippet_role: str = Field(
        default="",
        description="片段来自哪一侧（user / assistant）；只在标题里命中时为空串",
    )
    hit_message_id: str = Field(
        default="",
        description=(
            "命中的那条消息的 id —— 前端据此滚到它并高亮关键词。"
            "**只在标题里命中时为空串**（那种情况下没有「哪一条消息」可言）"
        ),
    )


class ConversationUpsert(BaseModel):
    """`PUT /api/conversations/{id}` 的请求体（**整体替换**）。

    ⚠ `messages` 用 `list[dict[str, Any]]` 而不是严格的 `Message` 模型，**是刻意的**：
    严格模型会把前端以后新增的字段**静默丢掉** —— 那正是「用 jsonb 存整个对象」
    要避免的事，而且直到有人问「为什么这个功能刷新后没了」才会被发现。
    后端不解释消息内容，原样进 `payload`。

    ⚠ 请求体是 JSON 解析出来的，所以里面**不会有 `Decimal` / `date`**，
    回程那句 `json.dumps` 安全（这是 `PgResult` 那个坑的反面）。
    """

    title: str = ""
    created_at: int = Field(description="epoch 毫秒")
    updated_at: int = Field(description="epoch 毫秒")
    messages: list[dict[str, Any]] = Field(default_factory=list)


class FeedbackUpsert(BaseModel):
    """`PUT /api/feedback/{conversation_id}/{message_id}` 的请求体。

    快照（`question` / `answer` / `doc_ids`）由**前端**给。后端不去 `payload` 里翻 ——
    那就是 `ConversationUpsert` 上面那条「不解释消息内部结构」要避免的事。

    ⚠ `rating` 声明成 `int` 而**不是** `Literal[1, -1]`：非法值要走到路由里那句
    **中文** 400（「rating 只能是 1（👍）或 -1（👎）」），而不是 pydantic 的 422 英文报错。
    与「搜索词为空 → 400『搜索词不能为空』」是同一条惯例。
    """

    rating: int = Field(description="1 = 👍，-1 = 👎")
    question: str = ""
    answer: str = ""
    doc_ids: list[str] = Field(default_factory=list)


class FeedbackRow(BaseModel):
    """`GET /api/feedback` 的一行。

    ⚠ 时间戳是 **epoch 毫秒的 int**，不是 `datetime` —— 转换发生在 `conv_store` 内部
    （`_fb()`），漏到这里就是 `api.py` 那句 `json.dumps` 炸成 `TypeError`（第五次）。
    """

    conversation_id: str
    message_id: str
    rating: int
    question: str = ""
    answer: str = ""
    doc_ids: list[str] = Field(default_factory=list)
    created_at: int = Field(description="epoch 毫秒")
    updated_at: int = Field(description="epoch 毫秒")


class ChatRequest(BaseModel):
    question: str
    session_id: str | None = None
    history: list[ChatMessage] = Field(default_factory=list)
    top_k: int | None = None
    hybrid: bool = True
    use_llm: bool = True
    # None 表示沿用服务端配置（RERANK_ENABLED）
    rerank: bool | None = None
    # 图片解析结果，作为这次提问的上下文附给模型。
    # ⚠ **只进提示词，不进检索**——见 rag.with_image_context 的注释，
    #   拼进 question 去检索会让向量被稀释、BM25 满屏乱命中。
    # 空串时整条链路与不加这个字段**逐字节相同**。
    image_context: str = ""
    # 联网搜索开关（前端「联网搜索」按钮）。False/缺省时整条链路与改动前一致；
    # 还需要服务端配了 TAVILY_API_KEY 才真正生效（web_search.ENABLED），
    # 两者同时为真才挂工具——**这是唯一一个「每请求可变」的可选能力**。
    # ⚠ 开启时**两条链路都会在作答之前无条件联网搜一次**（用原问题），本地答得出也照搜：
    #   `/api/chat` 与 `/api/agent` 都是「先本地检索 → 再联网 → 两份一起进【资料】」。
    #   2026-09 之前是「答不出才搜」的兜底语义，别再按那个理解这段代码。
    web_search: bool = False
    # 这次提问用哪家文本大模型（前端那个胶囊）。**空串 = 用服务端默认**
    # （`settings.llm_provider`，即 `.env` 的 `LLM_PROVIDER`），此时整条链路与不加这个
    # 字段**逐字节相同**。
    # ⚠ 这是第二个「每请求可变」的维度，但**与 web_search 不同源**：那个还要服务端配了
    #   Key 才生效（两维取交集），这个只要名字认识就照办 —— 名字合法但没配 Key 时
    #   **如实报未配置**，绝不悄悄换成另一家（静默换模型 = 用户拿到他没要的模型的答案）。
    #   前端只会列出真配了 Key 的（`/api/health` 的 `llm_providers`），所以正常点选
    #   碰不到那种；能碰到的只有手写的请求体。
    llm_provider: str = ""


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None
    mode: Literal["hybrid", "vector", "bm25"] = "hybrid"
    rerank: bool | None = None


class SearchResponse(BaseModel):
    query: str
    mode: str
    sources: list[Source]
    latency_ms: dict[str, float]


class HealthResponse(BaseModel):
    status: str
    collection: str
    qdrant_mode: str
    qdrant_points: int
    bm25_docs: int
    embedding_model: str
    vector_size: int
    llm_model: str
    llm_ready: bool
    # 文本大模型也是**两维**（同 web_search 那条先例）：`llm_provider` 是**服务端默认**
    # （`.env` 的 `LLM_PROVIDER`），而「这次用哪家」只存在于请求体里（`ChatRequest.llm_provider`），
    # 健康检查看不见、也不该看见。所以 `llm_model` / `llm_ready` 报的**只是默认那家**。
    llm_provider: str
    # 前端胶囊的选项列表：[{name,label,model}]，**只含真配了 Key 的**（`ready` 为真）。
    # 少于 2 个时前端不渲染那个胶囊 —— 只有一个选项时没有可选的，不摆一个点不动的死按钮。
    llm_providers: list[dict[str, str]]
    rerank_enabled: bool
    rerank_model: str
    # 知识图谱是可选能力：available=False 只说明图谱工具不可用，问答链路不受影响
    graph_configured: bool
    graph_available: bool
    graph_detail: str
    # 采购记录库同理：连不上只是这个工具不注册，问答照常
    pg_configured: bool
    pg_available: bool
    pg_detail: str
    # 图片解析同理：没配 Key 只是附件按钮不可用，问答与其召回完全不受影响。
    # ⚠ available **不代表做过网络探测**——健康检查每 30 秒轮询一次，不可能每次都真调一次
    #   视觉 API（既慢又花钱）。它读的是「配了 Key」+「最近一次真实调用没失败」，见 vision.py。
    vision_configured: bool
    vision_available: bool
    vision_detail: str
    vision_model: str
    # 联网搜索同理，但**多一维**：服务端配没配 Key（configured）与用户这次开没开开关
    # 是两件事，这里只能报告前者。开关状态只存在于请求体里。
    # ⚠ 与 vision 一样 available **不代表做过网络探测**（健康检查每 30 秒一次，
    #   不可能每次真搜一次），读的是「配了 Key」+「最近一次真实调用没失败」。
    web_configured: bool
    web_available: bool
    web_detail: str
    # 对话历史持久化同理：连不上只是历史不落库（前端退回 localStorage 并如实提示），
    # 问答链路完全不受影响。
    # ⚠ 前端的「未同步」提示**不要读这三个字段**，要读列表接口的成败：健康检查在服务
    #   初始化期间是 503，而那时历史其实是好的（历史刻意不被 _require_ready 挡住）。
    #   这三个字段只用来解释「为什么降级了」。
    history_configured: bool
    history_available: bool
    history_detail: str


class ConfigResponse(BaseModel):
    llm_model: str
    embedding_model: str
    vector_size: int
    top_k: int
    hybrid: bool
    rerank: bool
    rerank_model: str
    qdrant_mode: str
    collection: str
