"""独立验证对话历史持久化层：conv_store 的往返 / 整体替换 / 级联 / 搜索 / 降级。

这一层不依赖大模型、不依赖 Qdrant/BM25，也不依赖 API 服务，可以单独跑。

三条最要紧的断言，都是「静默出错」那一类（不报错、不异常，只是数据悄悄不对）：

1. **⑩ `pg_store` 的只读防线仍然成立** —— 本次给同一个库加了**第二条可写连接**
   （`conv_store` 没有 `default_transaction_read_only=on`，因为它必须能写）。
   必须证明这没有把采购查询工具的只读保护拆掉：那条连接上 `DELETE` 仍要被拒。
   两条连接各自独立（`_run()` 只碰自己模块的 `_conn`），但这是「加写连接」唯一的风险点，
   所以由行为断言钉死，而不是靠读代码相信它。
2. **④ 大 payload 往返逐字节相同** —— 一条对话可以塞进 12 条 sources、整行 pg 原文、
   整段 reasoning。jsonb 若在某个大小上开始截断或转义，界面上的表现是「历史对话点开是坏的」，
   而那是几百 KB 之后的偶发事件，手测碰不到。
3. **⑦ `%` / `_` 被转义**（复用 `pg_store._like`）—— 不转义的话用户搜一个 `%` 会命中**全表**，
   侧栏列出所有对话。这与图谱「空 entity」、`pg_store`「空筛选」是同一类
   **看起来完全合理的错误答案**。
   ⚠ 同一节后半还钉着 `hit_message_id`（点搜索结果跳到命中那条的落点）：标题命中时**为空串**、
   正文命中时**逐字等于那条消息的 id**、以及**片段与 id 同源**（m1 / m2 都命中时取 seq 最小的
   那一条，两边必须来自同一个 LATERAL 行）。判据都是**逐字相等**，不是「非空」——
   非空只证明它拿到了某条消息，证明不了拿对了。

⚠ **①②⑨⑪ 是离线断言**（接线 / DDL 文本 / 降级 / 路由映射），必须跑在 `warmup()` **之前**：
它们恰恰是「数据库连不上时」才需要成立的性质，而 `probe_pg_store.py` 一旦连不上就
`return 1`，那几条在那里永远跑不到。本脚本因此把执行顺序排成 ①②⑨⑪ → ③④⑤⑥⑦⑧⑩⑫
（`usage_stats` 的降级分支也在 ⑨ 那批里，所以看板取数失败时的行为**离线就验得到**）。

⚠ **⑫ 是给 `eval/dashboard.py` 的**（`conv_store.usage_stats()`）：它钉的核心一件事是
「看板的数字**不是**封顶列表的 `len()`」—— 把 `_LIST_HARD_CAP` 临时压到 1，
`list_conversations()` 立刻只剩 1 条，而 `usage_stats()` 一动不动。

跑法：

    uv run python scripts/probe_conv_store.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import conv_store, pg_store  # noqa: E402
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.schemas import ConversationUpsert, FeedbackUpsert  # noqa: E402

# 所有临时对话都带这个前缀，跑完自己清干净（也顺手清掉上次崩在半路的残留）
ID_PREFIX = "probe-conv-"

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


# ---------------------------------------------------------------- 夹具
def make_message(mid: str, role: str, content: str) -> dict:
    """一条**形状完整**的前端消息。刻意带上前端各个可选字段 + 一个不存在的字段。

    `future_field` 是刻意留的：后端 `ConversationUpsert.messages` 是
    `list[dict[str, Any]]`，**不解释**消息内部结构，所以前端以后给 Message 加字段
    （像上次加 image 那样）不用改表。这条断言就是那个设计意图的看门人 ——
    哪天有人把 messages 改成严格的 pydantic 模型，这里会红。
    """
    return {
        "id": mid,
        "role": role,
        "content": content,
        "reasoning": "先看资料\n再比对条文\n最后给结论",
        "steps": [
            {
                "id": "step-1",
                "name": "search_knowledge_graph",
                "query": "查询类型：entity_search · 激光打印机",
                "status": "done",
                "graph": {"total": 107, "rows": [{"label": "某公司", "count": 12, "amount": 3456.78}]},
            }
        ],
        "sources": [{"index": 1, "question": "问", "answer": "答", "score": 0.87, "rerank_score": None}],
        "latency": {"retrieve": 12, "rerank": 0},
        "streaming": False,
        "future_field": {"nested": [1, 2, {"deep": True}], "null": None, "中文键": "值"},
    }


def upsert(title: str, messages: list[dict], created: int, updated: int) -> ConversationUpsert:
    return ConversationUpsert(
        title=title, created_at=created, updated_at=updated, messages=messages
    )


def cleanup() -> int:
    """删掉所有 `probe-conv-` 开头的对话（本次的 + 上次崩在半路留下的）。

    ⚠⚠ **绝不能图快改成 `delete_all_conversations()`**：那会把用户自己的真实历史对话
      一起删光。前缀过滤在这里不是「顺手加的一道」，它就是「只删自己那几条」这件事本身。
    """
    gone = 0
    for meta in conv_store.list_conversations(limit=500):
        if meta.id.startswith(ID_PREFIX):
            conv_store.delete_conversation(meta.id)
            gone += 1
    return gone


def _row_count(conv_id: str) -> int:
    rows = conv_store._run("SELECT count(*) AS n FROM messages WHERE conversation_id = %s", (conv_id,))
    return int(rows[0]["n"])


# ---------------------------------------------------------------- ① 接线（离线）
def probe_wiring() -> None:
    banner("① 接线 / 开关一致性（离线）")

    check(
        "config.py 声明了 postgres_* 字段且读到了 .env 的值",
        hasattr(settings, "postgres_host") and bool(settings.postgres_host.strip()),
        detail=f"postgres_host={settings.postgres_host!r}",
    )
    check(
        "ENABLED == bool(POSTGRES_HOST)",
        conv_store.ENABLED == bool(settings.postgres_host.strip()),
        detail=f"ENABLED={conv_store.ENABLED}",
    )
    for name in (
        "ENABLED",
        "warmup",
        "conv_status",
        "list_conversations",
        "get_conversation",
        "save_conversation",
        "delete_conversation",
        "delete_all_conversations",
        "search_conversations",
        "usage_stats",
    ):
        check(f"模块导出 {name}", hasattr(conv_store, name))

    # 看板（`eval/dashboard.py`）靠 `usage_stats` 取数。`msg` 形参**必须有默认值**：
    # 没有的话「看板要自己的降级文案」就变成「所有调用点都得跟着改签名」，
    # 而这里会以 `TypeError` 的形式在**运行期**才暴露。
    import inspect

    param = inspect.signature(conv_store.usage_stats).parameters.get("msg")
    check(
        "usage_stats 有 msg 形参且带默认值",
        param is not None and param.default is conv_store._UNAVAILABLE_MSG,
        detail=str(inspect.signature(conv_store.usage_stats)),
    )

    # ⚠ 这条不是形式主义：`_like` 若被抄成第二份，两边的转义字符或转义集合就可能不同，
    #   而 `search_conversations` 的 SQL 里写死了 `ESCAPE '\'` —— 与 `_like()` 的转义字符
    #   必须一致。抄一份 = 埋一个只在「用户搜了带 % 的词」时才炸的雷。
    check(
        "_like 是 pg_store 里的**同一个函数对象**（没有抄第二份）",
        conv_store._like is pg_store._like,
        detail=f"{conv_store._like!r} vs {pg_store._like!r}",
    )

    # 路由接线：五条都要在，且顺序要对
    try:
        from bidding_qa import api

        paths = [getattr(route, "path", "") for route in api.app.routes]
        for path in (
            "/api/conversations",
            "/api/conversations/search",
            "/api/conversations/{conv_id}",
        ):
            check(f"路由已注册 {path}", path in paths)

        # ⚠⚠ `/api/conversations/search` 必须**声明在** `/api/conversations/{conv_id}` 之前：
        #   FastAPI 按声明顺序匹配，反了的话搜一个词会被当成搜 `id="search"` 的对话，
        #   表现为「搜索永远没有结果」——一个完全静默的故障。
        gets = [
            getattr(route, "path", "")
            for route in api.app.routes
            if getattr(route, "methods", None) == {"GET"}
        ]
        try:
            order_ok = gets.index("/api/conversations/search") < gets.index(
                "/api/conversations/{conv_id}"
            )
        except ValueError:
            order_ok = False
        check("GET /search 声明在 GET /{conv_id} 之前", order_ok, detail=f"GET 路由序：{gets}")

        methods = set()
        for route in api.app.routes:
            if getattr(route, "path", "") == "/api/conversations/{conv_id}":
                methods |= getattr(route, "methods", set())
        check("单条路由上有 GET / PUT / DELETE", {"GET", "PUT", "DELETE"} <= methods, detail=str(methods))

        # ⚠ `DELETE /api/conversations`（1 段）与 `DELETE /api/conversations/{conv_id}`（2 段）
        #   靠**段数**区分，所以不存在上面 `search` 那种顺序问题。但「集合路由上只有
        #   GET 与 DELETE」仍要钉一下：多出个 PUT 就等于多一条能把整表覆盖掉的路径。
        coll = set()
        for route in api.app.routes:
            if getattr(route, "path", "") == "/api/conversations":
                coll |= getattr(route, "methods", set())
        check(
            "集合路由上是 GET + DELETE（没有 PUT —— 那会是一条整表覆盖路径）",
            coll == {"GET", "DELETE"},
            detail=str(coll),
        )

        fields = set(api.HealthResponse.model_fields)
        check(
            "/api/health 带 history_* 三字段",
            {"history_configured", "history_available", "history_detail"} <= fields,
        )
    except Exception as exc:  # noqa: BLE001
        check("api.py 可导入且路由接线正常", False, detail=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- ② DDL（离线）
def probe_ddl() -> None:
    banner("② DDL 文本（离线）")

    joined = "\n".join(conv_store._DDL)
    low = joined.lower()

    check("两张表都建", "create table if not exists conversations" in low and "create table if not exists messages" in low)
    check("全部是 IF NOT EXISTS（多跑无害）", all("if not exists" in s.lower() for s in conv_store._DDL))
    check(
        "外键带 ON DELETE CASCADE（删对话即删消息，不留孤儿）",
        "references conversations(id) on delete cascade" in low,
    )
    # ⚠ 用正则而不是字符串 in：DDL 里的对齐空格是给人看的，改一次缩进就会让 `in` 断言假失败
    check(
        "role / content 是两个 STORED 生成列",
        re.search(r"role\s+text\s+generated always as \(payload->>'role'\)\s+stored", low) is not None
        and re.search(r"content\s+text\s+generated always as \(payload->>'content'\)\s+stored", low)
        is not None,
    )
    # 生成列必须真的对得上 payload 里的键名 —— 拼错了不会报错，只会让**所有搜索都查不到东西**
    check("生成列取的键名与前端 Message 一致（role / content）", "payload->>'role'" in low and "payload->>'content'" in low)
    check("两个索引都在", "messages_conv_seq_idx" in joined and "conversations_updated_idx" in joined)
    check(
        "messages 主键是 (conversation_id, id)",
        "primary key (conversation_id, id)" in low,
    )
    # 生成列只能引用**已经在同一 CREATE TABLE 里声明过**的列 —— 把 payload 挪到生成列后面
    # 会让建表直接报 UndefinedColumn，而 `ensure_schema` 的失败会被 `_guard` 包成
    # ConvStoreUnavailable → 整个历史功能静默降级成「未同步」。顺序是硬约束，钉住它。
    body = conv_store._DDL[1]
    check(
        "payload 声明在生成列之前（否则建表报 UndefinedColumn）",
        body.index("payload") < body.index("payload->>'role'"),
    )


# ---------------------------------------------------------------- ⑨ 降级（离线）
def probe_degradation() -> None:
    banner("⑨ 降级：一律抛 ConvStoreUnavailable，且不泄露连接信息（离线）")

    calls = {
        "list_conversations": lambda: conv_store.list_conversations(),
        "get_conversation": lambda: conv_store.get_conversation("x"),
        "save_conversation": lambda: conv_store.save_conversation("x", upsert("t", [], 0, 0)),
        "delete_conversation": lambda: conv_store.delete_conversation("x"),
        "delete_all_conversations": lambda: conv_store.delete_all_conversations(),
        "search_conversations": lambda: conv_store.search_conversations("x"),
        "usage_stats": lambda: conv_store.usage_stats(),
    }

    # ---- 未配置 POSTGRES_HOST ----
    original_enabled = conv_store.ENABLED
    conv_store.ENABLED = False
    try:
        for name, fn in calls.items():
            try:
                fn()
                check(f"未配置时 {name} 抛异常", False, detail="竟然正常返回了")
            except conv_store.ConvStoreUnavailable as exc:
                check(f"未配置时 {name} 抛 ConvStoreUnavailable", bool(str(exc)), detail=str(exc))
            except Exception as exc:  # noqa: BLE001
                check(f"未配置时 {name} 抛 ConvStoreUnavailable", False, detail=f"抛的是 {type(exc).__name__}")
    finally:
        conv_store.ENABLED = original_enabled

    # ---- 传输层失败被包起来，且原始异常文本不外泄 ----
    #
    # ⚠⚠ 这一段必须**原样还原** `_conn` / `_failed_at` / `_last_error`：
    #   `_guard` 见到 `OperationalError` 会调 `_drop_conn(exc)`，那会置 `_failed_at`
    #   → **武装 30 秒冷却** → 后面每一节真连库的断言全部失败（且看起来像数据库挂了）。
    import psycopg

    snapshot = (conv_store._conn, conv_store._failed_at, conv_store._last_error)
    real_get_conn = conv_store._get_conn
    marker = "boom-marker-9f2c"  # 假装这是连接串里的主机名/用户名
    conv_store._get_conn = lambda: (_ for _ in ()).throw(psycopg.OperationalError(marker))
    try:
        for name, fn in calls.items():
            try:
                fn()
                check(f"{name} 把 psycopg 异常包成 ConvStoreUnavailable", False, detail="竟然正常返回了")
            except conv_store.ConvStoreUnavailable as exc:
                text = str(exc)
                check(f"{name} 抛 ConvStoreUnavailable", True, detail=text)
                check(f"{name} 的报错不含原始异常内容（连接串/主机名不外泄）", marker not in text, detail=text)
            except Exception as exc:  # noqa: BLE001
                check(f"{name} 把 psycopg 异常包成 ConvStoreUnavailable", False, detail=f"抛的是 {type(exc).__name__}: {exc}")
    finally:
        conv_store._get_conn = real_get_conn
        conv_store._conn, conv_store._failed_at, conv_store._last_error = snapshot
        check("冷却状态已还原（否则后面每节都会假失败）", conv_store._failed_at is snapshot[1])

    # conv_status 是诊断接口，**任何情况下都不能抛**（健康检查每 30 秒轮询一次）
    conv_store.ENABLED = False
    try:
        status = conv_store.conv_status()
        check(
            "未配置时 conv_status 不抛且如实报未配置",
            status == {"configured": False, "available": False, "detail": "未配置 POSTGRES_HOST"},
            detail=str(status),
        )
    except Exception as exc:  # noqa: BLE001
        check("未配置时 conv_status 不抛", False, detail=f"{type(exc).__name__}: {exc}")
    finally:
        conv_store.ENABLED = original_enabled

    # ---- `msg` 形参必须**原样**传出去 ----
    #
    # ⚠⚠ 这一条逮住的**不是**假想问题：`probe_feedback.py` ③ 就是这么发现
    #   `list_feedback` 漏传 `msg` 的 —— 用户点了 👎 会读到「本次对话将只保存在本机浏览器」，
    #   说的是一件**没发生**的事（对话好好地存着，丢的是评价）。
    #   看板（`eval/dashboard.py`）同样靠这句文案说清「谁失败了」：复用默认那句的话，
    #   跑一个只读的看板会声称「对话历史不会保存到数据库」。
    conv_store.ENABLED = False
    marker = "自定义-看板文案-7b31"
    try:
        conv_store.usage_stats(marker)
        check("usage_stats 的 msg 覆盖生效", False, detail="竟然正常返回了")
    except conv_store.ConvStoreUnavailable as exc:
        check(
            "usage_stats 的 msg 原样传出（不被默认那句盖掉）",
            str(exc) == marker,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        check("usage_stats 的 msg 覆盖生效", False, detail=f"抛的是 {type(exc).__name__}")
    finally:
        conv_store.ENABLED = original_enabled

    # ---- 清空有**自己那句**文案，不复用默认那句 ----
    #
    # ⚠⚠ 同 `probe_feedback.py` ③ 的由来，而这里更严重：默认那句说的是**保存**
    #   （「本次对话将只保存在本机浏览器」），用户点的是**删除** —— 答的不是他问的问题。
    #   而侧栏的确认卡片会把这句话**原样显示**在按钮下面。
    conv_store.ENABLED = False
    try:
        conv_store.delete_all_conversations()
        check("未配置时 delete_all_conversations 抛异常", False, detail="竟然正常返回了")
    except conv_store.ConvStoreUnavailable as exc:
        check(
            "⚠⚠ 清空失败用的是**专用**文案（不是「只保存在本机浏览器」那句）",
            str(exc) == conv_store._DELETE_ALL_UNAVAILABLE_MSG
            and str(exc) != conv_store._UNAVAILABLE_MSG,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        check("未配置时 delete_all_conversations 抛 ConvStoreUnavailable", False, detail=f"抛的是 {type(exc).__name__}")
    finally:
        conv_store.ENABLED = original_enabled


# ---------------------------------------------------------------- ⑪ 路由层（离线）
def probe_routes() -> None:
    banner("⑪ 路由层：异常 → HTTP 状态码的映射（离线，替身 store）")

    try:
        from fastapi.testclient import TestClient

        from bidding_qa import api
    except Exception as exc:  # noqa: BLE001
        check("能构造 TestClient", False, detail=f"{type(exc).__name__}: {exc}")
        return

    # ⚠ **不进 `with`**：进入上下文会跑 lifespan → `_bootstrap()` → 加载索引与向量模型
    #   （几十秒）。不进入正好还多验一条 —— 这 5 条路由**不依赖启动完成**
    #   （它们刻意不调 `_require_ready()`，否则重启后头几十秒首屏历史会消失）。
    client = TestClient(api.app)

    unavailable = conv_store.ConvStoreUnavailable("测试用的不可用文案")
    saved = {
        name: getattr(conv_store, name)
        for name in (
            "list_conversations",
            "search_conversations",
            "get_conversation",
            "save_conversation",
            "delete_conversation",
            "delete_all_conversations",
        )
    }

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise unavailable

    try:
        # ---- 五个端点全部映射成 503 + 中文 detail ----
        for name in saved:
            setattr(conv_store, name, boom)

        cases = [
            ("列表 503", lambda: client.get("/api/conversations")),
            ("搜索 503", lambda: client.get("/api/conversations/search", params={"q": "x"})),
            ("单条 503", lambda: client.get("/api/conversations/x")),
            (
                "保存 503",
                lambda: client.put(
                    "/api/conversations/x",
                    json={"title": "t", "created_at": 1, "updated_at": 1, "messages": []},
                ),
            ),
            ("删除 503", lambda: client.delete("/api/conversations/x")),
            # ⚠ 这条同时证明它**没调 `_require_ready()`**：不进 `with`（不跑 lifespan）时
            #   `state.ready` 是 False，真调了会拿到「服务正在初始化」而不是替身那句文案。
            ("清空 503", lambda: client.delete("/api/conversations")),
        ]
        for label, call in cases:
            resp = call()
            check(
                f"{label}（能力不可用 → 服务端不可用，不是 500）",
                resp.status_code == 503 and resp.json().get("detail") == "测试用的不可用文案",
                detail=f"{resp.status_code} {resp.text[:120]}",
            )

        # ---- 空 q → 400，且**在碰 store 之前**就拒掉（否则是「空查询返回全表」）----
        resp = client.get("/api/conversations/search", params={"q": "   "})
        check(
            "空 q → 400（不是 503，证明守卫在调 store 之前）",
            resp.status_code == 400 and "搜索词不能为空" in resp.json().get("detail", ""),
            detail=f"{resp.status_code} {resp.text[:120]}",
        )

        # ---- 「不存在」是 404，**不能**塌成 503（前端据此决定要不要整体降级）----
        setattr(conv_store, "get_conversation", lambda _cid: None)
        resp = client.get("/api/conversations/nope")
        check(
            "取不存在的对话 → 404（不是 503）",
            resp.status_code == 404 and "不存在" in resp.json().get("detail", ""),
            detail=f"{resp.status_code} {resp.text[:120]}",
        )
        check("404 与 503 是两个不同的码", resp.status_code != 503)

        # ---- 正常路径：snake_case 原样出网 + 时间戳真的是整数 ----
        from bidding_qa.schemas import ConversationFull, ConversationMeta

        setattr(
            conv_store,
            "list_conversations",
            lambda _limit=100: [ConversationMeta(id="a", title="甲", created_at=1, updated_at=2, message_count=3)],
        )
        resp = client.get("/api/conversations")
        row = resp.json()[0] if resp.status_code == 200 and resp.json() else {}
        check("列表 200", resp.status_code == 200, detail=resp.text[:120])
        check(
            "载荷是 snake_case（camelCase 会在前端转换，别漏到后端）",
            set(row) == {"id", "title", "created_at", "updated_at", "message_count"},
            detail=str(sorted(row)),
        )
        # ⚠ 时间戳必须是 int：前端 `Conversation.createdAt` 拿它排序，字符串会**静默**
        #   排出一堆乱序（JS 里字符串比大小按字典序），而界面上只看得出「顺序不对」。
        check(
            "created_at / updated_at 是整数（epoch 毫秒）",
            isinstance(row.get("created_at"), int) and isinstance(row.get("updated_at"), int),
            detail=f"{type(row.get('created_at')).__name__}",
        )

        setattr(
            conv_store,
            "get_conversation",
            lambda _cid: ConversationFull(
                id="a", title="甲", created_at=1, updated_at=2, message_count=1,
                messages=[{"id": "m1", "role": "user", "content": "中文"}],
            ),
        )
        resp = client.get("/api/conversations/a")
        body = resp.json() if resp.status_code == 200 else {}
        check("单条 200 且正文原样出网（中文没被转义坏）",
              resp.status_code == 200 and body.get("messages") == [{"id": "m1", "role": "user", "content": "中文"}],
              detail=resp.text[:160])

        # PUT 回执：前端只看 response.ok，但 200 必须真是 200
        setattr(conv_store, "save_conversation", lambda _cid, _data: None)
        resp = client.put(
            "/api/conversations/a",
            json={"title": "甲", "created_at": 1, "updated_at": 2, "messages": [{"id": "m1", "role": "user", "content": "x"}]},
        )
        check("PUT 200", resp.status_code == 200, detail=resp.text[:120])

        # ⚠ 请求体缺 messages 也必须能过（前端在极端情况下可能送出空对象）——
        #   严格模型会让这里 422，而 422 在 `lib/api.ts` 里同样是 throw → 静默降级
        resp = client.put("/api/conversations/a", json={"title": "甲", "created_at": 1, "updated_at": 2})
        check("PUT 缺 messages 仍 200（有默认值）", resp.status_code == 200, detail=resp.text[:120])

        # DELETE 幂等
        setattr(conv_store, "delete_conversation", lambda _cid: None)
        codes = {client.delete("/api/conversations/a").status_code for _ in range(2)}
        check("DELETE 幂等（重复删都是 200）", codes == {200}, detail=str(codes))

        # ---- 清空：200 + `deleted` 原样出网；且**不撞**单条那条路由 ----
        #   ⚠ 两条路径的段数不同（1 段 vs 2 段），FastAPI 按段数匹配，声明顺序无所谓。
        #     这里拿「各自打到各自的处理函数」当凭证 —— `deleted` 只可能来自集合那条，
        #     `id` 只可能来自单条那条。
        setattr(conv_store, "delete_all_conversations", lambda: 7)
        resp = client.delete("/api/conversations")
        check(
            "清空 200 且 deleted 原样出网",
            resp.status_code == 200 and resp.json() == {"status": "ok", "deleted": 7},
            detail=f"{resp.status_code} {resp.text[:120]}",
        )
        resp = client.delete("/api/conversations/a")
        check(
            "⚠ 清空路由与单条路由互不遮蔽（两条同时都还能打到）",
            "id" in resp.json() and "deleted" not in resp.json(),
            detail=f"{resp.status_code} {resp.text[:120]}",
        )
    finally:
        for name, fn in saved.items():
            setattr(conv_store, name, fn)


# ---------------------------------------------------------------- ③ 往返
def probe_roundtrip() -> None:
    banner("③ 往返逐字段相等（中文 / emoji / 换行 / 引号 / 反斜杠）")

    conv_id = f"{ID_PREFIX}roundtrip"
    conv_store.delete_conversation(conv_id)

    tricky = "引号\"双\"与'单'，反斜杠\\，换行\n第二行，制表\t，emoji 🎉，百分号%，下划线_，中文标点——…"
    data = upsert(
        "往返测试",
        [
            make_message("m1", "user", tricky),
            make_message("m2", "assistant", "答：" + tricky),
        ],
        created=1758336000123,
        updated=1758336999456,
    )
    conv_store.save_conversation(conv_id, data)

    full = conv_store.get_conversation(conv_id)
    check("get_conversation 命中", full is not None)
    if full is None:
        return
    check("messages 逐字段相等（含未声明的 future_field）", full.messages == data.messages,
          detail=_diff_hint(full.messages, data.messages))
    check("title 相等", full.title == data.title, detail=repr(full.title))
    check("message_count 对得上", full.message_count == len(data.messages),
          detail=f"{full.message_count} vs {len(data.messages)}")

    # 再存一次同样的内容：整体替换必须幂等（前端每次轮末都无条件 PUT）
    conv_store.save_conversation(conv_id, data)
    again = conv_store.get_conversation(conv_id)
    check("重复 PUT 幂等（消息不翻倍）", again is not None and again.messages == data.messages)
    check("库里行数没有翻倍", _row_count(conv_id) == 2, detail=f"{_row_count(conv_id)} 行")


def _diff_hint(got: object, want: object) -> str:
    try:
        return f"got={json.dumps(got, ensure_ascii=False)[:200]}… want={json.dumps(want, ensure_ascii=False)[:200]}…"
    except Exception:  # noqa: BLE001
        return f"{got!r} vs {want!r}"


# ---------------------------------------------------------------- ④ 大 payload
def probe_large() -> None:
    banner("④ 大 payload 往返（600KB reasoning + 12 条 sources，逐字节相同）")

    conv_id = f"{ID_PREFIX}large"
    conv_store.delete_conversation(conv_id)

    big_reasoning = ("深度思考分片。" * 40) * 2000  # ≈ 600KB
    big_answer = "依据《招标投标法》第四十六条。\n" * 3000
    sources = [
        {
            "index": i,
            "doc_id": f"doc-{i}",
            "question": f"问题 {i}？",
            "answer": "答" * 2000,
            "snippet": "片段" * 200,
            "highlights": [f"关键词{i}"],
            "score": 0.9 - i / 100,
            "rrf_score": 0.5,
            "vector_score": 0.8,
            "bm25_score": 21.5,
            "rerank_score": 0.73,
            "rank_vector": i,
            "rank_bm25": i,
            "rank_fused": i,
            "matched_by": ["vector", "bm25"],
            "url": None,
            "origin": "kb",
            "published_date": "",
        }
        for i in range(1, 13)
    ]
    messages = [
        make_message("m1", "user", "一个很长的问题？" * 100),
        {
            **make_message("m2", "assistant", big_answer),
            "reasoning": big_reasoning,
            "sources": sources,
        },
    ]
    data = upsert("大 payload", messages, 1758336000123, 1758336999456)
    payload_bytes = len(json.dumps(data.messages, ensure_ascii=False).encode("utf-8"))

    conv_store.save_conversation(conv_id, data)
    full = conv_store.get_conversation(conv_id)

    ok = full is not None and full.messages == data.messages
    check(f"{payload_bytes / 1024:.0f}KB 往返逐字段相同", ok, detail="" if ok else _diff_hint(full.messages if full else None, data.messages))
    if full is not None and len(full.messages) == 2:
        check("reasoning 逐字节相同", full.messages[1].get("reasoning") == big_reasoning)
        check("12 条 sources 逐字节相同", full.messages[1].get("sources") == sources)
    # 字符数逐条对账（比「逐字段相等」弱，但能定位到**是哪条**被吃掉了，出问题时更好查）
    want_chars = sum(len(m.get("content", "")) for m in messages)
    got_chars = sum(len(m.get("content", "")) for m in (full.messages if full else []))
    check(
        f"中文一个字符都没少（{want_chars} 字）",
        got_chars == want_chars,
        detail=f"读回 {got_chars} 字",
    )


# ---------------------------------------------------------------- ⑤ 整体替换
def probe_replace() -> None:
    banner("⑤ 整体替换语义 / 重复 id 兜底")

    conv_id = f"{ID_PREFIX}replace"
    conv_store.delete_conversation(conv_id)

    three = [make_message(f"m{i}", "user", f"第 {i} 条") for i in range(1, 4)]
    conv_store.save_conversation(conv_id, upsert("三条", three, 1, 1))
    check("先存三条 → 读到三条", (conv_store.get_conversation(conv_id) or _EMPTY).message_count == 3)
    check("库里三行", _row_count(conv_id) == 3)

    # regenerate 截断的场景：前端直接送一个更短的数组
    conv_store.save_conversation(conv_id, upsert("一条", three[:1], 1, 2))
    full = conv_store.get_conversation(conv_id)
    check("再存一条 → 读到一条（旧的被删掉，不是追加）", full is not None and full.message_count == 1,
          detail=f"{full.message_count if full else None} 条")
    check("库里只剩一行（DELETE 真的执行了）", _row_count(conv_id) == 1)
    check("留下的是第 1 条", full is not None and full.messages[0]["content"] == "第 1 条")

    # ⚠ 重复 id：主键是 (conversation_id, id)，撞了会让 executemany 抛错 →
    #   **整条对话从此存不进去**，而界面上只是「历史不保存」。`_message_id` 必须兜住。
    dup_id = f"{ID_PREFIX}dup"
    conv_store.delete_conversation(dup_id)
    dupes = [make_message("same", "user", "甲"), make_message("same", "assistant", "乙")]
    try:
        conv_store.save_conversation(dup_id, upsert("重复 id", dupes, 1, 1))
        check("重复 id 的对话仍然存得进去（不撞主键）", True)
        check("两条都在（换了自造 id，没有丢一条）", _row_count(dup_id) == 2, detail=f"{_row_count(dup_id)} 行")
    except Exception as exc:  # noqa: BLE001
        check("重复 id 的对话仍然存得进去（不撞主键）", False, detail=f"{type(exc).__name__}: {exc}")

    # 缺 id 的也要能存
    noid = f"{ID_PREFIX}noid"
    conv_store.delete_conversation(noid)
    try:
        conv_store.save_conversation(noid, upsert("缺 id", [{"role": "user", "content": "没有 id"}], 1, 1))
        check("缺 id 的消息存得进去", _row_count(noid) == 1)
    except Exception as exc:  # noqa: BLE001
        check("缺 id 的消息存得进去", False, detail=f"{type(exc).__name__}: {exc}")

    # 空 messages：服务端必须接受（删光所有消息也是一次合法的 PUT）
    empty = f"{ID_PREFIX}empty"
    conv_store.save_conversation(empty, upsert("空对话", [], 1, 1))
    full_empty = conv_store.get_conversation(empty)
    check("空 messages 的 PUT 存得下（读回 0 条）", full_empty is not None and full_empty.message_count == 0)
    check("空对话仍在列表里（不是被删了）", any(m.id == empty for m in conv_store.list_conversations()))


class _Empty:
    message_count = -1


_EMPTY = _Empty()


# ---------------------------------------------------------------- ⑥ 级联
def probe_cascade() -> None:
    banner("⑥ 级联删除 / 幂等删除")

    conv_id = f"{ID_PREFIX}cascade"
    conv_store.delete_conversation(conv_id)
    conv_store.save_conversation(conv_id, upsert("待删", [make_message("m1", "user", "甲")], 1, 1))
    check("删除前库里有一行", _row_count(conv_id) == 1)

    conv_store.delete_conversation(conv_id)
    check("get_conversation 返回 None（不是抛异常）", conv_store.get_conversation(conv_id) is None)
    check("messages 里没有残留（ON DELETE CASCADE 生效）", _row_count(conv_id) == 0)
    check("列表里也没有了", not any(m.id == conv_id for m in conv_store.list_conversations()))

    # 幂等：前端删除是「本地立刻消失 + 补一发 DELETE」，重复发是常态
    try:
        conv_store.delete_conversation(conv_id)
        conv_store.delete_conversation("probe-conv-从未存在过")
        check("删不存在的 id 是空操作（不报错）", True)
    except Exception as exc:  # noqa: BLE001
        check("删不存在的 id 是空操作（不报错）", False, detail=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- ⑦ 搜索
def probe_search() -> None:
    banner("⑦ 搜索：标题命中 / 正文命中 / 命中定位（hit_message_id）/ 通配符转义")

    # 一个干净的地基：清掉前缀残留，再建 3 条对照
    cleanup()

    title_hit = f"{ID_PREFIX}title"
    body_hit = f"{ID_PREFIX}body"
    control = f"{ID_PREFIX}control"
    # 长到足够被 `_snippet` 截短（关键词在正中，两端都会被切掉）
    long_answer = "依据《招标投标法》，评标委员会由五人以上单数组成，其中技术经济专家不少于三分之二。" * 3
    conv_store.save_conversation(
        title_hit, upsert("履约保证金怎么算", [make_message("m1", "user", "随便问问")], 1, 30)
    )
    conv_store.save_conversation(
        body_hit,
        upsert(
            "普通标题",
            [
                make_message("m1", "user", "评标委员会人数有什么要求？"),
                make_message("m2", "assistant", long_answer),
            ],
            1,
            20,
        ),
    )
    # ⚠ 对照组必须**一个通配符都不含** —— 否则「搜 % 不该命中全表」这条断言会因为它
    #   自己就含 % 而假通过（写这个探针时踩过一次）。
    conv_store.save_conversation(
        control,
        upsert("对照组", [make_message("m1", "user", "这条消息里没有任何通配符")], 1, 10),
    )

    hits = conv_store.search_conversations("履约保证金")
    check("标题命中", any(h.id == title_hit for h in hits), detail=str([(h.id, h.snippet) for h in hits]))
    check("标题命中时 snippet 为空串（片段只对正文有意义）",
          all(h.snippet == "" for h in hits if h.id == title_hit))
    # ⚠⚠ 标题命中时 `hit_message_id` **必须是空串**。前端拿它 `getElementById` 跳转，
    #   退化成「随便给一条消息的 id」的话，会把用户送到一个与关键词毫无关系的位置 ——
    #   而且看起来一切正常（真跳了、真有高亮，只是标错了地方）。
    check("标题命中时 hit_message_id 为空串（没有「哪一条消息」可言）",
          all(h.hit_message_id == "" for h in hits if h.id == title_hit),
          detail=str([(h.id, h.hit_message_id) for h in hits]))

    hits = conv_store.search_conversations("技术经济专家")
    body_rec = next((h for h in hits if h.id == body_hit), None)
    check("正文命中（搜的是消息内容，不只是标题）", body_rec is not None)
    if body_rec is not None:
        check("片段含关键词", "技术经济专家" in body_rec.snippet, detail=body_rec.snippet)
        check("片段来自 assistant", body_rec.snippet_role == "assistant", detail=body_rec.snippet_role)
        # 命中的是第 2 条（m2 = assistant）—— 这是「跳到命中的那一条」的全部依据。
        # ⚠ 判据是**逐字等于消息 id**，不是「非空」：非空只证明它拿到了某条消息。
        check("hit_message_id 指向命中的那条消息（m2）",
              body_rec.hit_message_id == "m2", detail=repr(body_rec.hit_message_id))
        # 关键词在正文正中 → 两端都该被切掉。⚠ 只能断尾部那个省略号：`_snippet` 在
        # start == 0 时**不加**前导 `…`（命中就在开头，加个省略号反而是错的）。
        check(
            "片段是截出来的一段、不是整篇（比原文短且尾部有省略号）",
            len(body_rec.snippet) < len(long_answer) and body_rec.snippet.endswith("…"),
            detail=f"片段 {len(body_rec.snippet)} 字 vs 原文 {len(long_answer)} 字",
        )

    hits = conv_store.search_conversations("评标委员会人数")
    check("提问侧也能命中，snippet_role=user",
          any(h.snippet_role == "user" for h in hits if h.id == body_hit))
    check("提问侧命中时 hit_message_id 指向 m1（不是笼统地「这条对话有」）",
          any(h.hit_message_id == "m1" for h in hits if h.id == body_hit),
          detail=str([(h.id, h.hit_message_id) for h in hits if h.id == body_hit]))

    # ⚠ «评标委员会» 在 m1（提问）和 m2（回答）**都**出现 —— LATERAL 取的是 `ORDER BY m.seq`
    #   的**第一条**。片段与 `hit_message_id` 必须来自**同一条**（都取 h.* 那两个列），
    #   否则会出现「片段是提问的、跳到的是回答」这种自相矛盾的定位。
    hits = conv_store.search_conversations("评标委员会")
    both = next((h for h in hits if h.id == body_hit), None)
    check("两处都命中时取 seq 最小的那条，且片段与 hit_message_id 同源",
          both is not None and both.hit_message_id == "m1" and both.snippet_role == "user",
          detail=f"hit_message_id={both.hit_message_id!r} role={both.snippet_role!r}" if both else "没命中")

    # ⚠⚠ 转义：不转义的话 `%%%` 命中全表（8751 行那套教训在这里是「列出全部历史对话」）
    hits = conv_store.search_conversations("%")
    check("搜 % 只命中真含 % 的（转义生效，不是全表）",
          all(h.id != control for h in hits) and not any(h.id == title_hit for h in hits),
          detail=f"命中 {len(hits)} 条：{[h.id for h in hits]}")
    check("搜 % 不返回全部对话", len(hits) < 3, detail=f"{len(hits)} 条")

    hits = conv_store.search_conversations("_")
    check("搜 _ 也不返回全部对话（下划线同样被转义）", len(hits) < 3, detail=f"{len(hits)} 条")

    hits = conv_store.search_conversations("绝对不存在的词组zzz")
    check("查不到就是空列表（不是全表、不是报错）", hits == [], detail=str(hits))

    # 大小写不敏感（ILIKE）—— 中文无影响，网页/英文标题有
    en = f"{ID_PREFIX}en"
    conv_store.save_conversation(en, upsert("Tavily API Key 配置", [make_message("m1", "user", "x")], 1, 5))
    check("英文大小写不敏感（ILIKE）", any(h.id == en for h in conv_store.search_conversations("tavily")))

    # 排序：按 updated_at DESC
    hits = conv_store.search_conversations("probe-conv")
    stamps = [h.updated_at for h in hits]
    check("搜索结果按 updated_at 倒序", stamps == sorted(stamps, reverse=True), detail=str(stamps))

    meta = conv_store.list_conversations(limit=5)
    check("列表按 updated_at 倒序", [m.updated_at for m in meta] == sorted([m.updated_at for m in meta], reverse=True))
    check("列表只回元数据（不带 messages 字段）", not hasattr(meta[0], "messages"))
    check("列表 limit 生效", len(conv_store.list_conversations(limit=2)) == 2)

    conv_store.delete_conversation(en)


# ---------------------------------------------------------------- ⑧ 时间戳
def probe_timestamps() -> None:
    banner("⑧ 时间戳往返逐毫秒相等")

    conv_id = f"{ID_PREFIX}ts"
    conv_store.delete_conversation(conv_id)

    # 刻意选一个毫秒部分非零的值：用 int() 截断的实现会在这里差 1ms
    created_ms = 1758336000123
    updated_ms = 1758336999456
    conv_store.save_conversation(conv_id, upsert("时间戳", [], created_ms, updated_ms))

    full = conv_store.get_conversation(conv_id)
    check("created_at 逐毫秒相等", full is not None and full.created_at == created_ms,
          detail=f"{full.created_at if full else None} vs {created_ms}")
    check("updated_at 逐毫秒相等", full is not None and full.updated_at == updated_ms,
          detail=f"{full.updated_at if full else None} vs {updated_ms}")

    meta = next((m for m in conv_store.list_conversations() if m.id == conv_id), None)
    check("列表里的时间戳与单条一致", meta is not None and meta.created_at == created_ms and meta.updated_at == updated_ms)

    hits = conv_store.search_conversations("时间戳")
    hit = next((h for h in hits if h.id == conv_id), None)
    check("搜索结果里的时间戳与单条一致", hit is not None and hit.updated_at == updated_ms)

    # 时区：写入时按 UTC 解释，读回来必须还是同一时刻（差 8 小时 = 时区处理反了）
    check("没有整小时的时区偏移", full is not None and abs(full.updated_at - updated_ms) < 1000)


# ---------------------------------------------------------------- ⑩ 只读防线
def probe_pg_readonly() -> None:
    banner("⑩ pg_store 的只读防线仍然成立（本次加了第二条可写连接）")

    # 先把采购工具那条连接真的建起来，否则「拒绝 DELETE」可能只是因为连不上
    pg_store.warmup()
    status = pg_store.pg_status()
    check("采购库连接可用（预热成功）", status["available"], detail=str(status["detail"]))

    # ⚠⚠ 断言的是**行为**，不是属性：`conn.read_only` 在 autocommit 下**不生效时也返回 True**
    #   （psycopg 3.3.5 实测，见 pg_store._connect 的注释）。写成 `assert conn.read_only`
    #   只制造安全感 —— 防线失效时它照样绿。
    try:
        pg_store._run("DELETE FROM bid_records WHERE false")
        check("只读连接拒绝 DELETE", False, detail="写操作竟然被执行了 —— 只读防线已破！")
    except Exception as exc:  # noqa: BLE001
        check("只读连接拒绝 DELETE", True, detail=f"{type(exc).__name__}: {str(exc)[:90]}")

    # 第二道：连接参数里必须还带着只读选项（万一哪天有人「统一」两条连接）
    source = Path(__file__).resolve().parents[1] / "src" / "bidding_qa" / "pg_store.py"
    text = source.read_text(encoding="utf-8")
    check(
        "pg_store._connect 仍带 default_transaction_read_only=on",
        "default_transaction_read_only=on" in text,
    )
    # 反面：conv_store 那条连接**必须是可写的**。⚠ 这里断言的是**服务端的实际取值**，
    # 不是源码里有没有那个字符串 —— conv_store.py 的注释里就写着 `default_transaction_read_only`
    # （解释为什么这里**不**用它），文本检查会被自己的注释绊倒（写这个探针时踩过一次）。
    rows = conv_store._run("SHOW default_transaction_read_only")
    value = str(next(iter(rows[0].values()))).strip().lower()
    check("conv_store 的连接可写（服务端 default_transaction_read_only=off）", value == "off", detail=value)

    # 采购工具本身没有被这次改动影响：跨栈对账那个常数照旧
    result = pg_store.query({"contains": "激光打印机"})
    check(
        "采购工具跨栈对账常数照旧（107 笔 / 973172 元）",
        result.ok and result.total == 107 and abs(float(result.extra.get("中标金额合计", 0)) - 973172.0) < 0.01,
        detail=f"total={result.total} extra={result.extra}",
    )


# ---------------------------------------------------------------- ⑫ 看板计数
def probe_usage_stats() -> None:
    banner("⑫ usage_stats / 看板计数：真 count(*) 而不是封顶列表的 len()")

    CONV_A = f"{ID_PREFIX}dash-a"
    CONV_B = f"{ID_PREFIX}dash-b"
    FB_UP = f"{CONV_A}#m1"  # 评价的 key 是 (conversation_id, message_id)
    FB_DOWN = f"{CONV_B}#m1"

    def live(sql: str, params: tuple = ()) -> int:
        """独立查询，**不经过 `usage_stats`** —— 交叉对账的凭证就是这个「独立」。"""
        rows = conv_store._run(sql, params)
        return int(next(iter(rows[0].values())))

    def clear() -> None:
        """⚠ 反馈**没有外键**，`delete_conversation` 带不走它，必须单独删。"""
        for conv_id, msg_id in ((CONV_A, FB_UP), (CONV_B, FB_DOWN)):
            try:
                conv_store.delete_feedback(conv_id, msg_id)
                conv_store.delete_conversation(conv_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ 清理 {conv_id} 失败: {type(exc).__name__}: {exc}")

    clear()  # 上次崩在半路的残留

    try:
        # ---- 1) 形状：五个 int 键 ----
        stats = conv_store.usage_stats()
        check(
            "键集合 == _STAT_KEYS 且全是 int（看板直接拿去格式化）",
            set(stats) == set(conv_store._STAT_KEYS)
            and all(isinstance(v, int) and not isinstance(v, bool) for v in stats.values()),
            detail=str(stats),
        )

        # ---- 2) 与独立查询交叉对账（照 probe_pg_store.probe_group() 的成例）----
        for key, sql in (
            ("conversations", "SELECT count(*) FROM conversations"),
            ("messages", "SELECT count(*) FROM messages"),
            ("feedback", "SELECT count(*) FROM feedback"),
            ("up", "SELECT count(*) FROM feedback WHERE rating = 1"),
            ("down", "SELECT count(*) FROM feedback WHERE rating = -1"),
        ):
            want = live(sql)
            check(f"{key} 与独立 count(*) 对得上", stats[key] == want, detail=f"{stats[key]} vs {want}")

        # ---- 3) 恒等式：rating 只有 ±1 两种，所以两个拇指加起来不会超过总数 ----
        #   ⚠ 写成 `==` 会红：库里若混进过 rating=0 的行（旧版本、手工 SQL），
        #     `feedback` 会比两个拇指之和大，那是**如实反映**，不是 bug。
        check(
            "up + down <= feedback（未识别的 rating 不混进两个拇指）",
            stats["up"] + stats["down"] <= stats["feedback"],
            detail=f"{stats['up']} + {stats['down']} <= {stats['feedback']}",
        )

        # ---- 4) 增量：建两条对话 + 各一条评价，看五个数是不是**精确**跟着走 ----
        #   ⚠ 断言的是**增量**不是绝对值 —— 这是用户的真实库，里面可能有他自己的对话。
        before = conv_store.usage_stats()
        conv_store.save_conversation(CONV_A, upsert("看板 A", [make_message(f"{CONV_A}#m{i}", "user", f"a{i}") for i in range(3)], 1000, 2000))
        conv_store.save_conversation(CONV_B, upsert("看板 B", [make_message(f"{CONV_B}#m{i}", "user", f"b{i}") for i in range(2)], 1000, 2000))
        conv_store.save_feedback(CONV_A, FB_UP, FeedbackUpsert(rating=1, question="q", answer="a"))
        conv_store.save_feedback(CONV_B, FB_DOWN, FeedbackUpsert(rating=-1, question="q", answer="a"))
        after = conv_store.usage_stats()
        delta = {k: after[k] - before[k] for k in conv_store._STAT_KEYS}
        check(
            "新增 2 条对话（3 + 2 条消息）后 conversations +2",
            delta["conversations"] == 2,
            detail=str(delta),
        )
        check("messages +5（逐条计数，不是「有多少条对话」）", delta["messages"] == 5, detail=str(delta))
        check(
            "赞/踩各一条后 feedback +2 / up +1 / down +1",
            (delta["feedback"], delta["up"], delta["down"]) == (2, 1, 1),
            detail=str(delta),
        )

        # ---- 5) ⚠⚠ 本节的核心：它**不是**封顶列表的 `len()` ----
        #
        # 看板要的是「攒了多少对话」，而 `list_conversations()` 封顶
        # `_LIST_HARD_CAP`（500）。拿 `len()` 顶上，数据长起来那天会显示一个
        # **看起来完全合理的错误数字**（界面写 500、实际 1200），不报错、不截断提示。
        #
        # 库里现在够不到 500，所以把上限**临时压到 1** —— 钳制立刻生效，
        # 此时 `len()` 与被夹紧的来源分道扬镳。两条断言各钉一半：先证明钳制真的在动，
        # 再证明 `usage_stats()` 一动不动（否则这条测的是「钳制没生效」，不是「没走列表」）。
        cap_conv, cap_fb = conv_store._LIST_HARD_CAP, conv_store._FEEDBACK_HARD_CAP
        try:
            conv_store._LIST_HARD_CAP = 1
            conv_store._FEEDBACK_HARD_CAP = 1
            capped_conv = len(conv_store.list_conversations(limit=500))
            capped_fb = len(conv_store.list_feedback(limit=500))
            now = conv_store.usage_stats()
            check(
                "把 _LIST_HARD_CAP 压到 1 后 list_conversations() 确实只剩 1 条（钳制生效）",
                capped_conv == 1,
                detail=f"len={capped_conv}",
            )
            check(
                "⚠⚠ 同一时刻 usage_stats().conversations 不受夹紧影响（它不是 len()）",
                now["conversations"] > capped_conv
                and now["conversations"] == live("SELECT count(*) FROM conversations"),
                detail=f"usage_stats={now['conversations']} len={capped_conv}",
            )
            check(
                "⚠⚠ feedback 同理（up / down 也不是 len(list_feedback(...))）",
                capped_fb == 1
                and now["up"] == live("SELECT count(*) FROM feedback WHERE rating = 1")
                and now["down"] == live("SELECT count(*) FROM feedback WHERE rating = -1"),
                detail=f"usage_stats up/down={now['up']}/{now['down']} len={capped_fb}",
            )
        finally:
            # ⚠⚠ **必须还原**，且必须在任何清理动作之前：`main()` 收尾的 `cleanup()`
            #   走的正是 `list_conversations(limit=500)`，上限留在 1 的话它只会删掉一条，
            #   其余 `probe-conv-*` 全部残留 —— 而且是**静默**的（打印「删掉 1 条」）。
            conv_store._LIST_HARD_CAP = cap_conv
            conv_store._FEEDBACK_HARD_CAP = cap_fb

    finally:
        clear()


# ---------------------------------------------------------------- ⑬ 清空全部
class _RollbackSignal(Exception):
    """哨兵：让 `with conn.transaction()` 走 **ROLLBACK** 分支。

    ⚠⚠ 这不是错误，是这一节存在的**全部理由**：`delete_all_conversations()` 删的是
      **整张表**（包括用户自己那堆真实对话），所以只能在**必定回滚**的事务里验它。
      没有这个哨兵，`with` 正常退出会 COMMIT —— 那就是**真删了**。
    """


class _AbortCheck(Exception):
    """前置安全断言失败。⚠ 必须在**动任何一行之前**炸出去。

    ⚠⚠ 用 `raise` 而不是 `check()`：`check()` 只记一笔失败然后**继续往下执行**，
      而这里继续执行 = 在真实的库上做实验。全仓库只有这一处需要这种「硬炸」语义。
    """


def probe_delete_all() -> None:
    banner("⑬ delete_all_conversations：整库清空（⚠ 全程跑在**回滚**的事务里）")

    # psycopg 3.3.5 已锁，枚举名稳定。⚠ 别改成 `str(status)` 比较 —— 那个字符串
    # 随 psycopg 版本变（`INTRANS` / `TransactionStatus.INTRANS` / …），会**静默**变成
    # 一条永远为假的断言，而失败原因看起来像「事务没生效」。
    from psycopg.pq import TransactionStatus

    CONV_A = f"{ID_PREFIX}delall-a"
    CONV_B = f"{ID_PREFIX}delall-b"
    FB_KEY = "m1"  # 反馈的 key 是 (conversation_id, message_id)

    def live(sql: str, params: tuple = ()) -> int:
        """独立查询，**不经过被测函数** —— 交叉对账的凭证就是这个「独立」。"""
        rows = conv_store._run(sql, params)
        return int(next(iter(rows[0].values())))

    # ⚠⚠ 基线一律在**事务外**取：事务里读到的会被自己刚才的删除污染。
    conv_store.delete_conversation(CONV_A)  # 上次崩在半路的残留
    conv_store.delete_conversation(CONV_B)
    try:
        conv_store.delete_feedback(CONV_A, FB_KEY)
    except Exception:  # noqa: BLE001
        pass  # 反馈**没有外键**，`delete_conversation` 带不走它，得单独删

    conv_store.save_conversation(
        CONV_A, upsert("清空 A", [make_message(f"{CONV_A}#m0", "user", "a0")], 1000, 2000)
    )
    conv_store.save_conversation(
        CONV_B, upsert("清空 B", [make_message(f"{CONV_B}#m0", "user", "b0")], 1000, 2000)
    )
    conv_store.save_feedback(CONV_A, FB_KEY, FeedbackUpsert(rating=1, question="q", answer="a"))

    ids_before = sorted(
        r["id"] for r in conv_store._run("SELECT id FROM conversations ORDER BY id")
    )
    conv_before = live("SELECT count(*) FROM conversations")
    msg_before = live("SELECT count(*) FROM messages")
    fb_before = live("SELECT count(*) FROM feedback")

    try:
        # ⚠⚠ 本节唯一真正危险的地方。**空库上 skip 掉不解决问题** —— 这台机器库里有
        #   用户的真实对话，我们要能在这份真实数据上验它。
        conn = conv_store._get_conn()
        try:
            with conn.transaction():
                # ---- 前置安全断言：不成立就**硬炸**，一行都不许删 ----
                # ① 真在显式事务里（否则下面的删除不可回滚，直接就是毁灭性的）
                # ② 被测函数用的就是我们开事务的这条连接 —— `_get_conn()` 是模块级单例，
                #    它若被换过（缓存被丢、改造过），事务框住的就**不是**那次删除。
                ts = conn.info.transaction_status
                if ts != TransactionStatus.INTRANS or conv_store._get_conn() is not conn:
                    raise _AbortCheck(
                        f"事务未生效（transaction_status={ts}）或连接不是同一条，拒绝执行"
                    )
                check("✓ 前置：已进入显式事务，且与被测函数是同一条连接（可回滚）", True)

                deleted = conv_store.delete_all_conversations()

                check(
                    "返回 int（不是 bool / None / Decimal）",
                    isinstance(deleted, int) and not isinstance(deleted, bool),
                    detail=f"{type(deleted).__name__} {deleted!r}",
                )
                # ⚠⚠ 本节最值钱的一条，一次钉三件事：
                #   ① 真删光了（不是只删了 `list_conversations` 看得到的那批）
                #   ② 没有 WHERE 子集 —— 将来有人加 `user_id` 过滤时会**当场变红**
                #   ③ 条数不是猜的
                check(
                    "⚠⚠ 删除条数 == 删除前整表 count(*)（钉「没有 WHERE 子集」）",
                    deleted == conv_before,
                    detail=f"deleted={deleted} before={conv_before}",
                )
                check(
                    "conversations 已清空",
                    live("SELECT count(*) FROM conversations") == 0,
                    detail=str(live("SELECT count(*) FROM conversations")),
                )
                # 级联是 DDL 上的外键在做（`ON DELETE CASCADE`），不是 `_run` 手动删的
                check(
                    "messages 随外键级联一起走（不是只删了表头留下孤儿）",
                    live("SELECT count(*) FROM messages") == 0,
                    detail=str(live("SELECT count(*) FROM messages")),
                )
                # ⚠⚠ 决策：反馈**留着**给分析取数用（与单条删除一致）。
                #    与 `probe_feedback.py` ⑦⑧ 是一对 —— 那边钉单删，这边钉清空。
                check(
                    "⚠⚠ feedback 一行没动（不是「删对话顺手清反馈」）",
                    live("SELECT count(*) FROM feedback") == fb_before
                    and live(
                        "SELECT count(*) FROM feedback WHERE conversation_id = %s AND message_id = %s",
                        (CONV_A, FB_KEY),
                    )
                    == 1,
                    detail=f"feedback={live('SELECT count(*) FROM feedback')} 期望 {fb_before}",
                )
                # `eval/dashboard.py` 读的就是这个函数 —— 删完它的数字必须跟着归零
                check(
                    "看板计数同步归零（读的就是 usage_stats）",
                    conv_store.usage_stats()["conversations"] == 0,
                    detail=str(conv_store.usage_stats()),
                )
                # 幂等：客户端超时后重发是安全的（与单条删除的约定一致）
                check(
                    "对空库再删一次返回 0",
                    conv_store.delete_all_conversations() == 0,
                )

                raise _RollbackSignal  # ⚠⚠ 必须是异常：正常退出会 COMMIT = 真删
        except _RollbackSignal:
            pass  # 预期路径：事务被强制回滚，下面开始验「库还原了没有」
        except _AbortCheck as exc:
            # 安全闸响了 = 事务没框住那次删除 ⇒ **可能已经真删了**。记一笔失败并
            # 立刻跳到验证段（不 raise 出去 —— 那会跳过下面「还原了没有」的取证，
            # 而那份取证正是出事后最需要的东西）。
            check("⚠⚠ 前置安全断言（事务 / 同一条连接）", False, detail=str(exc))

        # ---- 回滚生效：库必须**逐条**复原 ----
        ids_after = sorted(
            r["id"] for r in conv_store._run("SELECT id FROM conversations ORDER BY id")
        )
        check(
            "⚠⚠ 回滚后对话**逐条**都在（比的是 id 集合，不是「数量对得上」）",
            ids_after == ids_before,
            detail=f"少了 {sorted(set(ids_before) - set(ids_after))} / 多了 {sorted(set(ids_after) - set(ids_before))}",
        )
        check(
            "回滚后 conversations / messages 计数与删除前逐字相等",
            live("SELECT count(*) FROM conversations") == conv_before
            and live("SELECT count(*) FROM messages") == msg_before,
            detail=f"conv={live('SELECT count(*) FROM conversations')}/{conv_before} "
            f"msg={live('SELECT count(*) FROM messages')}/{msg_before}",
        )
        check(
            "回滚后 feedback 与看板计数都回到原值",
            live("SELECT count(*) FROM feedback") == fb_before
            and conv_store.usage_stats()["conversations"] == conv_before,
            detail=str(conv_store.usage_stats()),
        )
        # ⚠ 只数表头不足以证明回滚干净 —— 表头回来了、消息没回来也是一样的 count。
        #   取一次正文，证明**级联删除被回滚到了消息层**。
        full = conv_store.get_conversation(CONV_A)
        check(
            "回滚后 probe 自己的正文还能完整取到（不是只剩表头）",
            full is not None and len(full.messages) == 1,
            detail=f"messages={len(full.messages) if full else 'None'}",
        )
    finally:
        # ⚠ 清理必须**两头都清**：反馈没有外键，`delete_conversation` 带不走它。
        for conv_id in (CONV_A, CONV_B):
            try:
                conv_store.delete_feedback(conv_id, FB_KEY)
                conv_store.delete_conversation(conv_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ 清理 {conv_id} 失败: {type(exc).__name__}: {exc}")


def main() -> int:
    print(
        f"PostgreSQL: {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}"
    )

    # ---------- 离线几条先跑：它们恰恰是「连不上时」才需要成立的性质 ----------
    probe_wiring()
    probe_ddl()
    probe_degradation()
    probe_routes()

    if not settings.postgres_host.strip():
        banner(f"结果：{_pass} 通过 / {_fail} 失败（未配置 POSTGRES_HOST，DB 相关各节跳过）")
        return 1

    conv_store.warmup()
    status = conv_store.conv_status()
    if status["available"]:
        print(f"\n预热: 对话历史库连接正常（{status['detail']}）")
    else:
        banner(f"结果：{_pass} 通过 / {_fail} 失败（预热失败: {status['detail']}）")
        return 1

    try:
        probe_roundtrip()
        probe_large()
        probe_replace()
        probe_cascade()
        probe_search()
        probe_timestamps()
        probe_pg_readonly()
        probe_usage_stats()
        probe_delete_all()
    finally:
        try:
            gone = cleanup()
            print(f"\n清理: 删掉 {gone} 条 {ID_PREFIX}* 测试对话")
        except Exception as exc:  # noqa: BLE001
            print(f"\n⚠ 清理失败（残留 {ID_PREFIX}* 对话）: {type(exc).__name__}: {exc}")

    banner(f"结果：{_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
