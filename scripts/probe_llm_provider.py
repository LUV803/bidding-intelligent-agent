"""验证文本大模型供应商的**按请求选择**：工厂、缓存、可用列表、接线。

**全离线**：不需要任何 API Key / Qdrant / Neo4j / PostgreSQL / 网络。
供应商的 `model` 与 `ready` 都是**纯读配置**的 property，构造实例也只是给 `._client`
赋个 None —— 整份脚本一条网络都不发。

⑤ 那节会**导入 `api`** 并临时替换它的三个名字（用完在 `finally` 里恢复）。
`api` 的导入期不建任何连接（`_bootstrap` 是在 lifespan 里跑的），所以这一步仍然离线。

改 `clients/llm_factory.py` / `schemas.py` 的 `ChatRequest`·`HealthResponse` /
`api.py` 里那两处 `get_llm(...)` 之后都该跑一次。

⚠ 有一类断言**故意按 `type(llm).__name__` 判、不按 `settings.llm_provider` 判**
（照 `probe_tools.py` ⑤/⑤b 的成例）：工厂在名字认不出来时会**静默回退**，
那时 `settings` 与实际用的那家会不一致，以 `settings` 为准会看到假绿。

跑法：

    uv run python scripts/probe_llm_provider.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa.clients import available_providers, get_llm, resolve_provider  # noqa: E402
from bidding_qa.clients.llm_factory import (  # noqa: E402
    _DEFAULT_FALLBACK,
    _LABELS,
    _PROVIDERS,
)
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.schemas import ChatRequest, HealthResponse  # noqa: E402

failures: list[str] = []
count = 0


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def check(label: str, got: object, want: object, detail: str = "") -> None:
    global count
    count += 1
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok or detail:
        extra = f"  [{detail}]" if detail else ""
        print(f"      got={got!r} want={want!r}{extra}")
    if not ok:
        failures.append(label)


# ================================================================ ① 归一化与默认
def probe_resolve() -> None:
    banner("① 名字归一化：空 ⇒ 默认，大小写/空格无关，认不出来就降级")

    check("`_DEFAULT_FALLBACK` 本身是个已知供应商", _DEFAULT_FALLBACK in _PROVIDERS, True)
    check("至少两家可选（否则前端那个胶囊不该渲染）", len(_PROVIDERS) >= 2, True)

    # 「不传就是默认」——这三者必须**完全等价**，任何一处不等价都会让
    # 「不加 llm_provider 字段 = 改动前行为」这条不变量失效。
    d = resolve_provider()
    check("无参 == None", resolve_provider(None), d)
    check("无参 == 空串", resolve_provider(""), d)
    check("无参 == 全空白", resolve_provider("   "), d)
    check("默认那家确实是 settings.llm_provider（小写后）",
          d, settings.llm_provider.strip().lower() if settings.llm_provider.strip().lower() in _PROVIDERS
          else _DEFAULT_FALLBACK)

    for name in _PROVIDERS:
        check(f"{name}: 原样", resolve_provider(name), name)
        check(f"{name}: 大写", resolve_provider(name.upper()), name)
        check(f"{name}: 两侧空格", resolve_provider(f"  {name}  "), name)

    # 降级而非报错 —— 与 graph_store / pg_store / QDRANT_MODE=auto 同一条惯例。
    # 拼错一个环境变量不该让整个服务起不来。
    check("未知值 → 降级到默认（不抛）", resolve_provider("zaip"), _DEFAULT_FALLBACK)
    check("未知值 → 空串也不炸", resolve_provider(""), d)


# ================================================================ ② 缓存
def probe_cache() -> None:
    banner("② 缓存：每家一个实例，且**保住 `_client`**（连接池不丢）")

    # get_llm() 无参的行为必须与改动前逐字相同（全仓 16 处调用点都这么用）
    check("get_llm() == get_llm(None)", get_llm() is get_llm(None), True)
    check("get_llm() == get_llm('')", get_llm() is get_llm(""), True)

    base = get_llm()
    check("无参拿到的就是默认那家（按实际类名判，不看 settings）",
          type(base).__name__, _PROVIDERS[resolve_provider()].__name__)

    for name in _PROVIDERS:
        a, b = get_llm(name), get_llm(name)
        check(f"{name}: 两次调用是同一个对象", a is b, True)
        check(f"{name}: 类型正确", type(a).__name__, _PROVIDERS[name].__name__)
        check(f"{name}: model 非空", bool(a.model.strip()), True)

    names = sorted(_PROVIDERS)
    check("不同供应商是不同对象",
          get_llm(names[0]) is not get_llm(names[1]), True)
    check("不同供应商 model 不同",
          get_llm(names[0]).model != get_llm(names[1]).model, True)

    # ⚠ 这一条是**连接池**那件事的唯一凭证：`_client` 是懒建在**实例**上的
    #   （base_client 的 `client` property）。若哪天改成每次 `cls()` 新建，
    #   缓存这条断言不会红，但这里会 —— 每次提问都要重付一次 AsyncOpenAI 构造。
    victim = get_llm(names[0])
    sentinel = object()
    saved = victim._client
    try:
        victim._client = sentinel  # type: ignore[assignment]
        check("缓存住实例 ⇒ 也就缓存住了懒建的 _client",
              get_llm(names[0])._client is sentinel, True)
    finally:
        victim._client = saved


# ================================================================ ③ 可用列表
def probe_available() -> None:
    banner("③ 可用列表：**只列真配了 Key 的**（没配的点了必然失败，摆出来是骗人）")

    lst = available_providers()
    check("返回 list", isinstance(lst, list), True)
    check("只含 ready 的", all(get_llm(x["name"]).ready for x in lst), True)
    check("每项三个键", all(set(x) == {"name", "label", "model"} for x in lst), True)
    check("值全是 str", all(isinstance(v, str) for x in lst for v in x.values()), True)
    check("name 都在注册表里", all(x["name"] in _PROVIDERS for x in lst), True)
    check("顺序稳定、= _PROVIDERS 声明顺序",
          [x["name"] for x in lst], [n for n in _PROVIDERS if get_llm(n).ready])

    # 本机配了几家是**环境**不是代码，不该硬断言（换台机器只有一把 Key 就会假红）。
    # 打出来供人判断：少于 2 家时前端那个胶囊不会渲染，这是**正常**的。
    print(f"      本机可用：{len(lst)}/{len(_PROVIDERS)} "
          f"（{', '.join(x['label'] for x in lst) or '无'}）"
          f"{'  ⚠ <2，前端不渲染胶囊，属正常' if len(lst) < 2 else ''}")

    # ⚠ 下面这条是「只列 ready 的」**唯一**能真正证明的地方：上面几句里，一个
    #   「无脑列全部、从不看 Key」的实现也会全绿。抹掉一家的 Key，它必须当场消失。
    # ⚠ 键名映射写死在这里：加第三家供应商要顺手加一行（否则本节静默跳过）。
    _KEY_FIELD = {"deepseek": "deepseek_api_key", "zai": "zhipu_api_key"}
    victim = next((n for n in _PROVIDERS if n in _KEY_FIELD), None)
    if victim is None:
        print("      [SKIP] 没有可抹 Key 的供应商，跳过消失测试")
    else:
        key_field = _KEY_FIELD[victim]
        saved = getattr(settings, key_field)
        before = {x["name"] for x in lst}
        setattr(settings, key_field, "")
        try:
            after = {x["name"] for x in available_providers()}
            check(f"抹掉 {victim} 的 Key 后它从列表消失", victim in after, False)
            check("其余各家不受影响", after, before - {victim})
        finally:
            setattr(settings, key_field, saved)
        check("恢复 Key 后它回来了", victim in {x["name"] for x in available_providers()}, True)

    # ⚠⚠ 本地供应商（vLLM / Ollama）的 ready 判据**不是 Key 而是 base_url** ——
    #   与 `NEO4J_URI` / `POSTGRES_HOST` / `TAVILY_API_KEY` 的「字段非空即为开」同一条惯例。
    #   所以它们**不能**进上面那张 `_KEY_FIELD`：那张表的语义是「抹掉 Key 它就消失」，
    #   而这两个的开关是地址。硬塞进去会让 `victim` 从 deepseek 飘到别家，
    #   那条「抹 Key 必消失」的断言就变成在测另一件事了。
    # ⚠ 这一段在**任何**机器上都成立（不读本机配没配）：它自己造地址、自己抹掉。
    #   最值钱的是第一句 —— 它就是「本机开发环境零影响」那条交付要求的凭证。
    for name, field in (("vllm", "vllm_base_url"), ("ollama", "ollama_base_url")):
        saved = getattr(settings, field, "")
        was_in = name in {x["name"] for x in available_providers()}
        setattr(settings, field, "")
        try:
            check(f"{name}：base_url 为空时**不在**列表里（没跑本地服务的机器上等于不存在）",
                  name in {x["name"] for x in available_providers()}, False)
            setattr(settings, field, "http://127.0.0.1:9/v1")
            lst2 = available_providers()
            check(f"{name}：填上 base_url 后它出现（判据是地址，不是 Key）",
                  name in {x["name"] for x in lst2}, True)
            check(f"{name}：出现时字段齐全、model 非空",
                  all(isinstance(v, str) and v.strip()
                      for k, v in next(x for x in lst2 if x["name"] == name).items()), True)
        finally:
            setattr(settings, field, saved)
        check(f"{name}：恢复原值后回到原状态", name in {x["name"] for x in available_providers()}, was_in)

    # ⚠ SDK 拒绝空 api_key（`base_client.py` 建连处会抛），但本地服务根本不校验它。
    #   所以本地客户端返回一个占位串 —— 漏了它的表现是「地址配好了却连不上」，
    #   报错文本还是 SDK 的，完全指不到真因。
    check("本地两家给 SDK 一个非空占位 Key（空串会被 SDK 拒掉）",
          [get_llm(n).api_key for n in ("vllm", "ollama")], ["EMPTY", "EMPTY"])

    # label 是前端胶囊上显示的字，漏配会显示成键名（"zai" 而不是 "GLM"）——难看但不报错，
    # 所以必须有断言挡。这是本文件里唯一检查 `_LABELS` 完整性的地方。
    missing = set(_PROVIDERS) - set(_LABELS)
    check("_LABELS 覆盖 _PROVIDERS 全部键", not missing, True, f"缺：{missing}" if missing else "")
    check("label 都非空", all(v.strip() for v in _LABELS.values()), True)


# ================================================================ ④ 接线
def probe_wiring() -> None:
    banner("④ 接线：请求体与健康检查的字段")

    # ⚠ 默认值这条钉着 probe_web_search.py：那里直接构造 `ChatRequest(...)` 而**不传**
    #   llm_provider，没默认值那条探针会直接红。
    req = ChatRequest(question="x")
    check("ChatRequest.llm_provider 默认为空串", req.llm_provider, "")
    check("空串 ⇒ get_llm 走默认（与不加字段等价）",
          get_llm(req.llm_provider or None) is get_llm(), True)
    check("显式传值照样认", ChatRequest(question="x", llm_provider="zai").llm_provider, "zai")

    fields = HealthResponse.model_fields
    for f in ("llm_provider", "llm_providers"):
        check(f"HealthResponse 有 {f}", f in fields, True)

    # `llm_provider(s)` 报的是**默认**那家，不是「这次用的」—— 这条是那条限制的凭证
    check("health 里 llm_provider 是归一化后的默认名",
          resolve_provider(), settings.llm_provider.strip().lower()
          if settings.llm_provider.strip().lower() in _PROVIDERS else _DEFAULT_FALLBACK)

    # api.py 那两处 live 调用点必须真的把字段传下去。光看源码文本不行（注释里也有这词），
    # 所以查的是**函数签名**——`get_llm` 必须还能接受一个位置参数。
    import inspect

    sig = inspect.signature(get_llm)
    check("get_llm 能收一个位置参数", len(sig.parameters) >= 1, True)
    p = next(iter(sig.parameters.values()))
    check("该参数有默认值（16 处无参调用靠它）", p.default is not inspect.Parameter.empty, True)
    check("默认值是 None", p.default, None)


# ================================================================ ⑤ 请求级路由
def probe_request_routing() -> None:
    banner("⑤ 两条链路都**按请求**把供应商名交给 get_llm（不是只把字段收进 schema）")

    import asyncio

    from bidding_qa import api

    seen: list[object] = []
    #: 「`get_llm` 一次都没被调到」的哨兵 —— 与「调到了、传的是 None」必须能分开
    _MISSING = object()

    class _Halt(Exception):
        """哨兵：`get_llm` 一被调用就抛，用来在**整条管线跑起来之前**截住这次请求。

        只用两个替身（`_require_ready` / `get_retriever`）就够了 —— 两个 handler 里
        `get_llm` 都排在它们后面、排在一切真正干活的东西前面（`api.py:433` / `:603`）。
        """

    def recorder(*args, **kwargs):
        seen.append(args[0] if args else kwargs.get("provider"))
        raise _Halt

    saved = {
        "require_ready": api._require_ready,
        "get_retriever": api.get_retriever,
        "get_llm": api.get_llm,
    }

    def hit(handler, provider: str):
        seen.clear()
        payload = api.ChatRequest(question="x", llm_provider=provider)

        async def _go():
            try:
                result = await handler(payload)
                # 兜底：哪天 `get_llm` 被挪进 `event_stream()` 生成器体内，上面那句就**不会**
                # 执行到它 —— 那时不把生成器推进去，这节会静默变成空转。
                if hasattr(result, "__anext__"):
                    await anext(result)
            except _Halt:
                pass

        asyncio.run(_go())
        # ⚠⚠ 绝不能写 `seen[0] if seen else None` —— 「一次都没调到」与「调到了但传的是 None」
        #   就分不开了，而**空串那一档期望值正好就是 None**：两处 handler 的调用点若被整块
        #   删掉，那 2 条会继续 PASS（本节第一版就是这么写错的，实测 6 条全红才看出来）。
        return seen[0] if seen else _MISSING

    try:
        api._require_ready = lambda: None  # type: ignore[assignment]
        api.get_retriever = lambda: object()  # type: ignore[assignment]
        api.get_llm = recorder  # type: ignore[assignment]

        for label, handler in (("/api/chat", api.chat), ("/api/agent", api.agent)):
            # 空串 ⇒ None ⇒ 服务端默认。**这条是「不加字段 = 改动前行为」的凭证。**
            check(f"{label}：空串 → 传 None（走服务端默认）", hit(handler, ""), None)
            check(f"{label}：'zai' 原样传下去", hit(handler, "zai"), "zai")
            # 归一化**只在 get_llm 里做一处**：handler 把原样的串递下去，
            # 所以大小写/空格在这里不处理（处理两遍就会有两套口径）。
            check(f"{label}：大小写与空格原样传（归一化只在 get_llm）",
                  hit(handler, "  ZAI  "), "  ZAI  ")
            # 认不出来的名字也**照传**：降级发生在 resolve_provider 里，
            # handler 不预先过滤 —— 否则「未知值降级」那条就没有真机路径了。
            check(f"{label}：未知名字照传（降级在 get_llm 里做）", hit(handler, "zaip"), "zaip")
    finally:
        for name, value in saved.items():
            setattr(api, name, value)


# ================================================================ ⑥ 降级提示
def probe_fallback_hint() -> None:
    banner("⑥ 降级提示点名的是**对的那个键**（真机撞到的 bug 的回归）")

    from bidding_qa import rag
    from bidding_qa.schemas import Source

    kb = [
        Source(
            index=1,
            doc_id="kb1",
            question="知识库问题1",
            answer="知识库答案1",
            snippet="片段1",
            score=0.9,
        )
    ]
    tail = "以下为知识库中匹配度最高的原始答复。\n\n知识库答案1 [1]"

    # ⭐ 这条钉的是改动前的**逐字节原文**：DeepSeek 那一路一个字符都不许变。
    #   （`probe_web_search.py` ⑥ 的对应那条两参调用已改成中性话，这里补上真客户端那一份。）
    check(
        "⭐ DeepSeek：提示与改动前逐字节相同",
        rag.fallback_answer(kb, False, get_llm("deepseek")),
        "> 提示：未配置 DEEPSEEK_API_KEY，" + tail,
    )
    # ⭐⭐ 这条就是真机撞到的那个 bug：选了 GLM 却被叫去查 DEEPSEEK_API_KEY。
    zai_text = rag.fallback_answer(kb, False, get_llm("zai"))
    check("⭐⭐ zai（GLM）：点名的是 ZHIPU_API_KEY", "未配置 ZHIPU_API_KEY，" in zai_text, True)
    check("⭐⭐ zai（GLM）：**不许**提 DEEPSEEK", "DEEPSEEK" in zai_text, False)

    # 两参调用（老签名）退成中性话 —— 具体但不指错的取舍见 `rag._no_key_hint` 的注释。
    neutral = rag.fallback_answer(kb, False)
    check("不传客户端：中性措辞", "未配置 API Key，" in neutral, True)
    check("不传客户端：不指任何一家", "DEEPSEEK" in neutral or "ZHIPU" in neutral, False)

    # ⚠ 冻结路径：`llm_ready=True` 时前缀整个为空 —— 传哪家、传不传都不影响。
    #   这是「本次改动没碰正常路径」的凭证（`probe_web_search.py` ⑥ 也钉着）。
    ready_text = rag.fallback_answer(kb, True, get_llm("zai"))
    check("llm_ready=True：前缀为空（与客户端无关）", ready_text, "知识库答案1 [1]")

    # 泛化到注册表里**每一家**：新加供应商只要写了自己的 `_NOT_CONFIGURED` 就自动对，
    # 漏写则退成基类那句中性话（不是别人的键名）—— 这是这条能泛化的前提。
    for name in _PROVIDERS:
        text = rag.fallback_answer(kb, False, get_llm(name))
        head = text.split("，", 1)[0]
        check(f"{name}：提示来自它自己的 _NOT_CONFIGURED", head, f"> 提示：未配置{_key_of(name)}")
        others = [n for n in _PROVIDERS if n != name]
        check(f"{name}：不蹭别家的键名", any(h in head for h in (_key_of(o) for o in others)), False)


def _key_of(name: str) -> str:
    """该供应商 `_NOT_CONFIGURED` 里那个键名（`_NOT_CONFIGURED` 是「未配置 X，请…」的形状）。"""
    return str(getattr(get_llm(name), "_NOT_CONFIGURED", "")).split("，", 1)[0].removeprefix("未配置")


# ================================================================ main
def main() -> int:
    print(f"当前 settings.llm_provider = {settings.llm_provider!r} → 实际 {resolve_provider()!r}")
    probe_resolve()
    probe_cache()
    probe_available()
    probe_wiring()
    probe_request_routing()
    probe_fallback_hint()

    print("\n" + "=" * 68)
    if failures:
        print(f"结果：{count - len(failures)} 通过 / {len(failures)} 失败")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print(f"结果：{count} 通过 / 0 失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
