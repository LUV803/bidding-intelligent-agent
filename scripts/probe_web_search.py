"""独立验证联网搜索层：接线 / 降级 / 编号 / 提示词矩阵 / 泄漏归属 / 冻结路径。

这一层不依赖大模型、不依赖 Qdrant/BM25，**离线段不需要 TAVILY_API_KEY**，
配了 Key 才会多跑一段真机（⑬）。改 `web_search.py` / `rag.py` / `agent.py` / `api.py`
的联网相关代码之后都该跑一次。

分节编号（**照代码里的 banner 为准**，这份清单原先与它们对不上，2026-09-20 顺带修了）：

    ① 接线  ② 配置解析回归  ③ 降级  ④ to_sources（含 ④b 空结果重试）
    ⑤ JSON 序列化  ⑥ ⭐ 冻结路径  ⑦ ⭐ 提示词 ⟺ 工具表  ⑧ 泄漏归属
    ⑨ 渲染文案  ⑩ 端到端（假 search）  ⑪ 真机  ⑫ /api/chat 必定联网
    ⑬ Agent 预联网的种键条件

几条最要紧的断言，都是「静默出错」那一类（不报错、不异常，只是行为悄悄不对）：

1. **冻结路径**（⑥）：`web=False` 时 `build_context` / `build_messages` /
   `build_agent_messages` / `fallback_answer` / `_render_result` 必须与改动前**逐字节相同**。
   这是整个功能的护栏 —— 开关关着、或服务端没配 Key 时，用户体验不能有任何变化。
2. **提示词 ⟺ 工具表**（⑦）：`("search_web" in 提示词) == (工具表里有 search_web)`，
   四种组合都要成立。写反了就是 CLAUDE.md 点名的自相矛盾：提示词让模型用一个没注册的工具，
   或者工具注册了模型却不知道（后者表现为「联网功能像不存在」）。
   ⚠ 联网是**第一个「每请求可变」**的可选能力，图谱/采购库那套「提示词常量 == ENABLED」
   的断言形状在这里**不适用**，必须按 `tools_for()` 逐请求算。
3. **泄漏归属**（⑧）：模型把工具调用漏进正文时，`web_query` 形态必须恢复成 `search_web`。
   ⚠ 这一条**只在传了本轮工具表时才成立** —— `_match_schema` 默认只看得见进程级那批
   schema，看不见按请求挂的联网工具，表现正是那条线上 bug 的形状：
   「模型说了要联网，结果什么都没查」。
4. **网页不冒充知识库**（⑨）：`_render_result(kind="web")` 的空结果文案、以及
   `fallback_answer` 对网页条目的措辞里**都不能出现「知识库中……」** ——
   一次联网失败被描述成知识库结论，用户会以为法规库查过了。
5. **开关打开 ⇒ 必定联网**（⑫）：本地**答得出**也照样搜，且第二次 `sources` 必须排在
   第一个 `delta` **之前**，`answer_reset` 在 `/api/chat` 上**不再出现**。
   ⚠ 2026-09-20 之前这里是「兜底」语义（三种触发才搜），本节的负例正是那套旧语义的回归点。

跑法：

    uv run python scripts/probe_web_search.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import agent, graph_store, pg_store, rag, web_search  # noqa: E402
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.mcp import web_search_exa  # noqa: E402
from bidding_qa.schemas import Source, WebResult, WebRow  # noqa: E402

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
        if detail:
            print(f"         {detail}")


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


# ---------- 测试替身 ----------
def kb_source(index: int, **kw) -> Source:
    base = dict(
        index=index,
        doc_id=f"kb{index}",
        question=f"知识库问题{index}",
        answer=f"知识库答案{index}",
        snippet=f"片段{index}",
        score=0.9,
    )
    base.update(kw)
    return Source(**base)


def web_source(index: int, **kw) -> Source:
    base = dict(
        index=index,
        doc_id=f"web:https://ah.gov.cn/x{index}",
        question=f"网页标题{index}",
        answer=f"网页正文{index}",
        snippet=f"网页正文{index}",
        score=0.87,
        url=f"https://ah.gov.cn/x{index}",
        origin="web",
    )
    base.update(kw)
    return Source(**base)


def fake_result(rows: list[WebRow], *, ok: bool = True, note: str | None = None) -> WebResult:
    return WebResult(ok=ok, query="测试查询", rows=rows, total=len(rows), elapsed_ms=12.3, note=note)


def fake_search_factory(payload: WebResult):
    """替换 web_search.search。记录调用次数，供去重断言用。"""
    calls: list[tuple[str, object]] = []

    async def _fake(query: str, *, max_results=None):
        calls.append((query, max_results))
        return payload

    return _fake, calls


# ---------- ① 接线 ----------
def probe_wiring() -> None:
    banner("① 接线 / 开关一致性")

    # ⚠ 必须放最前面：Settings 是 extra="ignore"，.env 里写了 TAVILY_* 而 config.py 没声明
    #   字段的话会被**静默丢弃**（Neo4j / Postgres / Zhipu 各踩过一次，这是第四次），
    #   症状是「明明配了却一点动静都没有」。
    for field in (
        "tavily_api_key",
        "tavily_base_url",
        "tavily_search_depth",
        "tavily_max_results",
        "tavily_timeout",
        "web_search_min_score",
        "web_search_domains",
        "web_content_max_chars",
    ):
        check(f"config.py 声明了 {field}", hasattr(settings, field))

    check(
        "web_search_domains 是 str 而非 list（见 config.py 那段注释）",
        isinstance(settings.web_search_domains, str),
    )
    check(
        "web_search.ENABLED == bool(tavily_api_key.strip())",
        web_search.ENABLED == bool(settings.tavily_api_key.strip()),
        detail=f"ENABLED={web_search.ENABLED}",
    )
    for name in ("ENABLED", "is_configured", "search", "to_sources", "web_status"):
        check(f"模块导出 {name}", hasattr(web_search, name))

    # ⚠ **刻意没有 warmup**：联网没有「连接」可建，预热只是多一次网络往返 + 一笔失败日志。
    #   这条断言是防止下一个人「照 graph_store 补全它」。
    check("web_search 刻意没有 warmup()（联网无连接可建）", not hasattr(web_search, "warmup"))


def probe_settings_regression() -> None:
    """`WEB_SEARCH_DOMAINS=gov.cn,ccgp.gov.cn` 必须能解析。

    ⚠ 这是 P1-10 的回归：字段一旦写成 `list[str]`，pydantic-settings 会把它当 JSON 解析，
    在这句抛 SettingsError。而 `settings = get_settings()` 是 config.py 的**模块级**执行
    —— `_bootstrap()` 之前就崩，整个服务 503，症状还长得像「配置没生效」。
    """
    banner("② 配置解析回归（逗号分隔的域名白名单）")

    from bidding_qa.config import Settings

    tmp = Path(tempfile.mkdtemp()) / "probe.env"
    tmp.write_text(
        "WEB_SEARCH_DOMAINS=gov.cn, ccgp.gov.cn\nWEB_SEARCH_MIN_SCORE=0\n",
        encoding="utf-8",
    )
    try:
        s = Settings(_env_file=tmp)
    except Exception as exc:  # noqa: BLE001
        check("逗号分隔的 WEB_SEARCH_DOMAINS 不抛异常", False, detail=f"{type(exc).__name__}: {exc}")
        return

    check("逗号分隔的 WEB_SEARCH_DOMAINS 不抛异常", True)
    check(
        "web_search_domain_list 正确切分并去掉空白",
        s.web_search_domain_list == ["gov.cn", "ccgp.gov.cn"],
        detail=f"{s.web_search_domain_list}",
    )
    check("留空时为 []（≠ ['']，否则会被当成「一个域名都不许」）", Settings(
        _env_file=None, web_search_domains=""
    ).web_search_domain_list == [])


# ---------- ③ 降级 ----------
def probe_degradation() -> None:
    banner("③ 降级：未配 Key 时的行为")

    if web_search.ENABLED:
        print("  （已配置 TAVILY_API_KEY，跳过「未配置」分支）")
    else:
        import asyncio

        result = asyncio.run(web_search.search("任意查询"))
        check("未配 Key 时 search() 返回 ok=False 而不是抛异常", result.ok is False)
        check("note 里说清了原因", "TAVILY_API_KEY" in (result.note or ""), detail=f"{result.note!r}")
        check("空 query 被挡住（不静默返回全量结果）", asyncio.run(web_search.search("  ")).ok is False)

    check("is_configured() 不发起网络调用（纯读配置）", web_search.is_configured() == web_search.ENABLED)

    status = web_search.web_status()
    for key in ("configured", "available", "detail"):
        check(f"web_status() 含 {key}", key in status)
    check(
        "未配置时 configured=False 且 detail 点明缺什么",
        web_search.ENABLED or (status["configured"] is False and "TAVILY_API_KEY" in status["detail"]),
    )


# ---------- ④ to_sources ----------
def probe_to_sources() -> None:
    banner("④ to_sources：编号 / 归一化 / 截断 / 必填字段")

    rows = [
        WebRow(title="标题A", url="https://ah.gov.cn/a", content="正文A", score=0.9),
        WebRow(title="", url="https://ah.gov.cn/b", content="   ", score=0.8),  # 空内容 → 丢
        WebRow(title="标题C", url="https://ah.gov.cn/c", content="正文C", score=0.7),
    ]
    srcs = web_search.to_sources(fake_result(rows))
    check("空 content 的行被丢掉", len(srcs) == 2, detail=f"{[s.question for s in srcs]}")
    check("编号连续（1..N，闸门过滤之后才分配）", [s.index for s in srcs] == [1, 2])

    s0 = srcs[0]
    check("doc_id 带 web: 前缀（避免与知识库 doc_id 撞车）", s0.doc_id.startswith("web:"))
    check("origin == 'web'", s0.origin == "web")
    check("url 已填", s0.url == "https://ah.gov.cn/a")
    check("question 存的是标题、answer/snippet 存的是正文",
          s0.question == "标题A" and s0.answer == "正文A" and s0.snippet == "正文A")
    for field in ("index", "doc_id", "question", "answer", "snippet", "score"):
        check(f"Source 必填字段 {field} 非空", getattr(s0, field) not in (None, ""))

    # ⚠ URL 归一化只用于 doc_id：同一页面以两种 URL 返回时必须是同一个 doc_id，
    #   否则 SourceRegistry 按 doc_id 去重会失效 → 同一内容占两个 [n]、面板出现两条。
    # ⚠ score 在这里是**陪衬**：WebRow.score 默认 0.0，而 min_score>0 时会被闸门筛空
    #   → 下面三行全取 [0] 会 IndexError。这几条验的是归一化，分数给足就行。
    a = web_search.to_sources(fake_result([WebRow(title="t", url="https://AH.gov.cn/x/?utm_source=tavily#frag", content="c", score=0.9)]))
    b = web_search.to_sources(fake_result([WebRow(title="t", url="https://ah.gov.cn/x/", content="c", score=0.9)]))
    c = web_search.to_sources(fake_result([WebRow(title="t", url="https://ah.gov.cn/x", content="c", score=0.9)]))
    check("带 utm/fragment/大写主机/尾斜杠的 URL 归一成同一个 doc_id",
          a[0].doc_id == b[0].doc_id == c[0].doc_id, detail=f"{a[0].doc_id}")
    check("但展示用的 url 保持原样（不被归一化改掉）", a[0].url.endswith("#frag"))

    # ⚠ 不截断是**静默故障**：这个串会经 sources 事件进前端 localStorage，超配额时
    #   useChat 的 catch 把失败吞掉，表现是「历史对话莫名其妙少了」。
    long_rows = [WebRow(title="长", url="https://x.cn/1", content="字" * 5000, score=0.9)]
    clipped = web_search.to_sources(fake_result(long_rows))[0]
    check(
        f"超长正文被截断到 web_content_max_chars={settings.web_content_max_chars}",
        len(clipped.answer) <= settings.web_content_max_chars + 1,
        detail=f"实际 {len(clipped.answer)}",
    )
    check("截断处有省略号（看得出被截过）", clipped.answer.endswith("…"))

    check("ok=False 时 to_sources 返回空列表", web_search.to_sources(fake_result(rows, ok=False)) == [])

    # 分数闸门。⚠ 这个阈值的**依据是实测的**（2026-09-20 复标的，
    #   同批数据写进了 config.py 的同名字段注释）：
    #     真答得出的查询 top 分 0.7863~0.9077  ← 不能误杀
    #     网上根本没有答案的查询 top 分 0.093~0.6594  ← 这才是要筛的
    #   0.70 落在 (0.6594, 0.7863) 这条空带里。改这个值之前先重跑 ⑪ 重新量分布。
    #
    # ⚠⚠ **判据是这一批的 top 分，不是逐行分数**（2026-09-20 改）。下面第二条断言
    #   钉的就是这次修复本身：旧实现逐行筛，会把「top 分很高、但排第 3 的那条真相关」
    #   一起砍掉——用户实测的香港那条就是这么被砍到只剩 1 条的。
    gate = settings.web_search_min_score
    if gate <= 0:
        check(
            "web_search_min_score=0 时不筛",
            len(web_search.to_sources(fake_result(rows))) == 2,
        )
    else:
        check(
            "top 分达标时，**低分的尾部结果照样保留**（逐行筛是旧的错误行为）",
            [s.score for s in web_search.to_sources(fake_result([
                WebRow(title="顶", url="https://x.cn/1", content="c", score=0.9),
                WebRow(title="腰", url="https://x.cn/2", content="c", score=gate / 2),
                WebRow(title="尾", url="https://x.cn/3", content="c", score=0.05),
            ]))] == [0.9, gate / 2, 0.05],
        )
        check(
            f"top 分低于 min_score={gate} 时**整批**丢弃（不是留下最高分那条）",
            web_search.to_sources(fake_result([
                WebRow(title="矮子里拔将军", url="https://x.cn/4", content="c", score=gate - 0.01),
                WebRow(title="更差", url="https://x.cn/5", content="c", score=0.1),
            ])) == [],
        )
        check(
            "top 分恰好等于阈值时保留（判据是严格小于，不是小于等于）",
            len(web_search.to_sources(fake_result([
                WebRow(title="临界", url="https://x.cn/6", content="c", score=gate)]))) == 1,
        )
        check(
            "空内容不参与 top 判定（拿它当 top 会把整批放行）",
            web_search.to_sources(fake_result([
                WebRow(title="空但分高", url="https://x.cn/7", content="  ", score=0.99),
                WebRow(title="有内容但分低", url="https://x.cn/8", content="c", score=gate - 0.01),
            ])) == [],
        )
        check(
            "阈值落在实测的空带 (0.6594, 0.7863) 内",
            0.6594 < gate < 0.7863,
            detail=f"min_score={gate}；掉到带外说明要么误杀真实结果、要么形同虚设",
        )


def probe_empty_retry() -> None:
    banner("④b 空结果重试：Tavily 偶发回 200 但一条结果都没有")

    import asyncio

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self._payload

    class _Client:
        """按脚本依次回响应。记下被调用了几次。"""

        def __init__(self, script: list[object]) -> None:
            self.script = list(script)
            self.calls = 0

        async def post(self, _path: str, json: Any = None) -> _Resp:
            item = self.script[min(self.calls, len(self.script) - 1)]
            self.calls += 1
            if isinstance(item, Exception):
                raise item
            return _Resp(item)  # type: ignore[arg-type]

    empty = {"results": []}
    one = {"results": [{"title": "t", "url": "https://ah.gov.cn/z", "content": "c", "score": 0.9}]}

    saved_client = web_search._get_client
    saved_failed = web_search._failed_at
    try:
        # ① 第一次空、第二次有 → 必须有结果回来。这条钉的就是那次实测的偶发空结果。
        client = _Client([empty, one])
        async def _get() -> _Client:  # noqa: E306
            return client
        web_search._get_client = _get  # type: ignore[assignment]
        result = asyncio.run(web_search.search("偶发空结果"))
        check("第一次回空时重试，拿回第二次的结果", result.ok and len(result.rows) == 1,
              detail=f"ok={result.ok} rows={len(result.rows)}")
        # ⚠ 拿到结果就 break，所以这里是 2 次（首次 + 第一次重试），不是 _EMPTY_RETRY+1
        check("拿到结果就停，不做多余的重试（2 次：首次 + 一次重试）",
              client.calls == 2, detail=f"calls={client.calls}")

        # ② 两次都空 → 如实报「没有返回任何结果」，不无限重试
        client = _Client([empty])
        async def _get2() -> _Client:  # noqa: E306
            return client
        web_search._get_client = _get2  # type: ignore[assignment]
        result = asyncio.run(web_search.search("真的没有"))
        check("每次都空 → ok=True 但 0 条（不假装搜到了东西）",
              result.ok and not result.rows and result.total == 0)
        check("note 点明「没有返回任何结果」", "没有返回任何结果" in (result.note or ""),
              detail=f"{result.note!r}")
        check(f"仍然只调用 {web_search._EMPTY_RETRY + 1} 次（不无限重试）",
              client.calls == web_search._EMPTY_RETRY + 1, detail=f"calls={client.calls}")

        # ③ 传输层失败**不重试** —— 那是 _FAIL_COOLDOWN 的职责，重试只会让黑洞主机等两倍
        import httpx

        client = _Client([httpx.ConnectError("boom")])
        async def _get3() -> _Client:  # noqa: E306
            return client
        web_search._get_client = _get3  # type: ignore[assignment]
        result = asyncio.run(web_search.search("连不上"))
        check("传输层失败 → ok=False 且只试了 1 次", result.ok is False and client.calls == 1,
              detail=f"calls={client.calls} note={result.note!r}")
        check("失败进了冷却（下次直接短路，不再等一个 timeout）", web_search._failed_at is not None)
    finally:
        web_search._get_client = saved_client  # type: ignore[assignment]
        # ⚠ 冷却状态是**模块级**的，不还原会影响后面所有真的要走网络的断言
        web_search._failed_at = saved_failed


def probe_json() -> None:
    banner("⑤ JSON 可序列化（api.py 的 json.dumps 会在这里炸）")

    rows = [WebRow(title="t", url="https://x.cn/1", content="c", score=0.5, published_date="2026-01-01")]
    payload = fake_result(rows)
    try:
        dumped = json.dumps(payload.model_dump(), ensure_ascii=False)
        check("WebResult.model_dump() 可 json.dumps", True)
        check("往返后字段不丢", json.loads(dumped)["rows"][0]["url"] == "https://x.cn/1")
    except TypeError as exc:
        check("WebResult.model_dump() 可 json.dumps", False, detail=str(exc))

    # ⚠ published_date 必须保持字符串：Tavily 回的就是字符串，当 date 解析后
    #   json.dumps 会抛 TypeError —— 与 PgResult 的 Decimal/date 那条是同一个坑
    check("published_date 是 str 而非 date", isinstance(payload.rows[0].published_date, str))
    check("score 是 float", isinstance(payload.rows[0].score, float))


# ---------- ⑥ 冻结路径 ----------
def probe_frozen_paths() -> None:
    banner("⑥ ⭐ 冻结路径：web=False 时必须与改动前逐字节相同")

    kb = [kb_source(1), kb_source(2)]

    # 改前 build_context 的原文（照抄自 git 里的旧实现）
    old_context = "\n\n".join(f"【资料{s.index}】\n问题：{s.question}\n答案：{s.answer}" for s in kb)
    check("build_context(全知识库) 逐字节相同", rag.build_context(kb) == old_context)

    old_agent_system = rag.AGENT_SYSTEM_PROMPT
    check(
        "build_agent_messages(web=False) 的 system 逐字节等于 AGENT_SYSTEM_PROMPT",
        rag.build_agent_messages("问", kb)[0]["content"] == old_agent_system,
    )
    check(
        "AGENT_SYSTEM_PROMPT 常量本身不含 search_web（联网是每请求的）",
        "search_web" not in rag.AGENT_SYSTEM_PROMPT,
    )

    # 改前 fallback_answer 的原文
    old_kb = f"知识库答案1 [1]\n\n**相关内容**\n- 知识库问题2 [2]"
    check("fallback_answer(全知识库, llm_ready=True) 逐字节相同",
          rag.fallback_answer(kb, True) == old_kb)
    # ⚠ 这一条的期望值 **2026-09-21 改过**，不是「改测试迁就代码」，是那句提示本身原来是错的：
    #   它写死 `未配置 DEEPSEEK_API_KEY`，而加了每请求供应商之后，选了 GLM 的用户会被指去
    #   查一个**本来就配好了**的键（真机实测撞到）。现在那句话按传进来的 `llm` 点名，
    #   两参调用（不传客户端）退成中性的「未配置 API Key，」。
    #   ⇒ 钉的仍然是「措辞不漂移」这件事，只是换成了现在的正确措辞；
    #     DeepSeek 那条**逐字节原文**由 `probe_llm_provider.py` ⑥ 另钉一份。
    old_nollm = "> 提示：未配置 API Key，以下为知识库中匹配度最高的原始答复。\n\n" + old_kb
    check("fallback_answer(全知识库, llm_ready=False) 措辞稳定",
          rag.fallback_answer(kb, False) == old_nollm)
    check(
        "fallback_answer([]) 文案未变",
        rag.fallback_answer([], True).startswith("根据现有知识库资料，暂未找到相关内容。"),
    )

    # _render_result 的 kb 分支
    old_render = "检索「q」新增 1 条资料（编号续用全局序号）：\n\n" + rag.build_context(kb[:1])
    check("_render_result(kind='kb') 逐字节相同",
          agent._render_result("q", kb[:1], [], 0) == old_render)
    old_empty = (
        "检索「q」没有新增资料。\n"
        "请基于已有资料作答；若仍无法回答，请直接说明知识库中没有相关依据，不要重复检索相同的词。"
    )
    check("_render_result(kind='kb', 空结果) 逐字节相同",
          agent._render_result("q", [], [], 0) == old_empty)

    # /api/chat 那条链路（build_messages）不得出现**任何**工具名
    chat_msg = rag.build_messages("问", [*kb, web_source(3)])[-1]["content"]
    tool_names = [n for n in agent.TOOL_NAMES] + [agent.TOOL_WEB]
    check(
        "build_messages 的 user 消息不含任何工具名（那条链路一个工具都没挂）",
        not any(n in chat_msg for n in tool_names),
        detail=f"命中 {[n for n in tool_names if n in chat_msg]}",
    )
    check("build_messages 的 user 消息不含【提醒】（probe_enum_route 的既有断言）",
          "【提醒】" not in chat_msg)


# ---------- ⑦ 提示词接线 / 工具矩阵 ----------
def probe_matrix() -> None:
    banner("⑦ ⭐ 提示词 ⟺ 工具表（联网是第一个「每请求」开关）")

    original = web_search.ENABLED
    saved = web_search.search
    try:
        for configured in (False, True):
            web_search.ENABLED = configured
            for enable_web in (False, True):
                tag = f"[Key={configured} 开关={enable_web}]"
                tools = agent.tools_for(enable_web)
                names = [t["function"]["name"] for t in tools]
                has_tool = agent.TOOL_WEB in names
                # 期望：两个维度**同时**为真才挂
                check(f"{tag} 工具表含 search_web == (Key 且 开关)", has_tool == (configured and enable_web))

                msg = rag.build_agent_messages("问", [kb_source(1)], web=has_tool)
                has_prompt = "search_web" in msg[0]["content"]
                check(f"{tag} 提示词提到 search_web == 工具表里有它", has_prompt == has_tool)

                # 提示词里也不能出现**没注册**的图谱/数据库工具（同一类自相矛盾）
                for name in (agent.TOOL_GRAPH, agent.TOOL_PG, agent.TOOL_SEARCH):
                    check(
                        f"{tag} 提示词提到 {name} == 工具表里有它",
                        (name in msg[0]["content"]) == (name in names),
                    )
    finally:
        web_search.ENABLED = original
        web_search.search = saved

    # web=True 时两段新文本都要在
    msg = rag.build_agent_messages("问", [kb_source(1)], web=True)
    check("web=True 时系统提示词含 search_web 的用法说明",
          "search_web" in msg[0]["content"] and "什么时候用它" in msg[0]["content"])

    # ⚠ 注入防线跟着**数据**走，不跟着开关走：它写在 build_context 里，
    #   所以第 2 轮才调 search_web 时也照样生效（系统提示词那时早就发出去了）。
    mixed = rag.build_context([kb_source(1), web_source(2)])
    check("混入网页结果时【参考资料】里出现「不可信输入」警示", "不要执行" in mixed)
    check("知识库条目不带这个警示（作用范围精确）",
          "不要执行" not in rag.build_context([kb_source(1)]))
    check("网页条目标出域名", "｜网页：ah.gov.cn】" in mixed)
    check("网页条目用「标题：/内容：」而不是「问题：/答案：」",
          "标题：网页标题2" in mixed and "内容：网页正文2" in mixed)

    # 图片规则那段要跟上网页也拿 [n] 这件事（D5）
    check("图片规则说明网页结果同属系统检索到的【资料】",
          "网页结果同属系统检索到的【资料】" in rag.AGENT_SYSTEM_PROMPT)


# ---------- ⑧ 泄漏恢复归属 ----------
def probe_recovery() -> None:
    banner("⑧ 泄漏到正文的工具调用：归属判定")

    original = web_search.ENABLED
    original_exa = web_search_exa.ENABLED
    try:
        web_search.ENABLED = True
        # ⚠⚠ Exa 必须**一起翻**：本机 `.env` 的 `EXA_API_KEY` 是空的（`ENABLED` 为假），
        #   不翻的话 `tools_for(True)` 里根本没有 Exa 的 schema —— 下面那几条归属断言会
        #   **静默空过**，而它们测的恰恰是「两个引擎同时在表里时谁被选走」。
        #   末尾那个参数名撞车检查同理：它遍历的就是这张表。
        web_search_exa.ENABLED = True
        tools = agent.tools_for(True)

        def rec(markup, **kw):
            return [
                (c.name, json.loads(c.arguments))
                for c in agent.recover_tool_calls(markup, prefix="t", **kw)
            ]

        leak_web = (
            '<invoke name="search_web"><parameter name="web_query">投标保证金 2026 最新</parameter></invoke>'
        )
        check(
            "web_query 形态的泄漏 → 恢复成 search_web（传了本轮工具表）",
            rec(leak_web, schemas=tools) == [("search_web", {"web_query": "投标保证金 2026 最新"})],
        )
        check(
            "⚠ 不传工具表就恢复不出来（联网只在本轮工具表里）—— 这是 P0-B 那条 bug 的形状",
            rec(leak_web) == [],
        )

        # ⚠ 「keyword」对「搜一下」来说是最自然的措辞，而 _PARAM_ALIASES 会把它改写成
        #   entity（图谱工具的主参数）。必须保证它**不会**被当成一次图谱调用执行。
        leak_kw = '<invoke name="search_web"><parameter name="keyword">最新投标保证金规定</parameter></invoke>'
        check("keyword-only 的泄漏不得变成图谱调用（开着联网）", rec(leak_kw, schemas=tools) == [])
        check("keyword-only 的泄漏不得变成图谱调用（不带工具表）", rec(leak_kw) == [])

        # ⭐ **线上 bug 的原文**：模型把工具名写对了，参数却写成 `query`。
        #   `query` 恰是 search_bidding_knowledge 的主参数 → 只按参数投票时它 1:0 独得选票
        #   → **一次联网被当成检索知识库执行了**（不是没执行，是执行错了工具）。
        #   名字精确时优先按名字归属 + search_web 的名字限定别名（query→web_query）修的
        #   就是它。⚠ 少任何一半都不行：只认名字的话 `query` 落不到 web schema 上，
        #   归一化失败又退回投票 → 打回原形。
        leak_titled_query = (
            '<invoke name="search_web"><parameter name="query">'
            '香港政府采购招标制度 物料供应处 招标程序 与中国大陆区别</parameter></invoke>'
        )
        check(
            "⚠ 名字写对 + 参数写成 query（线上 bug 原文）→ 恢复成 search_web",
            rec(leak_titled_query, schemas=tools)
            == [("search_web", {"web_query": "香港政府采购招标制度 物料供应处 招标程序 与中国大陆区别"})],
        )
        # 不带本轮工具表时它仍会被判成检索——这正是 bug 的形状，钉住它是为了说明
        # 「生产路径必须传表」。（原先这里还写着「`api._recover_web_query` 也传」，
        # 那个函数 2026-09-20 随 `/api/chat` 的兜底触发一起删了；现在生产路径上
        # **只有 `run_agent_stream` 一处**，它必须传。）
        check(
            "不传工具表时该泄漏被判成检索（bug 的形状；生产路径都传表）",
            rec(leak_titled_query) == [("search_bidding_knowledge", {"query": "香港政府采购招标制度 物料供应处 招标程序 与中国大陆区别"})],
        )

        # 既有的图谱/检索泄漏必须照旧能恢复。
        # ⚠ 这两条夹具刻意用**改名前的旧名**（现在是假名）：这样它们走的是**参数投票**
        #   路径，而「新增联网 schema 会不会把票抢走 / 把平票搅出来」问的正是投票那条路。
        #   若换成真名就成了「名字优先」，测不到这个 —— 见 probe_recover.py 的模块 docstring。
        leak_graph = (
            '<invoke name="query_knowledge_graph"><parameter name="query_type">search_entity</parameter>'
            '<parameter name="keyword">打印机</parameter></invoke>'
        )
        check(
            "原有图谱泄漏照旧恢复（投票路径：别名 + 枚举反序）",
            rec(leak_graph) == [("search_knowledge_graph", {"query_type": "entity_search", "entity": "打印机"})],
        )
        leak_search = '<invoke name="search_knowledge_base"><parameter name="query">保证金</parameter></invoke>'
        check(
            "原有检索泄漏不因新增联网 schema 而打平（投票路径）",
            rec(leak_search) == [("search_bidding_knowledge", {"query": "保证金"})],
        )

        # ---- Exa 备用引擎（第二个联网工具）----
        # ⚠⚠ 这几条的判据是**「名字精确优先」到底成不成立」**，而不是「能不能恢复出来」。
        #   两个引擎的参数名各成一派（`web_query` / `exa_query`），所以模型写对工具名、
        #   却写另一个引擎的参数名时，**名字那条路会因为「必需参数缺失」而归一化失败、
        #   退回投票**，而投票只看参数名 —— 结果就是**调用被拐到另一个引擎上执行**。
        #   两边的 `name_scoped_aliases` 各收对方那个参数名，正是为了在不改投票逻辑的前提下
        #   把它掰回来（对称的一对，改一边要看另一边）。
        def _exa(q: str) -> str:
            return f'<invoke name="search_web_exa"><parameter name="exa_query">{q}</parameter></invoke>'

        check(
            "Exa 泄漏（名字 + 自己的参数名）→ 恢复成 search_web_exa",
            rec(_exa("投标保证金 2026"), schemas=tools)
            == [("search_web_exa", {"exa_query": "投标保证金 2026"})],
        )
        check(
            "Exa 名字 + 写成 query → 名字限定别名救回来",
            rec(f'<invoke name="search_web_exa"><parameter name="query">投标保证金 2026</parameter></invoke>',
                schemas=tools)
            == [("search_web_exa", {"exa_query": "投标保证金 2026"})],
        )
        check(
            "⚠ Exa 名字 + 写成 web_query（另一个引擎的参数名）→ 仍是 Exa，不被拐去 Tavily",
            rec(f'<invoke name="search_web_exa"><parameter name="web_query">投标保证金 2026</parameter></invoke>',
                schemas=tools)
            == [("search_web_exa", {"exa_query": "投标保证金 2026"})],
        )
        check(
            "⚠ search_web 名字 + 写成 exa_query（反方向串台）→ 仍是 search_web",
            rec(f'<invoke name="search_web"><parameter name="exa_query">投标保证金 2026</parameter></invoke>',
                schemas=tools)
            == [("search_web", {"web_query": "投标保证金 2026"})],
        )
        check(
            "Exa + keyword 同样整条丢弃（与 search_web 那条对称）",
            rec(f'<invoke name="search_web_exa"><parameter name="keyword">投标保证金</parameter></invoke>',
                schemas=tools)
            == [],
        )
        # ⚠ P0-B 的形状：Exa **不在** `TOOL_SCHEMAS` 里（每请求挂的），所以不传本轮工具表
        #   就恢复不出来。少了这条，模型把 Exa 调用漏进正文时会**安静地什么都不做**。
        check(
            "⚠ 不传本轮工具表时 Exa 的泄漏恢复不出来（它是每请求工具）",
            rec(_exa("投标保证金 2026")) == [],
        )

        # ---- 参数名撞车检查 ----
        # 撞了就是「多数决打平 → 整条丢弃」或「被别的工具抢走」，是回归。
        # ⚠⚠ 它**必须留在这个 try 里面**：遍历的是 `tools_for(True)`，而 Exa 的 schema
        #   只在三个条件合取为真时才在表里 —— 本机 `EXA_API_KEY` 是空的，挪到 `finally`
        #   之后（那把 Exa 关回去了）的话，`exa_query` / `exa_max_results` 这两个名字
        #   压根不会被检查，而它们恰恰是最新加的、最需要检查的。
        seen: dict[str, list[str]] = {}
        for schema in agent.tools_for(True):
            for p in schema["function"]["parameters"].get("properties", {}):
                seen.setdefault(p, []).append(schema["function"]["name"])
        dupes = {k: v for k, v in seen.items() if len(v) > 1}
        check("两个联网工具的参数名与其它工具无撞车", not dupes, detail=f"{dupes}")

        check(
            "「泛用票仓」参数名未被使用（limit/count/size）",
            not any(p in seen for p in ("limit", "count", "size")),
        )
        # ⚠ Exa 那个 schema **确实在表里**的凭证：上面那两条断言在「Exa 没挂出去」时会
        #   平凡通过（少一个工具当然少一份撞车可能），而它们的判据恰恰依赖它在场。
        check(
            "（前提）Exa 的 schema 确实在本轮工具表里",
            "exa_query" in seen and agent.TOOL_EXA in seen["exa_query"],
            detail=f"exa_query 的持有者={seen.get('exa_query')}",
        )
    finally:
        web_search.ENABLED = original
        web_search_exa.ENABLED = original_exa

    # ⚠ 这里原先还有一整段 `api._recover_web_query()` 的断言（「`/api/chat` 收到泄漏时
    #   该不该顺势联网」）。**随 `/api/chat` 那三条兜底触发一起删了**（2026-09-20）：
    #   那条链路现在开关一打开就**必定**先联网再生成，没有「从泄漏里捞查询词补搜」这一步。
    #   上面这批 `recover_tool_calls(..., schemas=本轮工具表)` 的归属断言**必须保留** ——
    #   Agent 链路的泄漏恢复还在用（`agent.py` 的 `run_agent_stream`），CLAUDE.md 点名
    #   钉着的「一次联网调用被错判成图谱查询」那条线上 bug 全靠它。



# ---------- ⑨ 渲染文案 ----------
def probe_render() -> None:
    banner("⑨ 渲染 / 展示文案")

    empty_web = agent._render_result("最新规定", [], [], 0, kind="web")
    check("kind='web' 的空结果说「联网搜索」而不是「检索」", empty_web.startswith("联网搜索"))
    check(
        "⚠ 空结果文案**不提知识库**（联网没搜到 ≠ 知识库没有依据）",
        "知识库" not in empty_web,
        detail=empty_web.replace("\n", " / "),
    )

    # fallback_answer 对网页条目：不能冒充「知识库中匹配度最高的原始答复」
    web_fb = rag.fallback_answer([web_source(1)], False)
    check("⚠ 网页条目的降级回答不提「知识库中匹配度最高的原始答复」",
          "知识库中匹配度最高的原始答复" not in web_fb)
    check("网页条目的降级回答带标题与链接（让用户自己判断可不可信）",
          "网页标题1" in web_fb and "https://ah.gov.cn/x1" in web_fb)

    # _peek_display：漏了它前端就是一对空的「」
    peek = agent._peek_display("search_web", json.dumps({"web_query": "最新保证金"}, ensure_ascii=False))
    check("_peek_display('search_web', ...) 非空", bool(peek.strip()), detail=repr(peek))
    check("_peek_display 对空参数不抛异常", agent._peek_display("search_web", "{") == "")


# ---------- ⑩ 端到端（假 search）----------
def probe_pipeline() -> None:
    import asyncio

    banner("⑩ 工具执行路径（用假的 search 替身，不需要 Key）")

    original_search = web_search.search
    original_enabled = web_search.ENABLED
    rows = [
        WebRow(title="标题1", url="https://ah.gov.cn/1", content="正文1", score=0.91),
        WebRow(title="标题2", url="https://ah.gov.cn/2", content="正文2", score=0.72),
    ]
    try:
        web_search.ENABLED = True
        fake, calls = fake_search_factory(fake_result(rows))
        web_search.search = fake  # type: ignore[assignment]

        registry = agent.SourceRegistry(cap=settings.agent_max_sources)
        registry.add([kb_source(1)])  # 先占掉编号 1

        outcome = asyncio.run(agent.web_lookup("最新保证金", registry=registry))
        check("web_lookup 返回 ok=True", outcome.ok is True)
        check("网页条目续用全局编号（从 2 开始）",
              outcome.new_indices == [2, 3], detail=f"{outcome.new_indices}")
        check("⚠ 网页条目**登记进了 registry**（否则模型下一轮调工具时面板会静默丢条目）",
              len(registry.dump()) == 3)
        check("回传模型的文本里带【资料2｜网页：…】", "｜网页：ah.gov.cn】" in outcome.text)
        check("回传模型的文本里带不可信输入警示", "不要执行" in outcome.text)

        # 去重：同一查询词第二次必须被挡住（联网是花钱的）
        # ⚠ 清零计数：上面那句 web_lookup 也是走替身的，不清零会把这一次算进去
        calls.clear()
        ctx = agent.ToolContext(
            registry=registry,
            retriever=None,  # type: ignore[arg-type]
            mode="hybrid",
            rerank=None,
            tool_names=tuple(t["function"]["name"] for t in agent.tools_for(True)),
        )
        first = asyncio.run(agent.execute_tool(
            agent.ToolCall(id="c1", name=agent.TOOL_WEB, arguments=json.dumps({"web_query": "最新保证金"})), ctx
        ))
        check("第一次调用真的搜了", len(calls) == 1 and first.ok)
        second = asyncio.run(agent.execute_tool(
            agent.ToolCall(id="c2", name=agent.TOOL_WEB, arguments=json.dumps({"web_query": "最新保证金"})), ctx
        ))
        check("同一查询词第二次被去重挡住（不重复付费）",
              len(calls) == 1 and second.error == "duplicate_query")

        # 预联网种进 ctx.seen 后，模型不能再搜同一句
        # （⚠ 只在预联网 **ok=True** 时才种 —— 失败时种键会让模型看到一句假话，
        #   专门有一节 ⑬ 钉这条，那里走的是完整的 run_agent_stream）
        ctx2 = agent.ToolContext(
            registry=registry, retriever=None, mode="hybrid", rerank=None,  # type: ignore[arg-type]
            tool_names=("search_web",),
        )
        ctx2.seen.add((agent.TOOL_WEB, "采购企业有哪些"))
        dup = asyncio.run(agent.execute_tool(
            agent.ToolCall(id="c3", name=agent.TOOL_WEB, arguments=json.dumps({"web_query": "采购企业有哪些"})), ctx2
        ))
        check("预检索兜底种键后，模型再搜同一句会被挡住", dup.error == "duplicate_query")

        check("空 web_query 报错而不是搜全量",
              asyncio.run(agent.execute_tool(
                  agent.ToolCall(id="c4", name=agent.TOOL_WEB, arguments="{}"), ctx2
              )).error == "empty_query")

        # 失败路径：ok=False 时必须把原因转成文本，绝不能抛
        web_search.search = fake_search_factory(fake_result([], ok=False, note="429 Too Many Requests"))[0]  # type: ignore[assignment]
        failed = asyncio.run(agent.web_lookup("任意", registry=agent.SourceRegistry(cap=12)))
        check("search 失败时 web_lookup 不抛异常、ok=False", failed.ok is False)
        check("失败原因进了回传文本", "429" in failed.text, detail=failed.text[:80])
        check("⚠ 失败文案说「未能完成」而非「没有新增资料」（两者含义不同）",
              "未能完成" in failed.text and "没有新增资料" not in failed.text)

        # 工具执行永不抛异常（不变量 1）+ 未知工具报错用的是本轮工具表
        check(
            "未知工具的报错列出**本轮**工具表（含 search_web）",
            "search_web" in asyncio.run(agent.execute_tool(
                agent.ToolCall(id="c5", name="nope", arguments="{}"), ctx
            )).text,
        )
    finally:
        web_search.search = original_search  # type: ignore[assignment]
        web_search.ENABLED = original_enabled


# ---------- ⑫ /api/chat：开关打开 ⇒ 必定本地 + 联网都查（完整事件序列，全部替身）----------
def probe_chat_always_web() -> None:
    """端到端跑 `/api/chat` 的 event_stream，验「开关打开就必定本地与联网都查一遍」。

    需求原文：「选择了「联网搜索」后，**必需**检索本地知识库和联网搜索，并基于知识库检索
    结果和联网搜索结果回答用户问题。」本节钉的就是那个「**必需**」在 `/api/chat` 上的形状。

    ⚠ 这一节**只该有一个 kind→SSE 的映射**（api.py 里的 `_run_pass()`）。最容易踩的坑是把
    `markup_leak` 落到兜底的 `else` 分支上——那会把它当 `delta` 原样发给前端，等于修这个
    bug 反而亲手把标记送出去。下面第一条正例里就有这条断言。

    三条**语义变更**是本节的回归重点（改回兜底语义就会立刻变红）：

    1. 本地**答得出**也照样联网 —— 原先只有「本地 0 召回 / 正文泄漏 / 模型说答不出」
       三种情况才搜。
    2. `answer_reset` 在 `/api/chat` 上**彻底消失**：联网发生在任何正文流出**之前**，
       没有「让我联网搜索」那种前导正文要作废，生成只跑一遍。
    3. 第二次 `sources` 必须排在**第一个 delta 之前** —— 这是「联网在生成之前，不是事后补救」
       的结构性证据。

    替身清单（都不碰网络）：`_require_ready` / `get_retriever` / `get_llm` /
    `stream_answer` / `web_lookup`，加上 `web_search.ENABLED` 这个开关。
    ⚠ 替身**故意不实现 `ok` / `text`**（`api._log_web_outcome` 走 getattr）——
       它就在请求路径上，字段缺失时抛异常会把整条请求变成 error 事件。
    """
    import asyncio
    from types import SimpleNamespace

    banner("⑫ /api/chat：开关打开 ⇒ 本地检索 + 联网搜索，两份一起作答")

    from bidding_qa import api

    QUESTION = "在招投标法规流程方面，中国大陆和香港有什么不同？"
    ANSWERED = "履约保证金的最高限额不得超过中标合同金额的 10%。[1]"
    # 模型如实报告「答不出」—— 旧语义下这是第三次联网的触发点，现在它**什么都不该触发**
    NO_ANSWER = "根据现有资料，暂未找到明确依据。建议从两地法规体系差异入手。"
    # 故意带一个 image_context：查询词必须是**原问题**，不是拼了图片上下文那个串
    # （图片内容只进提示词、绝不进检索 —— 见 rag.with_image_context 的 docstring）。
    # 不带它的话两个串相等，那条断言就成了空转。
    IMG = "【图片解析】项目名称：某某县高标准农田建设工程；招标人：某某县农业农村局"

    kb = [kb_source(1), kb_source(2)]
    web_src = web_source(91)

    state: dict[str, object] = {"kb": list(kb), "answer": ANSWERED, "leak": False}
    passes: list[str] = []
    web_calls: list[tuple] = []

    class _Retriever:
        last_latency = {"vector_ms": 3.0, "bm25_ms": 1.0}

        def search(self, *args, **kwargs):
            return list(state["kb"])  # type: ignore[arg-type]

    class _LLM:
        ready = True

    bar = chr(0xFF5C)
    token = bar * 2 + "DSML" + bar * 2
    leak_markup = (
        f"</{token}tool_calls> <{token}invoke name=\"search_web\"> "
        f"<{token}parameter name=\"web_query\" string=\"true\">"
        f"香港政府采购招标制度 物料供应处 招标程序 与中国大陆差异</{token}parameter> "
        f"</{token}invoke> </{token}tool_calls>"
    )

    async def fake_stream_answer(llm, question, sources, history, *, use_llm=True):
        """每轮只吐一次正文。`state["answer"]` 决定它是答得出还是答不出。"""
        passes.append(question)
        if state["leak"]:
            yield ("reasoning", "本地知识库里似乎没有香港的规定。")
            yield ("content", "让我联网搜索补充香港的相关规定。")
            yield ("markup_leak", leak_markup)
            return
        yield ("content", str(state["answer"]))

    async def fake_web_lookup(query, *, registry=None, max_results=None):
        web_calls.append((query, max_results))
        # ⚠ 现在**三个调用点全都传 registry**（本地先占住 1..K，网页续接编号）。
        #   断言里钉住 registry 非空，免得日后有人「顺手」退回 to_sources() 的 1..N。
        assert registry is not None, "「必定联网」必须传 registry，否则会和本地条目撞号"
        return SimpleNamespace(new_sources=registry.add([web_src])[0], elapsed_ms=88.0)

    async def empty_web_lookup(query, *, registry=None, max_results=None):
        web_calls.append((query, max_results))
        return SimpleNamespace(new_sources=[], elapsed_ms=88.0)

    saved = {
        "require_ready": api._require_ready,
        "get_retriever": api.get_retriever,
        "get_llm": api.get_llm,
        "stream_answer": api.stream_answer,
        "web_lookup": api.web_lookup,
        "enabled": web_search.ENABLED,
    }

    def run(*, web_search_flag: bool = True, image_context: str = "", use_llm: bool = True):
        async def _go():
            payload = api.ChatRequest(
                question=QUESTION,
                web_search=web_search_flag,
                rerank=False,
                image_context=image_context,
                use_llm=use_llm,
            )
            resp = await api.chat(payload)
            return [
                json.loads(chunk[len("data: ") :].strip()) async for chunk in resp.body_iterator
            ]

        return asyncio.run(_go())

    try:
        api._require_ready = lambda: None  # type: ignore[assignment]
        api.get_retriever = lambda: _Retriever()  # type: ignore[assignment]
        # ⚠ 必须能收**位置参数**：`/api/chat` 与 `/api/agent` 现在按请求传
        #   `get_llm(payload.llm_provider or None)`（2026-09-21 起）。
        #   写成 `lambda: _LLM()` 会抛 `TypeError: takes 0 positional arguments but 1 was given`
        #   —— 好在它死在 try 里、finally 会恢复，是**响的**失败，不是静默降级。
        api.get_llm = lambda *_a, **_k: _LLM()  # type: ignore[assignment]
        api.stream_answer = fake_stream_answer  # type: ignore[assignment]
        api.web_lookup = fake_web_lookup  # type: ignore[assignment]
        web_search.ENABLED = True

        # ---- 正例：本地答得出，也照样联网（这就是语义变更本身）----
        events = run(image_context=IMG)
        kinds = [e["type"] for e in events]
        check(
            "事件序列：start → sources(本地) → sources(本地+网页) → delta → done",
            kinds == ["start", "sources", "sources", "delta", "done"],
            detail=f"{kinds}",
        )
        # 用**位置推导**而非写死下标：多一个 delta 不该让断言错位，「顺序」才是这里要钉的。
        first_src = kinds.index("sources")
        second_src = kinds.index("sources", first_src + 1)
        first_delta = kinds.index("delta")

        # ⭐ 整节的命门：联网必须在**任何正文流出之前**。排在 delta 之后的话，用户就先看到
        #    一句没有依据的回答、再看到来源卡片补上来——那正是被删掉的兜底语义的形状。
        check(
            "⭐ 第二次 sources 必须排在第一个 delta **之前**（联网在生成之前，不是事后补救）",
            second_src < first_delta,
            detail=f"second_src={second_src} first_delta={first_delta}",
        )
        check(
            "⚠ `/api/chat` 不再产生 answer_reset（联网在正文之前，没有前导正文要作废）",
            "answer_reset" not in kinds,
            detail=f"{kinds}",
        )
        check(
            "首屏 sources 只有本地条目（秒出，不等联网）",
            [s["index"] for s in events[first_src]["sources"]] == [1, 2],
            detail=f"{[s['index'] for s in events[first_src]['sources']]}",
        )
        check(
            "⚠ 联网用的是**原问题**，不是拼了图片上下文那个串（图片只进提示词）",
            web_calls == [(QUESTION, None)],
            detail=f"{web_calls}",
        )
        llm_q = rag.with_image_context(QUESTION, IMG)
        check(
            "送进大模型的问题**才**拼图片上下文（检索与提示词各走各的）",
            llm_q != QUESTION and passes[0] == llm_q,
            detail=f"{passes[0][:40]}",
        )
        check(
            "编号续接：本地占 1、2，网页接 3 且 origin=web（撞号的话正文角标会滚错地方）",
            [s["index"] for s in events[second_src]["sources"]] == [1, 2, 3]
            and events[second_src]["sources"][2]["origin"] == "web",
            detail=f"{[(s['index'], s['origin']) for s in events[second_src]['sources']]}",
        )
        check(
            "重发 sources 带完整的 latency_ms（既有约定：sources 是整体替换）",
            {"vector_ms", "bm25_ms", "retrieve_ms", "web_ms"}
            <= set(events[second_src]["latency_ms"]),
            detail=f"{events[second_src]['latency_ms']}",
        )
        check("一轮只生成一遍（不再有「补搜之后重答」那第二遍）", len(passes) == 1, detail=f"{passes}")
        check("一轮最多联网一次（防重复花钱）", len(web_calls) == 1, detail=f"{web_calls}")

        # ---- 正例：模型答**不出**，语义与答得出时完全相同 ----
        # 旧语义下这句「暂未找到明确依据」是第三次联网的触发点（由 _looks_like_no_answer 判），
        # 现在它什么都不该触发 —— 联网早就做完了。
        passes.clear()
        web_calls.clear()
        state["answer"] = NO_ANSWER
        events = run()
        kinds = [e["type"] for e in events]
        check(
            "⚠ 模型说「答不出」不再触发任何补搜/重答（语义已变，事件序列与答得出时一致）",
            kinds == ["start", "sources", "sources", "delta", "done"]
            and len(web_calls) == 1
            and len(passes) == 1,
            detail=f"{kinds} calls={len(web_calls)} passes={len(passes)}",
        )
        state["answer"] = ANSWERED

        # ---- 正例：0 召回 + 开关开 → 第二次 sources 是**纯网页**（无本地前缀）----
        passes.clear()
        web_calls.clear()
        state["kb"] = []
        events = run()
        kinds = [e["type"] for e in events]
        first_src = kinds.index("sources")
        second_src = kinds.index("sources", first_src + 1)
        check(
            "本地 0 召回时首屏是空 sources（前端 `sources.length > 0` 不渲染面板，不闪）",
            events[first_src]["sources"] == [],
            detail=f"{events[first_src]['sources']}",
        )
        check(
            "本地 0 召回 → 第二次 sources 是纯网页，编号从 1 起（本地没占编号）",
            [s["index"] for s in events[second_src]["sources"]] == [1]
            and events[second_src]["sources"][0]["origin"] == "web",
            detail=f"{[(s['index'], s['origin']) for s in events[second_src]['sources']]}",
        )
        state["kb"] = list(kb)

        # ---- 正例：标记被拦下 → 一个字都不许漏给前端 ----
        # ⚠ 恢复路径（把标记还原成真调用去执行）随那三条兜底一起删了，现在只抑制 + 记日志。
        #   但**抑制必须照旧生效**：落到 `_run_pass()` 的 `else` 分支就是亲手把标记送出去。
        passes.clear()
        web_calls.clear()
        state["leak"] = True
        events = run()
        check(
            "⚠⚠ 整个 SSE 流里没有任何标记 token（否则等于亲手把标记发给前端）",
            "DSML" not in json.dumps(events, ensure_ascii=False),
        )
        check(
            "泄漏轮照样先联网再一次生成（抑制不影响联网时机）",
            len(web_calls) == 1 and len(passes) == 1,
            detail=f"calls={len(web_calls)} passes={len(passes)}",
        )
        state["leak"] = False

        # ---- 负例：开关关着 / 服务端没配 Key → 与改动前逐字节相同 ----
        for label, flag, enabled in (
            ("用户没开联网开关", False, True),
            ("服务端没配 TAVILY_API_KEY", True, False),
        ):
            passes.clear()
            web_calls.clear()
            web_search.ENABLED = enabled
            events = run(web_search_flag=flag)
            kinds = [e["type"] for e in events]
            check(
                f"{label} → 不联网、单个 sources、无 answer_reset",
                web_calls == [] and kinds == ["start", "sources", "delta", "done"],
                detail=f"{kinds} calls={web_calls}",
            )
            # ⚠ 冻结路径的另一半：本地条目的编号在开关关着时必须还是 [1, 2]。
            #   联网那段会先 registry.add(sources) 再重盖编号，重盖后**必须逐个相同**
            #   （retriever 是 `enumerate(kept, start=1)`，registry 是「按登记顺序盖 len+1」，
            #   同批同序必然相等）。不相等就说明重盖改变了本地条目，是回归。
            check(
                f"{label} → 本地条目编号仍是 [1, 2]（开关关着不该动任何东西）",
                [s["index"] for s in events[kinds.index("sources")]["sources"]] == [1, 2],
                detail=f"{events[kinds.index('sources')]['sources']}",
            )
        web_search.ENABLED = True

        # ---- 负例：use_llm=False → 不联网 ----
        # 没有大模型时 stream_answer 退化成「把第一条知识库答案原样端出来」，搜回来的网页
        # **没人会读**，只是白花一次 credit。这不是给「必定联网」开后门——用户要的是
        # **作答时**用上联网结果。
        passes.clear()
        web_calls.clear()
        events = run(use_llm=False)
        kinds = [e["type"] for e in events]
        check(
            "use_llm=False → 不联网（搜回来没人读，纯浪费一个 credit）",
            web_calls == [] and kinds == ["start", "sources", "delta", "done"],
            detail=f"{kinds} calls={web_calls}",
        )

        # ---- 负例：搜到了但一条都没新增（全被闸门丢掉 / 全被判成已知）→ 不发第二次 sources ----
        # ⚠ 与「压根没触发」要区分开：**这一轮确实发起了联网**，只是空手而归。
        passes.clear()
        web_calls.clear()
        api.web_lookup = empty_web_lookup  # type: ignore[assignment]
        events = run()
        kinds = [e["type"] for e in events]
        check(
            "⚠ 一条新来源都没有 → 不发第二次 sources（重发一张同样的表毫无作用）",
            kinds == ["start", "sources", "delta", "done"] and len(passes) == 1,
            detail=f"{kinds} passes={len(passes)}",
        )
        check(
            "（对照）这一轮**确实**发起了联网，只是空手而归 —— 与「压根没触发」区分开",
            len(web_calls) == 1,
            detail=f"{web_calls}",
        )
        check(
            "空手而归时 latency 里不该有 web_ms（那个键只在真拿到新来源时才写）",
            "web_ms" not in events[kinds.index("sources")]["latency_ms"],
            detail=f"{events[kinds.index('sources')]['latency_ms']}",
        )
        api.web_lookup = fake_web_lookup  # type: ignore[assignment]
    finally:
        api._require_ready = saved["require_ready"]  # type: ignore[assignment]
        api.get_retriever = saved["get_retriever"]  # type: ignore[assignment]
        api.get_llm = saved["get_llm"]  # type: ignore[assignment]
        api.stream_answer = saved["stream_answer"]  # type: ignore[assignment]
        api.web_lookup = saved["web_lookup"]  # type: ignore[assignment]
        web_search.ENABLED = saved["enabled"]


# ---------- ⑬ Agent：预联网的种键条件（`ok=True` 才种）----------
def probe_agent_presearch() -> None:
    """`run_agent_stream` 的预联网在**什么条件下**把 `(search_web, 原问题)` 种进 `ctx.seen`。

    这一段修的是一个 2026-09-20 引入的静默错答案：预联网**失败**（冷却 / 429 / 401 / 超时，
    `web_lookup` 回 `ok=False`）时若照样种键，模型随后老老实实调 `search_web` 搜同一句，
    撞上 `_run_web` 的去重分支拿到的是「**该调用已执行过，结果与上次完全相同**」——
    而事实是**什么都没查到**。那句话会把 `_render_result(kind="web")` 特意保留的
    「未能完成 ≠ 没有新增资料」整条抹平，模型据此告诉用户「联网也没有搜到」。

    正例与负例**必须成对**：只钉负例的话，「干脆什么都不种键」也能通过，
    而那会让模型把同一句查询重复付两次 Tavily 费用。

    替身：`web_search.search` 与一个两轮的 `_FakeLLM`（第一轮调 search_web，第二轮收尾）。
    ⚠ 用 `enable_web=True` 走**真**工具表（`agent.tools_for`），别手搓 schema——
    归属、去重、`_match_schema` 都依赖它。
    """
    import asyncio

    from bidding_qa.llm import LLMEvent, ToolCall

    banner("⑬ Agent：预联网失败时不该种键（否则模型会看到一句假话）")

    QUESTION = "采购企业有哪些？"

    class _Retriever:
        last_latency: dict = {}

        def search(self, query, top_k, mode, rerank):  # noqa: ANN001
            # 预检索恒为空 —— 本节判据与本地召回无关，清空只为让计数干净
            return []

    class _FakeLLM:
        """第一轮调 search_web（查询词**就是原问题**，与预联网那句完全相同），第二轮收尾。

        ⚠ 工具的回传文案**不在 `tool_result` 事件里**（那个载荷只有 ok / error / 编号等，
        见 agent.py 的 yield 处）——它落在 `role="tool"` 那条消息的 `content` 上。所以第二轮
        把它抠出来，这正是模型实际读到的文字。
        """

        def __init__(self) -> None:
            self.round = 0
            self.tool_messages: list[str] = []

        @property
        def ready(self) -> bool:
            return True

        async def stream_with_tools(self, messages, *, tools=None, tool_choice=None, **kw):  # noqa: ANN001
            self.round += 1
            if self.round == 1:
                yield LLMEvent(
                    type="message",
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name=agent.TOOL_WEB,
                            arguments=json.dumps({"web_query": QUESTION}),
                        )
                    ],
                )
            else:
                self.tool_messages += [
                    str(m.get("content", "")) for m in messages if m.get("role") == "tool"
                ]
                yield LLMEvent(type="content", text="好的。")
                yield LLMEvent(type="message", content="好的。", tool_calls=[], finish_reason="stop")

    def drive() -> tuple[list[tuple[str, dict]], str]:
        llm = _FakeLLM()

        async def _go():
            events: list[tuple[str, dict]] = []
            async for ev in agent.run_agent_stream(
                llm,  # type: ignore[arg-type]
                _Retriever(),  # type: ignore[arg-type]
                question=QUESTION,
                session_id=None,
                history=[],
                top_k=None,
                mode="hybrid",
                rerank=False,
                enable_web=True,
            ):
                events.append(ev)
            return events

        return asyncio.run(_go()), "\n".join(llm.tool_messages)

    original_enabled = web_search.ENABLED
    original_search = web_search.search
    try:
        web_search.ENABLED = True
        assert any(t["function"]["name"] == agent.TOOL_WEB for t in agent.tools_for(True)), (
            "工具表里没有 search_web，本节的前提不成立"
        )

        # ---- 正例：预联网**成功**（含「Tavily 正常应答但 0 条」）→ 种键，重搜同一句被挡 ----
        ok_fake, ok_calls = fake_search_factory(fake_result([], ok=True, note="联网搜索没有返回任何结果。"))
        web_search.search = ok_fake  # type: ignore[assignment]
        _, text = drive()
        check("（前提）预联网那次真的发起了请求", len(ok_calls) == 1, detail=f"{ok_calls}")
        check(
            "预联网成功（ok=True）→ 模型重搜同一句被去重挡住（不重复付费）",
            "已执行过" in text,
            detail=text[:120],
        )
        check(
            "成功那轮模型的重搜**没有**再落到 search() 上（被 ctx.seen 短路了）",
            len(ok_calls) == 1,
            detail=f"{len(ok_calls)} 次（应当只有预联网那一次）",
        )

        # ---- 负例：预联网**失败** → 不种键，模型重搜时拿到的是**真实原因** ----
        fail_fake, fail_calls = fake_search_factory(
            fake_result([], ok=False, note="429 Too Many Requests")
        )
        web_search.search = fail_fake  # type: ignore[assignment]
        _, text = drive()
        check(
            "⚠⚠ 预联网失败（ok=False）→ 不种键，模型重搜拿到的是**真实失败原因**",
            "未能完成" in text and "已执行过" not in text,
            detail=text[:200],
        )
        check(
            "⚠ 更不能说成「没有新增资料」——「没搜成」与「网上没有」是两件事",
            "没有新增资料" not in text,
            detail=text[:200],
        )
        check(
            "失败那轮模型的重搜落到了真实的 search() 上（不是被去重短路掉）",
            len(fail_calls) == 2,
            detail=f"{len(fail_calls)} 次（1 次预联网 + 1 次模型重搜）",
        )
    finally:
        web_search.ENABLED = original_enabled
        web_search.search = original_search  # type: ignore[assignment]


# ---------- 真机 ----------
def probe_live() -> None:
    import asyncio

    banner("⑪ 真机（需要 TAVILY_API_KEY 与网络）")

    print(f"  base_url={settings.tavily_base_url}  depth={settings.tavily_search_depth}"
          f"  max_results={settings.tavily_max_results}  timeout={settings.tavily_timeout}s")
    if not settings.tavily_api_key.strip():
        print("  ✗ 未配置 TAVILY_API_KEY，跳过。代码路径已由 ①~⑩ 覆盖。")
        print("    申请地址 https://tavily.com（免费档 1000 credits/月），写进 .env 后重跑本节。")
        return

    result = asyncio.run(web_search.search("2026年 投标保证金 最新规定"))
    if not result.ok:
        print(f"  ✗ 搜索失败：{result.note}")
        return

    print(f"  返回 {result.total} 条，耗时 {result.elapsed_ms:.0f}ms")
    print("  ---- 实测相关性分数分布（标定 web_search_min_score 的依据）----")
    for row in result.rows:
        print(f"    {row.score:.4f}  {row.published_date or '（无日期）':12s}  {row.title[:40]}")
    scores = [r.score for r in result.rows]
    if scores:
        print(f"  min={min(scores):.4f}  max={max(scores):.4f}  条数={len(scores)}")
        print("  ⚠ Tavily 的 score **不是我们的相似度**：它与 MIN_VECTOR_SCORE 不可比。")
        print("    要设阈值就按**实测分布**来，别照抄一个「看起来合理」的数（盐化工那次的教训）。")
    print(f"  ---- 分数闸门（min_score={settings.web_search_min_score}，判据是**这批的 top 分**）----")
    if scores:
        top = max(scores)
        passed = settings.web_search_min_score <= 0 or top >= settings.web_search_min_score
        print(f"    top={top:.4f} → {'放行（整批保留）' if passed else '整批丢弃'}")
        print("    ⚠ 重标定看的是**这批查询的 top 分**与那些「网上根本没答案」的查询的 top 分")
        print("      之间那条空带，不是逐行分——逐行筛会把真相关但排名靠后的结果误杀。")
    print("  ---- 转成 Source ----")
    for s in web_search.to_sources(result)[:3]:
        print(f"    [{s.index}] {s.question[:36]}  score={s.score:.3f}  {s.url}")


def main() -> int:
    print("联网搜索层自检")
    print(f"  TAVILY_API_KEY={'已配置' if settings.tavily_api_key.strip() else '未配置'}"
          f"  ENABLED={web_search.ENABLED}")

    probe_wiring()
    probe_settings_regression()
    probe_degradation()
    probe_to_sources()
    probe_empty_retry()
    probe_json()
    probe_frozen_paths()
    probe_matrix()
    probe_recovery()
    probe_render()
    probe_pipeline()
    probe_chat_always_web()
    probe_agent_presearch()
    probe_live()

    banner(f"结果：{_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
