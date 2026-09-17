"""验证 `/api/*` 的 slowapi 限流：接线、真触发、豁免、分桶、以及它没弄坏 SSE。

**全离线**：一条网络都不发。限额在探针里被临时改成 3 以便打满，用 `TestClient`
直接打 ASGI app（**刻意不进 `with`** —— 进了会跑 lifespan → `_bootstrap()` →
加载索引与向量模型几十秒；而且限流发生在中间件里，压根不需要服务就绪）。

⚠⚠ 两个**实测踩出来的** slowapi 细节，本文件的断言就是为它们写的：

1. **限额是「每个端点」的，不是全站合计**。桶键是 `(key_func(), 端点函数名)`
   （`extension.py:570` 的 `_endpoint_key`，默认取 `f"{func.__module__}.{func.__name__}"`）。
   所以 `/api/chat` 与 `/api/agent` 各有一个 120/分钟的桶。③ 钉着这条。
2. **429 处理器必须是同步的 `def`**。`SlowAPIMiddleware` 走 `sync_check_limits`，
   那里对协程处理器会**静默**退回 slowapi 的英文默认文案 —— 不报错，只是用户读到英文。
   ① 用 `iscoroutinefunction` 钉着它。

⚠ ③ 的 `/api/health` 豁免**不是洁癖**：前端轮询间隔是
`status === "ok" ? 30000 : 4000`，健康检查一旦被 429，`status` 就不再是 `ok`，
前端会把轮询**加快到每 4 秒一次** —— 正好去撞更紧的限流，是个正反馈。

⚠ `settings.rate_limit_per_minute` **必须在 import `api` 之前改**：限额字符串是
`api.py` 的模块级 `_limits`，import 期算一次就定死了。

跑法：

    uv run python scripts/probe_rate_limit.py
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa.config import settings  # noqa: E402

failures: list[str] = []
count = 0

#: 真实配置（.env 里那个），探针跑完恢复回去
_CONFIGURED = settings.rate_limit_per_minute
#: 探针里用的极小额度 —— 要在几十次请求内打满，不能真的打 120 次
_TIGHT = 3

# ⚠⚠ 顺序要紧：必须在 `import bidding_qa.api` **之前**改完。
settings.rate_limit_per_minute = _TIGHT
from bidding_qa import api  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402
from starlette.testclient import TestClient as _TC  # noqa: E402,F401  (存在性即依赖)


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


def note(text: str) -> None:
    print(f"      {text}")


# ================================================================ ① 接线
def probe_wiring() -> None:
    banner("① 接线：限额读到了、健康检查豁免、429 处理器是同步的、用对了中间件")

    check("settings 里的值被 api 读到（limiter 开着）", api.limiter.enabled, True,
          f"（探针把 rate_limit_per_minute 临时改成 {_TIGHT}，真实配置是 {_CONFIGURED}）")
    # ⚠ 不断言限额**字符串**本身：`_default_limits` 里装的是 `LimitGroup` 对象，
    #   要读出 "3/minute" 得伸手进它的私有 `_LimitGroup__limit_provider` —— 那是
    #   钉 slowapi 内部实现，升级一次就假红。**「生效的确实是 3/分钟」由 ② 行为验证**
    #   （第 4 次 429 + 文案里点名限额数），那比读字符串有力得多。
    check("默认限额挂了一组（非空即表示 default_limits 真的接上了）",
          len(api.limiter._default_limits), 1)

    # ⚠⚠ 这条**必须**是 `def` 而不是 `async def`：中间件路径走 `sync_check_limits`，
    #   那里对协程处理器**静默**退回 slowapi 的英文 `_rate_limit_exceeded_handler`。
    check("⭐ 429 处理器是同步函数（异步会被静默换成英文兜底）",
          __import__("inspect").iscoroutinefunction(api._rate_limited), False)
    from slowapi.errors import RateLimitExceeded

    check("它确实注册在 app 上（中间件是从 app.exception_handlers 取的）",
          api.app.exception_handlers.get(RateLimitExceeded) is api._rate_limited, True)

    # ⚠ 豁免是靠「端点函数名」匹配的（`_get_route_name` = `模块名.函数名`）。
    #   若哪天 health 被挪进别的模块/改了名，这条会红 —— 那正是它该红的时候。
    check("⭐ /api/health 在豁免名单里", "bidding_qa.api.health" in api.limiter._exempt_routes, True)
    health = next(r for r in api.app.routes if getattr(r, "path", "") == "/api/health")
    check("  且路由上那个端点的名字**逐字**对得上（豁免靠它匹配）",
          f"{health.endpoint.__module__}.{health.endpoint.__name__}", "bidding_qa.api.health")

    classes = [m.cls for m in api.app.user_middleware]
    check("中间件栈里有 SlowAPIMiddleware", SlowAPIMiddleware in classes, True)
    # ⚠ 纯 ASGI 那个**不能用**：它的 send_wrapper 每收到一个 `http.response.body`
    #   就重发一次 `http.response.start`，对 SSE（几百个分片）是协议违例。
    from slowapi.middleware import SlowAPIASGIMiddleware

    check("⭐⭐ 且**没有**误用 SlowAPIASGIMiddleware（它会重发 start，SSE 必炸）",
          SlowAPIASGIMiddleware in classes, False)


# ================================================================ ② 真触发
def probe_trigger() -> None:
    banner(f"② 真行为：打满 {_TIGHT} 次之后真的 429、文案是中文、带 Retry-After")

    client = TestClient(api.app)
    codes = [client.get("/api/config").status_code for _ in range(_TIGHT)]
    check(f"前 {_TIGHT} 次都不限流", codes, [200] * _TIGHT)

    r = client.get("/api/config")
    check(f"第 {_TIGHT + 1} 次是 429", r.status_code, 429)

    body = r.json()
    check("429 响应体是 JSON 且有 detail", isinstance(body.get("detail"), str), True)
    # ⭐ 这条是「同步处理器真的生效了」的**唯一**凭证：走 slowapi 的英文兜底时
    #   文案是 "Rate limit exceeded: ..."，不是这句中文。
    check("⭐ 文案是中文那句（不是 slowapi 的英文兜底）",
          "请求过于频繁" in body.get("detail", ""), True, body.get("detail", ""))
    check("文案里点名了限额数", str(_TIGHT) in body.get("detail", ""), True)
    check("⭐ 带 Retry-After（slowapi 在中间件路径上不注入，是 api 自己补的）",
          r.headers.get("retry-after"), "60")

    # 打满之后再打还是 429（桶不会被一次拒绝重置）
    check("继续打仍是 429", client.get("/api/config").status_code, 429)


# ================================================================ ③ 豁免与分桶
def probe_bucket_and_exempt() -> None:
    banner("③ 分桶：豁免的不受限、每个端点各一个桶（⚠ 是「每端点」不是「全站」）")

    client = TestClient(api.app)

    # /api/config 的桶此刻是满的（② 打满的），所以下面每条都是「在桶满时」测的 ——
    # 这正是豁免与分桶该被验证的时刻。
    health = [client.get("/api/health").status_code for _ in range(20)]
    check("⭐⭐ 桶满时 /api/health 连打 20 次，一次 429 都没有",
          [c for c in health if c != 200], [])
    note(f"（health 返回 {sorted(set(health))}；未就绪时是 503，**但不是 429**，"
         f"这才是豁免的判据）")

    # ⚠⚠ 每个端点一个桶 —— 这条**反直觉**，是本文件最值得留下的断言。
    #   若哪天 slowapi 换了 `key_style`（`"url"` 会按路径分桶），或我们在 `Limiter(...)`
    #   里传了共享 scope，两条腿都会变，这条会红。
    search = client.post("/api/search", json={"query": "x", "mode": "hybrid", "top_k": 1})
    check("⭐⭐ /api/config 的桶满了，/api/search 仍不受影响（各端点各一个桶）",
          search.status_code != 429, True, f"实际 {search.status_code}")

    chat = client.post("/api/chat", json={"question": "x", "rerank": False})
    check("    /api/chat 也不受影响（它有自己的桶）", chat.status_code != 429, True,
          f"实际 {chat.status_code}")


# ================================================================ ④ SSE 没被弄坏
def probe_sse_survives() -> None:
    banner("④ SSE 仍能穿过中间件：事件序列完整（替身，不碰网络）")

    # ⚠ 必须是**真的** `Source`，不能用 SimpleNamespace 糊：`event_stream` 里那句
    #   `s.model_dump()` 是硬性的，替身会在 SSE 生成到一半时抛 AttributeError ——
    #   而那时 HTTP 头已经发出去了，表现是一个中途断掉的流，不好查。
    from bidding_qa.schemas import Source

    kb = [Source(
        index=1, doc_id="kb1", question="q1", answer="a1", snippet="s1",
        score=0.9, vector_score=0.9, bm25_score=1.0, matched_by=["vector"],
    )]

    class _Retriever:
        last_latency = {"vector_ms": 3.0, "bm25_ms": 1.0}

        def search(self, *args, **kwargs):
            return list(kb)

    class _LLM:
        ready = True

    async def fake_stream_answer(llm, question, sources, history=None, *, use_llm=True):
        yield ("reasoning", "先想一下。")
        yield ("content", "这是回答。")

    saved = {n: getattr(api, n) for n in ("_require_ready", "get_retriever", "get_llm", "stream_answer")}
    try:
        api._require_ready = lambda: None  # type: ignore[assignment]
        api.get_retriever = lambda: _Retriever()  # type: ignore[assignment]
        api.get_llm = lambda *_a, **_k: _LLM()  # type: ignore[assignment]
        api.stream_answer = fake_stream_answer  # type: ignore[assignment]

        client = TestClient(api.app)
        r = client.post("/api/chat", json={"question": "x", "rerank": False, "web_search": False})
        check("HTTP 200 且是事件流", (r.status_code, r.headers.get("content-type", "").startswith("text/event-stream")), (200, True))

        kinds = [json.loads(line[len("data: "):])["type"]
                 for line in r.text.splitlines() if line.startswith("data: ")]
        check("事件序列完整且有序（含 delta）", kinds, ["start", "sources", "reasoning", "delta", "done"])
        check("正文分片真的到了", any(line.startswith("data: ") and "这是回答。" in line
                                      for line in r.text.splitlines()), True)
    finally:
        for name, value in saved.items():
            setattr(api, name, value)

    # ⚠ **没能在这里钉住的**：`BaseHTTPMiddleware` 会不会把整条流**缓冲**到最后才吐。
    #   TestClient 是进程内 ASGI，一个「攒完再放」的中间件在这里看起来与真流式一样。
    #   这条只能真机验：实测 `/api/agent` 一次请求的事件到达时刻是
    #   start 0.02s → sources 1.27s → reasoning 1.71s（79 个分片）→ delta 3.03s，
    #   **确实是边生成边到**，没有被中间件攒住。别把 ④ 当成那条性质的凭证。


# ================================================================ ⑤ 0 = 关闭
def probe_disabled() -> None:
    banner("⑤ rate_limit_per_minute = 0 ⇒ 整个关掉（不挂中间件、不检查）")

    settings.rate_limit_per_minute = 0
    fresh = importlib.reload(api)  # 限额是 import 期算的，必须重载才看得到新值
    try:
        check("limiter.enabled 为假", fresh.limiter.enabled, False)
        check("限额列表为空", list(fresh.limiter._default_limits), [])
        check("中间件栈里**没有** SlowAPIMiddleware",
              SlowAPIMiddleware in [m.cls for m in fresh.app.user_middleware], False)

        client = TestClient(fresh.app)
        codes = {client.get("/api/config").status_code for _ in range(_TIGHT + 5)}
        check("连打也不会有 429", 429 in codes, False, f"实际 {sorted(codes)}")
    finally:
        settings.rate_limit_per_minute = _TIGHT
        importlib.reload(api)  # 恢复成探针中间那个状态，免得影响后面（本文件⑤在最后）


# ================================================================ main
def main() -> int:
    print(f"真实配置 RATE_LIMIT_PER_MINUTE = {_CONFIGURED}"
          f"{'（0 = 限流关闭）' if _CONFIGURED == 0 else ''}")
    print(f"探针内临时值 = {_TIGHT}")
    if _CONFIGURED == 0:
        print("⚠ .env 里限流是关着的 —— 探针仍会验代码接线，但线上不受限。")

    probe_wiring()
    probe_trigger()
    probe_bucket_and_exempt()
    probe_sse_survives()
    probe_disabled()

    settings.rate_limit_per_minute = _CONFIGURED
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
