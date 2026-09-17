"""独立验证智谱视觉链路：SDK 可用性 + base64 报文形态 + OCR 实效 + 结构化 JSON。

这一层不依赖 Qdrant/BM25/Neo4j/PostgreSQL，只需要一个 ZHIPU_API_KEY，可以单独跑。

**为什么要有这个脚本**（两条都是实测结论，不是推测）：

1. **base64 要不要带 `data:image/...;base64,` 前缀**——新旧两版智谱 SDK 都会在发送前
   调 `drop_prefix_image_data()` 把前缀**剥掉**（zai-sdk 见 `core/_utils/_utils.py:402`
   与 `api_resource/chat/completions.py:119,121`）。也就是说**两种形态都能用**，SDK 统一
   成裸 base64 发出。本脚本把两种都实调一遍钉住这个结论，免得日后有人「顺手统一格式」
   时把其中一种改坏还没人发现。

2. **`glm-4.6v-flashx` 是真实可用的模型名**，且是**推理型**（会先吐思维链再出正文）。
   本项目在 batch 那边踩过「思维链把 max_tokens 吃光」的坑（见 batch/run_batch.py 注释），
   所以这里特意断言**正文非空**——`max_tokens` 给小了的表现是「图传上去了，返回空」，
   最难查的那种。

⚠ 图片是**代码里现造的**（手写 5x7 点阵字体渲染数字，再自己拼 PNG），不依赖仓库里
任何图片文件，也不依赖 Pillow。渲染的是 "2024"，让模型读回来即可验证 OCR 真的在工作。

跑法：

    uv run python scripts/probe_vision.py
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import time
import zlib
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa.config import settings  # noqa: E402

_pass = 0
_fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _pass, _fail
    if ok:
        _pass += 1
        print(f"  [OK  ] {label}")
    else:
        _fail += 1
        print(f"  [FAIL] {label}")
    if detail and not ok:
        print(f"         {detail}")


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


# --------------------------------------------------------------------------
# 现造一张 PNG：5x7 点阵数字，放大 scale 倍，白底黑字
# --------------------------------------------------------------------------
_FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def make_png(text: str = "2024", scale: int = 14, margin: int = 20) -> bytes:
    """把数字渲染成 8 位灰度 PNG（colortype=0）。纯标准库，无第三方依赖。"""
    glyphs = [_FONT[c] for c in text if c in _FONT]
    gw, gh, gap = 5, 7, 1
    inner_w = len(glyphs) * (gw + gap) - gap
    width = inner_w * scale + margin * 2
    height = gh * scale + margin * 2

    # 全白，再把点阵处涂黑
    grid = [[255] * width for _ in range(height)]
    for gi, glyph in enumerate(glyphs):
        for ry, row in enumerate(glyph):
            for rx, ch in enumerate(row):
                if ch != "1":
                    continue
                x0 = margin + (gi * (gw + gap) + rx) * scale
                y0 = margin + ry * scale
                for y in range(y0, y0 + scale):
                    for x in range(x0, x0 + scale):
                        grid[y][x] = 0

    raw = b"".join(b"\x00" + bytes(row) for row in grid)  # 每行前缀 filter=0
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


_PROMPT = """请识别这张图片，只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块：
{"数字": "图片中的数字字符串，没有则填空字符串", "描述": "一句话描述图片内容"}"""


def _call(client, image_url: str, *, timeout: float) -> tuple[str, float]:
    """发一次视觉请求，返回 (正文, 耗时秒)。"""
    started = time.perf_counter()
    resp = client.chat.completions.create(
        model=settings.zhipu_vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
        # ⚠ 推理型模型的思维链会先吃掉 max_tokens，给少了正文就是空的
        max_tokens=settings.zhipu_vision_max_tokens,
        temperature=0.1,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - started
    msg = resp.choices[0].message
    return (msg.content or ""), elapsed


def _offline() -> None:
    """离线接线检查：不需要 API Key、不需要任何服务，纯断言代码形状。

    这一段最要紧的是**冻结路径不变量**：不传图时，`/api/chat` 与 `/api/agent` 的行为必须与
    加这个功能之前**逐字节相同**。判据就是 `with_image_context(q, "") is q` —— 它一旦不成立，
    后面所有「检索、提示词、SSE 事件序列都没变」的说法全部作废。
    """
    import json

    from bidding_qa.schemas import ChatRequest, Source, VisionResult
    from bidding_qa.rag import (
        AGENT_SYSTEM_PROMPT,
        _AGENT_IMAGE_RULES,
        build_agent_messages,
        build_messages,
        with_image_context,
    )
    from bidding_qa.vision import _extract_json, _to_fields

    q = "履约保证金最高限额是多少？"
    src = [
        Source(
            index=1,
            doc_id="d1",
            question="履约保证金的比例上限",
            answer="履约保证金不得超过中标合同金额的 10%。",
            snippet="不得超过中标合同金额的 10%",
            score=0.91,
        )
    ]

    # ---- 冻结路径不变量（本轮最该验的一条）----
    check("with_image_context(q, '') 原样返回同一个对象", with_image_context(q, "") is q)
    check("with_image_context(q, None) 原样返回同一个对象", with_image_context(q, None) is q)
    check(
        "不传图时 build_messages 逐字节相同",
        build_messages(with_image_context(q, ""), src, []) == build_messages(q, src, []),
    )
    check(
        "不传图时 build_agent_messages 逐字节相同",
        build_agent_messages(with_image_context(q, ""), src, [])
        == build_agent_messages(q, src, []),
    )

    # ---- 传图时的形状 ----
    wrapped = with_image_context(q, "文档类型：招标公告\n预算金额：100 万元")
    check(
        "带图时按【图片内容】/【用户提问】拼接",
        wrapped.startswith("【图片内容】")
        and "【用户提问】" in wrapped
        and wrapped.endswith(q),
    )
    check("带图时消息里确实带上了图片内容", "预算金额：100 万元" in build_messages(wrapped, src, [])[-1]["content"])
    check("图片规则拼进了 Agent 系统提示词", _AGENT_IMAGE_RULES in AGENT_SYSTEM_PROMPT)
    check(
        "不传图时 system 提示词也没变（规则是无条件拼的常量）",
        build_agent_messages(q, src, [])[0]["content"] == AGENT_SYSTEM_PROMPT,
    )

    # ---- 请求模型 ----
    check("ChatRequest 有 image_context 且默认为空串", ChatRequest(question="x").image_context == "")
    check(
        "ChatRequest 不传 image_context 时与显式传空串等价",
        ChatRequest(question="x").model_dump() == ChatRequest(question="x", image_context="").model_dump(),
    )

    # ---- 结果模型：必须能过 api.py 的 json.dumps ----
    result = VisionResult(
        ok=True,
        doc_type="招标公告",
        fields={"预算金额": "100 万元"},
        summary="某项目的招标公告",
        full_text="……",
    )
    try:
        json.dumps(result.model_dump(), ensure_ascii=False)
        serializable = True
    except TypeError as exc:
        serializable = False
        print(f"         {exc}")
    check("VisionResult.model_dump() 可 JSON 序列化", serializable)

    # ---- 宽容解析：提示词里写了「不要 markdown」，实测模型照样包围栏 ----
    check("_extract_json 剥得掉 ```json 围栏", _extract_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("_extract_json 抠得动前后有废话的", _extract_json('好的：\n{"a": 1}\n以上。') == {"a": 1})
    check("_extract_json 对非 JSON 返回 None（调用方据此降级）", _extract_json("我识别不出来") is None)

    # ---- 值必须是 str：模型会把金额返回成数字、把关键条款返回成数组 ----
    fields = _to_fields(
        {"预算金额": 123456, "关键条款": ["保证金不超过 10%", "工期 90 天"], "全文": "x", "文档类型": "招标公告"}
    )
    check(
        "_to_fields 的值全是 str（模型返回数字/数组也兜得住）",
        all(isinstance(v, str) for v in fields.values()),
    )
    check("_to_fields 把提升成一级字段的三个排掉了", "全文" not in fields and "文档类型" not in fields)
    check("_to_fields 顺序按 VISION_FIELDS 而不是模型的键序", list(fields)[:2] == ["预算金额", "关键条款"])

    # ---- 未配 Key 的处置：抛异常（→ 接口层 503），而不是返回 ok=False ----
    # 这两种失败要分开：**能力没配**是 503（用户该去改 .env），**这次解析失败**是 200 + ok=False
    # （用户该重试或换张图）。混成一种，前端就没法给出对得上的提示。
    # ⚠ **必须拿真模块，不能走 `bidding_qa.vision` 那个转发模块**：`_client` 是**模块全局**，
    #   而转发模块与真模块是两个不同的模块对象——往转发模块上赋值只会写进它自己的命名空间，
    #   `_get_client()` 读的仍是真模块的全局，那两行赋值会**静默变成空操作**（单例照旧复用）。
    from bidding_qa.clients import vision_client as vision
    from bidding_qa.vision import VisionNotConfiguredError

    saved_key = settings.zhipu_api_key
    settings.zhipu_api_key = ""
    vision._client = None  # 清掉已建好的单例，否则它会直接复用、根本走不到检查
    try:
        vision.analyze("AAAA")
        raised: Exception | None = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        settings.zhipu_api_key = saved_key
        vision._client = None
    check("未配 Key 时 analyze() 抛 VisionNotConfiguredError（而非返回 ok=False）",
          isinstance(raised, VisionNotConfiguredError), repr(raised))

    # ---- agent.py 的形参 ----
    import inspect

    from bidding_qa.agent import run_agent_stream

    check(
        "run_agent_stream 收 image_context 关键字参数",
        "image_context" in inspect.signature(run_agent_stream).parameters,
    )

    # ---- api.py 挂上了 /api/vision ----
    # 这个 import 会拉起 retriever → sentence_transformers，比较慢，放在最后
    from bidding_qa.api import app

    paths = {getattr(r, "path", "") for r in app.routes}
    check("api.py 注册了 POST /api/vision", "/api/vision" in paths)


def main() -> int:
    banner("① 离线接线检查（不需要 API Key，也不需要任何服务）")
    _offline()
    if _fail:
        print("\n离线断言就挂了——真机那部分依赖它们，先修这里再往下走。")
        return 1

    banner("② 凭据与 SDK")

    # 顺带抓 config.py 的 extra="ignore" 那个坑：字段没声明的话，.env 里的 Key 会被静默丢弃
    check("settings 里有 zhipu_api_key 字段", hasattr(settings, "zhipu_api_key"))
    key = getattr(settings, "zhipu_api_key", "").strip()
    if not key:
        print("\n离线部分已全部通过；未配置 ZHIPU_API_KEY，真机部分跳过。")
        return 1
    check("ZHIPU_API_KEY 非空（值不打印）", True)

    try:
        from zai import ZhipuAiClient  # noqa: F401
    except ImportError as exc:
        print(f"\n[错误] 未安装 zai-sdk：{exc}\n        请先 uv add zai-sdk")
        return 1
    check("from zai import ZhipuAiClient 可用", True)
    print(f"         模型 = {settings.zhipu_vision_model}")

    png = make_png("2024")
    b64 = base64.b64encode(png).decode("ascii")
    print(f"         造图 = {len(png)} 字节 PNG，base64 后 {len(b64)} 字符")

    from zai import ZhipuAiClient

    # 显式传参：SDK 自己认的环境变量是 ZAI_API_KEY，与 .env 里的 ZHIPU_API_KEY 不是一个名字
    client = ZhipuAiClient(api_key=key, base_url=settings.zhipu_base_url)

    banner("③ 裸 base64（不带 data: 前缀）")
    try:
        text_raw, secs_raw = _call(client, b64, timeout=settings.zhipu_vision_timeout)
    except Exception as exc:  # noqa: BLE001
        check("裸 base64 调用成功", False, f"{type(exc).__name__}: {exc}")
        text_raw, secs_raw = "", 0.0
    else:
        check("裸 base64 调用成功", True)
        print(f"         耗时 {secs_raw:.2f}s")
        print(f"         正文 {text_raw[:200]!r}")

    banner("④ data URI（带 data:image/png;base64, 前缀）")
    try:
        text_uri, secs_uri = _call(
            client, f"data:image/png;base64,{b64}", timeout=settings.zhipu_vision_timeout
        )
    except Exception as exc:  # noqa: BLE001
        check("data URI 调用成功", False, f"{type(exc).__name__}: {exc}")
        text_uri, secs_uri = "", 0.0
    else:
        check("data URI 调用成功", True)
        print(f"         耗时 {secs_uri:.2f}s")
        print(f"         正文 {text_uri[:200]!r}")

    banner("⑤ 结论断言")

    # 最关键的一条：正文非空。推理型模型的思维链吃光 max_tokens 时，这里会是空串。
    check(
        "两种形态都返回了非空正文（思维链没吃光 max_tokens）",
        bool(text_raw.strip()) and bool(text_uri.strip()),
        f"裸={len(text_raw)} 字符，data URI={len(text_uri)} 字符；"
        f"若都是 0，把 ZHIPU_VISION_MAX_TOKENS 调大",
    )

    # OCR 真的读到了 "2024"，而不是瞎编一句「一张图片」
    check(
        "OCR 读出了图里的数字 2024",
        "2024" in text_raw,
        f"实际返回：{text_raw[:200]!r}",
    )

    # 两种形态应当等价（SDK 内部都会剥前缀）
    check(
        "两种形态结论一致（SDK 会剥掉前缀，故等价）",
        text_raw.strip() == text_uri.strip() or ("2024" in text_raw and "2024" in text_uri),
    )

    # 结构化 JSON 能不能解析出来（vision.py 依赖这一点，解析失败要降级而不是报错）
    parsed = None
    raw = text_raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    try:
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        pass
    check("返回的是可解析的 JSON", isinstance(parsed, dict), f"实际：{raw[:200]!r}")

    banner("⑥ 汇总")
    print(f"  通过 {_pass} / 失败 {_fail}")
    if _fail:
        print("\n有断言未通过——先别接着写 vision.py，把上面的实际返回看清楚。")
    else:
        print("\n视觉链路可用。SDK 侧结论：裸 base64 与 data URI 均可，SDK 统一成裸 base64 发出。")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
