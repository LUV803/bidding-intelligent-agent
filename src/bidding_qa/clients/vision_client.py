"""智谱视觉模型客户端：把用户上传的图片解析成结构化字段。

⚠ **它不是 Agent 工具**（这一点最容易被下一个人改错）。解析发生在**上传那一刻**，
早于提问，也不由模型决定要不要调——与 `graph_store` / `pg_store` 那两把工具是**不同形状**
的东西。做成工具会引入 `_match_schema` 投票、`_PARAM_ALIASES` 撞名、`recover_tool_calls`
归属等一整套新风险，而需求里描述的本来就是「上传 → 解析 → 展示」。详见 CLAUDE.md。

⚠ **它也不属于 `BaseLLMClient` 那套契约**（2026-09-20 移入 `clients/` 时定的）：
那边是「异步 + 流式 + `stream()` 产 `(kind, text)`」，这边是「同步 + 一次调用 +
结构化 `VisionResult`」，而且走的是 `zai-sdk` 而不是 `openai`。硬套一个基类只会造出
每次都要 `raise NotImplementedError` 的假抽象。放在同一个包里是因为**它确实是个客户端**，
不是因为形状一样。

与前两把工具同构的是**可选能力**这条性质：没配 `ZHIPU_API_KEY` 时 `analyze()` 抛
`VisionNotConfiguredError`（接口层转成 503 中文提示），问答链路**完全不受影响**。

**不做 warmup**（与 `graph_store.warmup()` 刻意不同）：视觉能力没有任何「冷连接」成本
（不像 Neo4j 首查要付 2 秒握手），加预热纯属多一次网络往返、多花一次钱，还会在启动日志里
制造一条无意义的失败。别「补全」它。

依赖：`uv add zai-sdk`（PyPI 包名是 `zai-sdk`，导入名才是 `zai`，别写 `import zai_sdk`）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from functools import lru_cache
from typing import Any

from ..config import settings
from ..schemas import VisionResult

logger = logging.getLogger(__name__)

# 提示词要求模型输出的结构化字段。**这个元组的顺序就是前端的展示顺序**，
# 前端按 `fields` 的插入顺序渲染键值网格，所以 `_to_fields()` 按它取值而不是按
# 模型返回的键序——模型偶尔会打乱顺序，那会让同一份文档两次解析出来的卡片不一样。
#
# 另三个字段（文档类型 / 全文 / 摘要）不在这里：它们被提升成 VisionResult 的一级字段，
# 前端有专门的落点（徽章 / 折叠区 / 附件 chip 副标题）。
VISION_FIELDS: tuple[str, ...] = (
    "项目名称",
    "项目编号",
    "采购人",
    "代理机构",
    "预算金额",
    "中标金额",
    "发布时间",
    "投标截止",
    "标的物",
    "关键条款",
)

# 提升成一级字段的那三个，收额外字段时要排掉，否则卡片上会重复出现
_HOISTED_FIELDS = frozenset({"文档类型", "全文", "摘要"})

# 全文上限。它是给模型当上下文用的，也会随 Message.image 存进浏览器 localStorage，
# 不能无限长。超了截断——截断处给一句明确说明，不做静默截断。
_FULL_TEXT_MAX = 6000

# 关键条款单独再宽一点，它是后续追问的钩子（保证金比例、评标办法、工期、资质要求）
_KEY_CLAUSE_MAX = 1500

_PROMPT = """你是招投标文档识别助手。请识别这张图片，**只输出一个 JSON 对象**，不要任何解释、不要 markdown 代码块。

JSON 的键固定为下面这些，一个都不能少，顺序也照这个来：

{
  "文档类型": "招标公告 / 中标公告 / 采购公告 / 资格预审公告 / 合同 / 表格 / 其他，认不出就填「其他」",
  "项目名称": "...",
  "项目编号": "...",
  "采购人": "...",
  "代理机构": "...",
  "预算金额": "...",
  "中标金额": "...",
  "发布时间": "...",
  "投标截止": "...",
  "标的物": "...",
  "关键条款": "把图中与保证金、评标办法、工期、资质要求、付款方式有关的**原文句子**摘出来，每句一行",
  "全文": "图中所有可见文字的完整转录",
  "摘要": "一到两句话概括这张图片是什么"
}

规则：
1. 图中没有的字段填**空字符串**，不要写「无」「未知」「未提供」「N/A」。
2. **逐字转录**，不要改写、不要估算、不要补充图中没有的信息。金额与日期保持图中原样（含单位与格式）。
3. 图片若不是招投标相关文档，文档类型填「其他」，其余字段按图里的实际情况填，不要硬凑。
4. 只输出这个 JSON 对象本身。"""


class VisionNotConfiguredError(RuntimeError):
    """没配 Key / 没装 SDK。**这是唯一会被抛出来的异常**，其余失败一律退化成
    `ok=False` 的 VisionResult（同 `reranker` / `graph_store` 的「降级而非报错」）。"""


_client: Any = None
_client_lock = threading.Lock()
# 最近一次**真实调用**的结果。只给 vision_status() 做诊断用，不参与任何控制流
# （没有 graph_store 那样的冷却：这里没有「连接」可建，每次请求本来就要打一次网络）。
_last_error: str | None = None
_last_ok_at: float | None = None


def is_configured() -> bool:
    """配了 Key。**不做网络探测**——同 `llm.ready`。"""
    return bool(settings.zhipu_api_key.strip())


@lru_cache(maxsize=None)
def _has_module(name: str) -> bool:
    """这个进程里装没装某个包。**结果缓存**，不每次现查。

    ⚠ `importlib.util.find_spec` 每次都实打实走一遍 `sys.path` + `os.stat`，实测约
    **430µs** —— 四个 status 函数各调一次，那就是 `/api/health` 的全部开销（~1.8ms）。
    健康检查每 30 秒被轮询一次，而**装包这件事在一个进程的生命周期里不会变**，
    所以缓存它没有代价。`probe_*` 全离线段不依赖它的实时性（没有任何探针打桩 find_spec）。
    ⚠ `probe_vision.py` 会重置 `_client` 单例来验「未配置」路径，但那走的是
    `is_configured()`（读 Key），不经过这里 —— 不受缓存影响。

    ⚠ 与 `llm_factory.available_providers()` 读活 `settings` **不冲突**：那边缓存不得，
    是因为 settings 会在运行期被改（探针就靠改它验行为）；这边没有对应的场景。
    """
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _get_client():
    """懒建客户端。未配置时抛 VisionNotConfiguredError（由接口层转成 503）。

    ⚠ `_client` 是**模块全局**，所以 `bidding_qa/vision.py` 那个转发模块**转不动它**
    （转发模块与真模块是两个不同的模块对象，往转发模块上赋值只会写进它自己的命名空间）。
    探针要重置单例请 `from bidding_qa.clients import vision_client as vision` 后再赋值。
    """
    global _client

    if _client is not None:
        return _client

    key = settings.zhipu_api_key.strip()
    if not key:
        raise VisionNotConfiguredError(
            "未配置 ZHIPU_API_KEY，图片解析不可用。请在 .env 中填写后重试。"
        )

    if not _has_module("zai"):
        raise VisionNotConfiguredError(
            "未安装 zai-sdk，图片解析不可用。请先执行 uv add zai-sdk。"
        )

    with _client_lock:
        if _client is None:
            from zai import ZhipuAiClient

            # ⚠ api_key 必须**显式传**：SDK 自己认的环境变量是 ZAI_API_KEY，
            #   与 .env 里的 ZHIPU_API_KEY 不是一个名字，靠环境变量是取不到的。
            # base_url 用智谱开放平台那个（https://open.bigmodel.cn/api/paas/v4）；
            # SDK 里另一个 ZaiClient 指向 api.z.ai，是另一条线路。
            _client = ZhipuAiClient(
                api_key=key,
                base_url=settings.zhipu_base_url.strip().rstrip("/"),
                timeout=settings.zhipu_vision_timeout,
            )
            logger.info("视觉模型客户端已就绪：%s", settings.zhipu_vision_model)
    return _client


def _call(client: Any, image_base64: str) -> str:
    """发一次视觉请求，返回正文。

    **图片走裸 base64、不带 `data:image/...;base64,` 前缀。** 实测结论（见
    `scripts/probe_vision.py`）：SDK 内部无论如何都会调 `drop_prefix_image_data()`
    把前缀剥掉再发，所以两种形态其实都能通——但只让一种穿过前后端边界，
    省得日后有人对着请求体猜。这里写死裸 base64。
    """
    resp = client.chat.completions.create(
        model=settings.zhipu_vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    # 图片在前、文字在后：与官方示例一致
                    {"type": "image_url", "image_url": {"url": image_base64}},
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
        # ⚠ glm-4.6v-flashx 是**推理型**模型，会先吐思维链再出正文，思维链同样计入
        #   max_tokens。本项目在 batch 那边踩过同一个坑（batch/run_batch.py 的注释）。
        #   给少了的表现是「图传上去了，返回空」——最难查的那种。
        max_tokens=settings.zhipu_vision_max_tokens,
        temperature=0.1,
        # 这是本模块唯一的超时，调用方不另设（同 pg_store 的 statement_timeout）
        timeout=settings.zhipu_vision_timeout,
    )
    msg = resp.choices[0].message
    return getattr(msg, "content", None) or ""


def _unwrap_fence(text: str) -> str:
    """剥掉 ```json 围栏。

    **不是防御性代码，是实测必需**：提示词里写明了「不要 markdown 代码块」，
    `glm-4.6v-flashx` 两次调用**仍然**都包了围栏（见 scripts/probe_vision.py 的输出）。
    """
    raw = text.strip()
    if not raw.startswith("```"):
        return raw
    parts = raw.split("```")
    if len(parts) >= 2:
        return parts[1].removeprefix("json").removeprefix("JSON").strip()
    return raw


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。抠不出来返回 None（调用方降级，不报错）。"""
    raw = _unwrap_fence(text or "")
    # 两轮：先试剥过围栏的，再试原样的。围栏剥离在「全文」里本身含 ``` 时会截断，
    # 那时原样的这轮还能救回来。
    for candidate in (raw, (text or "").strip()):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(candidate[start : end + 1])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _as_text(value: Any) -> str:
    """把模型给的任意 JSON 值转成字符串。

    ⚠ **必须转，且必须在这里转**：模型有时把金额返回成数字、把关键条款返回成数组。
    非 str 的值会一路走到 `api.py` 的 `json.dumps` 那里抛 TypeError——与 `PgResult`
    不能留 `Decimal` / `date` 是同一个坑（CLAUDE.md 有专条）。
    """
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, bool):  # 必须排在 int 前面：bool 是 int 的子类
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(t for t in (_as_text(v) for v in value) if t)
    if isinstance(value, dict):
        pairs = [(str(k), _as_text(v)) for k, v in value.items()]
        return "\n".join(f"{k}：{v}" for k, v in pairs if v)
    return str(value).strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文另有 {len(text) - limit} 字）"


def _to_fields(data: dict[str, Any]) -> dict[str, str]:
    """把模型的 JSON 转成 `fields`。

    顺序按 `VISION_FIELDS`（稳定），模型多给的键排在后面，那三个被提升的键排掉。
    空值直接丢掉——提示词要求「没有的填空字符串」，留着会让卡片上一堆空行。
    """
    out: dict[str, str] = {}
    for key in VISION_FIELDS:
        text = _as_text(data.get(key))
        if text:
            out[key] = _clip(text, _KEY_CLAUSE_MAX) if key == "关键条款" else text
    for key, value in data.items():
        if key in out or key in _HOISTED_FIELDS or key in VISION_FIELDS:
            continue
        text = _as_text(value)
        if text:
            out[str(key)] = text
    return out


def _friendly(exc: Exception) -> str:
    """把 SDK 异常翻成用户看得懂的中文提示。"""
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if any(k in low for k in ("401", "invalid api key", "unauthorized", "authentication", "鉴权")):
        return f"{text}\n提示：ZHIPU_API_KEY 可能无效或已过期，请到 https://open.bigmodel.cn/ 核对。"
    if any(k in low for k in ("429", "rate limit", "quota", "insufficient", "余额", "欠费")):
        return (
            f"{text}\n提示：可能是额度或频率限制。当前模型 {settings.zhipu_vision_model} 是付费档"
            "（识一张图约 0.001 元），可在 .env 里把 ZHIPU_VISION_MODEL 改成免费档 "
            "glm-4.6v-flash 后重试。"
        )
    if any(k in low for k in ("timeout", "timed out", "超时")):
        return (
            f"{text}\n提示：解析超时（当前上限 {settings.zhipu_vision_timeout:.0f} 秒），"
            "大图或网络慢时可调大 ZHIPU_VISION_TIMEOUT。"
        )
    return text


def analyze(image_base64: str, media_type: str = "image/png") -> VisionResult:
    """解析一张图片（裸 base64），返回结构化结果。

    **同步函数**：SDK 只有同步客户端（`httpx.Client`，没有 AsyncZhipuAI），所以调用方
    要用 `await asyncio.to_thread(vision.analyze, ...)` 包一层——这是本项目的 house idiom
    （api.py 里 retriever / graph_store 都是这么调的），且**不依赖 SDK 是否异步**。

    抛出：仅 `VisionNotConfiguredError`（没配 Key / 没装 SDK），由接口层转成 503。
    其余一切失败都退化成 `ok=False` + `note`，**绝不抛异常**。
    """
    global _last_error, _last_ok_at

    started = time.perf_counter()
    client = _get_client()  # 未配置时在这一句抛出，属于「能力不可用」而非「这次解析失败」

    def _elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    logger.info(
        "开始解析图片：%s 字节的 %s，模型 %s",
        len(image_base64),
        media_type,
        settings.zhipu_vision_model,
    )

    try:
        text = _call(client, image_base64)
    except Exception as exc:  # noqa: BLE001 —— 刻意兜住所有异常，见 docstring
        _last_error = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning("图片解析失败（%.0fms）：%s", _elapsed(), _last_error)
        return VisionResult(ok=False, elapsed_ms=_elapsed(), note=_friendly(exc))

    if not text.strip():
        # 推理型模型把 max_tokens 全花在思维链上的典型表现。单独报出来，
        # 否则用户只看到「解析成功但什么都没有」，无从下手。
        _last_error = "模型返回空正文（可能是思维链吃光了 max_tokens）"
        logger.warning("图片解析返回空正文：模型 %s", settings.zhipu_vision_model)
        return VisionResult(
            ok=False,
            elapsed_ms=_elapsed(),
            note=(
                "模型没有返回任何内容。glm-4.6v-flashx 是推理型模型，思维链会先吃掉 "
                "max_tokens，请把 .env 里的 ZHIPU_VISION_MAX_TOKENS 调大后重试。"
            ),
        )

    _last_error = None
    _last_ok_at = time.monotonic()

    data = _extract_json(text)
    if data is None:
        # 降级而非报错：模型没守 JSON 格式时，至少把它的原始输出留下来，
        # 用户还能看到识别到了什么。不该让人对着一个红色错误干瞪眼。
        logger.warning("图片解析返回的不是 JSON，降级为纯文本（%d 字）", len(text))
        return VisionResult(
            ok=True,
            fields={},
            full_text=_clip(text, _FULL_TEXT_MAX),
            elapsed_ms=_elapsed(),
            note="模型未按 JSON 格式返回，已把识别原文放进「全文」。",
        )

    result = VisionResult(
        ok=True,
        doc_type=_as_text(data.get("文档类型")) or "其他",
        fields=_to_fields(data),
        summary=_as_text(data.get("摘要")),
        full_text=_clip(_as_text(data.get("全文")), _FULL_TEXT_MAX),
        elapsed_ms=_elapsed(),
    )
    logger.info(
        "图片解析完成：%s，%d 个字段，%.0fms",
        result.doc_type,
        len(result.fields),
        result.elapsed_ms,
    )
    return result


def vision_status() -> dict[str, Any]:
    """可用性摘要（诊断用，如 /api/health）。**不抛异常、不发起任何网络调用**。

    ⚠ `available` **不代表做过网络探测**：健康检查每 30 秒轮询一次，不可能每次都真调一次
    视觉 API（既慢又花钱）。它读的是「配了 Key + 装了 SDK」以及**最近一次真实调用的结果**——
    所以刚起服务时是乐观的 True，只有真失败过才会翻成 False，下一次成功再翻回来。
    """
    if not is_configured():
        return {"configured": False, "available": False, "detail": "未配置 ZHIPU_API_KEY"}
    if not _has_module("zai"):
        return {"configured": False, "available": False, "detail": "未安装 zai-sdk"}
    if _last_error is not None:
        return {
            "configured": True,
            "available": False,
            "detail": f"上次调用失败：{_last_error}",
        }
    if _last_ok_at is None:
        return {
            "configured": True,
            "available": True,
            "detail": f"就绪（{settings.zhipu_vision_model}，尚未调用过）",
        }
    return {
        "configured": True,
        "available": True,
        "detail": f"{settings.zhipu_vision_model}，最近一次调用成功",
    }
