"""独立验证回答反馈层：`feedback` 表的往返 / upsert / 降级 / 路由 / **无外键**。

这一层不依赖大模型、不依赖 Qdrant/BM25，也不依赖 API 服务，可以单独跑。

**最值钱的是 ⑦⑧ 两条**，它们钉的是整个表设计的唯一关键决定：

  `feedback` 表**一处外键都没有**。

直觉写法 `message_id text REFERENCES messages(id) ON DELETE CASCADE` 会**静默清空**
所有 👍/👎 —— 因为 `conv_store.save_conversation()` 每次保存都
`DELETE FROM messages WHERE conversation_id = %s` 再整批重插，而前端**一轮回答就 PUT 一次**。
不报错、界面上完全看不出来，只有几周后翻数据库才会发现反馈少了大半。

所以这两条断言是**行为断言**，不是文本检查：

  ⑦ **重新 save_conversation() 一次 → 反馈行还在、rating 没变**
  ⑧ **delete_conversation() → 反馈行仍在**（对话没了，分析数据还在）

谁日后「顺手把外键补上」，这两条会当场变红。

⚠ **①②③④ 是离线断言**（接线 / DDL 文本 / 降级 / 路由映射），必须跑在 `warmup()` **之前**：
它们恰恰是「数据库连不上时」才需要成立的性质，而 `probe_pg_store.py` 一旦连不上就
`return 1`，那几条在那里永远跑不到。本脚本因此把执行顺序排成 ①②③④ → ⑤⑥⑦⑧⑨⑩。

跑法：

    uv run python scripts/probe_feedback.py
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import conv_store  # noqa: E402
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.schemas import ConversationUpsert, FeedbackRow, FeedbackUpsert  # noqa: E402

# 所有临时数据都带这个前缀，跑完自己清干净（也顺手清掉上次崩在半路的残留）
ID_PREFIX = "probe-fb-"

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
def make_message(mid: str, role: str, content: str, **extra: Any) -> dict:
    """一条**形状完整**的前端消息（照 `probe_conv_store.make_message`）。

    刻意带上前端各个可选字段 —— 反馈那半边的设计（「哪个拇指亮着」随 `payload jsonb` 走）
    依赖的正是「后端不解释消息内部结构」，所以这里要送真形状。
    """
    msg = {
        "id": mid,
        "role": role,
        "content": content,
        "sources": [
            {"index": 1, "doc_id": "kb-12", "question": "问", "answer": "答", "score": 0.87},
            {"index": 2, "doc_id": "web:https://x.gov.cn/a", "question": "问", "answer": "答", "score": 0.9},
        ],
        "streaming": False,
    }
    msg.update(extra)
    return msg


def upsert(title: str, messages: list[dict], created: int, updated: int) -> ConversationUpsert:
    return ConversationUpsert(
        title=title, created_at=created, updated_at=updated, messages=messages
    )


def fb(rating: int = 1, question: str = "履约保证金最高限额？",
       answer: str = "不得超过中标合同金额的 10%。",
       doc_ids: list[str] | None = None) -> FeedbackUpsert:
    return FeedbackUpsert(
        rating=rating,
        question=question,
        answer=answer,
        doc_ids=["kb-12", "web:https://x.gov.cn/a"] if doc_ids is None else doc_ids,
    )


def cleanup() -> tuple[int, int]:
    """删掉所有 `probe-fb-` 开头的测试对话**与反馈行**（本次的 + 上次崩在半路留下的）。

    ⚠ 反馈行**必须单独删**：它没有外键（那正是这个设计的关键），所以
      `delete_conversation` 不会顺手带走它。残留会污染 ⑩ 的过滤断言。
    ⚠ 用 `DELETE ... RETURNING 1` 而不是裸 DELETE：`_run` 无条件 `cur.fetchall()`，
      而 psycopg 对「没产生结果集的语句」fetchall 会抛 ProgrammingError。
    """
    gone = 0
    for meta in conv_store.list_conversations(limit=500):
        if meta.id.startswith(ID_PREFIX):
            conv_store.delete_conversation(meta.id)
            gone += 1
    rows = conv_store._run(
        "DELETE FROM feedback WHERE conversation_id LIKE %s RETURNING 1",
        (ID_PREFIX + "%",),
    )
    return gone, len(rows)


def _fb_rows(conv_id: str) -> list[FeedbackRow]:
    return conv_store.list_feedback(conversation_id=conv_id, limit=100)


def _raw_count(conv_id: str) -> int:
    rows = conv_store._run(
        "SELECT count(*) AS n FROM feedback WHERE conversation_id = %s", (conv_id,)
    )
    return int(rows[0]["n"])


# ---------------------------------------------------------------- ① 接线（离线）
def probe_wiring() -> None:
    banner("① 接线：三条路由 + 两个模型 + conv_store 的三个函数（离线）")

    from bidding_qa import api

    routes = {
        (getattr(r, "path", ""), method)
        for r in api.app.routes
        for method in (getattr(r, "methods", None) or ())
    }
    check(
        "PUT /api/feedback/{conversation_id}/{message_id} 已注册",
        ("/api/feedback/{conversation_id}/{message_id}", "PUT") in routes,
    )
    check(
        "DELETE /api/feedback/{conversation_id}/{message_id} 已注册",
        ("/api/feedback/{conversation_id}/{message_id}", "DELETE") in routes,
    )
    check("GET /api/feedback 已注册", ("/api/feedback", "GET") in routes)
    # ⚠ 三条必须都在**对话历史那五条之后**声明，且 GET 是字面量路径 ——
    #   若哪天冒出一条 `/api/feedback/{something}`，声明顺序就成了静默陷阱
    #   （同 `/api/conversations/search` 必须先于 `{conv_id}` 那条）。
    check(
        "没有 /api/feedback/{...} 形式的路径参数路由（不会与 GET /api/feedback 抢匹配）",
        not any(p.startswith("/api/feedback/{") and m == "GET" for p, m in routes),
    )

    fields = FeedbackUpsert.model_fields
    check(
        "FeedbackUpsert 字段齐（rating/question/answer/doc_ids）",
        set(fields) == {"rating", "question", "answer", "doc_ids"},
        detail=str(sorted(fields)),
    )
    check(
        "FeedbackUpsert 的 rating 是 int（刻意不用 Literal[1,-1]，见模型注释）",
        fields["rating"].annotation is int,
        detail=str(fields["rating"].annotation),
    )
    check(
        "FeedbackUpsert 三个快照字段都有默认值（前端可以只提交 rating）",
        all(not fields[n].is_required() for n in ("question", "answer", "doc_ids")),
    )
    check(
        "FeedbackRow 字段齐且含时间戳",
        set(FeedbackRow.model_fields)
        == {"conversation_id", "message_id", "rating", "question", "answer", "doc_ids",
            "created_at", "updated_at"},
        detail=str(sorted(FeedbackRow.model_fields)),
    )
    for name in ("save_feedback", "delete_feedback", "list_feedback"):
        check(f"conv_store.{name} 存在且可调用", callable(getattr(conv_store, name, None)))
    check("conv_store._fb 转换器存在", callable(getattr(conv_store, "_fb", None)))
    check(
        "反馈有独立的不可用文案（不能复用对话历史那句）",
        conv_store._FEEDBACK_UNAVAILABLE_MSG != conv_store._UNAVAILABLE_MSG,
        detail=conv_store._FEEDBACK_UNAVAILABLE_MSG,
    )
    check(
        "_guard 的第三个形参有默认值（现有调用点一个字没改）",
        _guard_msg_has_default(),
    )


def _guard_msg_has_default() -> bool:
    import inspect

    params = inspect.signature(conv_store._guard).parameters
    return "msg" in params and params["msg"].default is conv_store._UNAVAILABLE_MSG


# ---------------------------------------------------------------- ② DDL 文本（离线）
def probe_ddl() -> None:
    banner("② DDL 文本：feedback 表**没有外键**（离线）")

    joined = "\n".join(conv_store._DDL)
    low = joined.lower()
    check("feedback 表在 _DDL 里", "create table if not exists feedback" in low)
    check(
        "全部是 IF NOT EXISTS（多跑无害）",
        all("if not exists" in s.lower() for s in conv_store._DDL),
    )

    # ⚠⚠ **断言必须限定在 feedback 那一条语句里，绝不能针对整个 `_DDL` 拼接结果。**
    #   整表拼接里**本来就有** `REFERENCES` —— `messages` 那条的
    #   `conversation_id text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE`。
    #   写成 `"REFERENCES" not in joined` 的话这条断言会以一个**完全错误的理由**变红
    #   （写这个探针时先踩了一次，报出来像是「刚写的无外键规则被自己违反了」）。
    fb = next(
        s for s in conv_store._DDL
        if "create table if not exists feedback" in s.lower()
    )
    fb_low = fb.lower()

    check(
        "⚠⚠ feedback 表里一处 REFERENCES 都没有（加了会让每次自动保存静默删光评价）",
        "references" not in fb_low,
        detail="有外键！`save_conversation` 的整批重插会级联删掉所有反馈",
    )
    check(
        "⚠⚠ feedback 表里没有 ON DELETE（同上）",
        "on delete" not in fb_low,
        detail="有级联规则！删对话/重存对话会带走反馈",
    )
    # 反面证据：整个 _DDL 拼接里**确实有** REFERENCES。这条不是废话 —— 它是
    # 上面那条断言**必须限定范围**的凭证：整串检查会红，而红得毫无意义。
    check(
        "（范围凭证）整个 _DDL 拼接里确实有 REFERENCES —— 所以上面两条只能查 feedback 那一条",
        "references conversations(id) on delete cascade" in low,
    )

    check(
        "主键是 (conversation_id, message_id)",
        re.search(r"primary\s+key\s*\(\s*conversation_id\s*,\s*message_id\s*\)", fb_low) is not None,
        detail=fb_low,
    )
    check("rating 是 NOT NULL 的小整数", re.search(r"rating\s+smallint\s+not null", fb_low) is not None)
    check(
        "doc_ids 是 jsonb NOT NULL 且默认空数组",
        re.search(r"doc_ids\s+jsonb\s+not null\s+default\s+'\[\]'::jsonb", fb_low) is not None,
    )
    check("question / answer 是 NOT NULL DEFAULT ''", fb_low.count("text") >= 2 and "not null default ''" in fb_low)
    check(
        "时间戳是 timestamptz（不是 bigint epoch —— 库里要可读）",
        len(re.findall(r"timestamptz\s+not null", fb_low)) == 2,
        detail=fb_low,
    )
    check("feedback_created_idx 索引在", "feedback_created_idx" in joined)

    # 列数固定：日后加列（比如「👎 原因」）必须**手工 DROP + CREATE**，
    # 因为 `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作、既不补列也不改列。
    # ⚠ 匹配必须带上类型名：只写 `^\s{4}(\w+)\s` 会把 `    CREATE TABLE …` 也算成一列。
    n_cols = len(re.findall(r"^\s*(\w+)\s+(?:text|smallint|jsonb|timestamptz)\b", fb, re.M))
    check("列数与设计一致（加列必须手工 DROP + CREATE）", n_cols == 8, detail=f"{n_cols} 列")


# ---------------------------------------------------------------- ③ 降级（离线）
def probe_degradation() -> None:
    banner("③ 降级：一律抛 ConvStoreUnavailable，用**反馈那句**文案（离线）")

    calls = {
        "save_feedback": lambda: conv_store.save_feedback("x", "y", fb()),
        "delete_feedback": lambda: conv_store.delete_feedback("x", "y"),
        "list_feedback": lambda: conv_store.list_feedback(),
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
                text = str(exc)
                check(f"未配置时 {name} 抛 ConvStoreUnavailable", bool(text), detail=text)
                # ⚠ 这一条是本节的**主要价值**：复用对话历史那句文案的话，用户点了 👎
                #   会读到「本次对话将只保存在本机浏览器」—— 说的是一件**没发生的事**。
                check(
                    f"未配置时 {name} 用的是反馈文案（不是对话历史那句）",
                    text == conv_store._FEEDBACK_UNAVAILABLE_MSG,
                    detail=text,
                )
            except Exception as exc:  # noqa: BLE001
                check(f"未配置时 {name} 抛 ConvStoreUnavailable", False,
                      detail=f"抛的是 {type(exc).__name__}")
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
    marker = "boom-marker-7a1e"  # 假装这是连接串里的主机名/用户名
    conv_store._get_conn = lambda: (_ for _ in ()).throw(psycopg.OperationalError(marker))
    try:
        for name, fn in calls.items():
            try:
                fn()
                check(f"{name} 把 psycopg 异常包成 ConvStoreUnavailable", False, detail="竟然正常返回了")
            except conv_store.ConvStoreUnavailable as exc:
                text = str(exc)
                check(f"{name} 抛 ConvStoreUnavailable", True, detail=text)
                check(
                    f"{name} 的报错不含原始异常内容（连接串/主机名不外泄）",
                    marker not in text,
                    detail=text,
                )
            except Exception as exc:  # noqa: BLE001
                check(f"{name} 把 psycopg 异常包成 ConvStoreUnavailable", False,
                      detail=f"抛的是 {type(exc).__name__}: {exc}")
    finally:
        conv_store._get_conn = real_get_conn
        conv_store._conn, conv_store._failed_at, conv_store._last_error = snapshot
        check("冷却状态已还原（否则后面每节都会假失败）", conv_store._failed_at is snapshot[1])


# ---------------------------------------------------------------- ④ 路由映射（离线）
def probe_routes() -> None:
    banner("④ 路由层：异常 → HTTP 状态码的映射（离线，替身 store）")

    try:
        from fastapi.testclient import TestClient

        from bidding_qa import api
    except Exception as exc:  # noqa: BLE001
        check("能构造 TestClient", False, detail=f"{type(exc).__name__}: {exc}")
        return

    # ⚠ **不进 `with`**：进入上下文会跑 lifespan → `_bootstrap()` → 加载索引与向量模型
    #   （几十秒）。不进入正好还多验一条 —— 这 3 条路由**不依赖启动完成**
    #   （它们刻意不调 `_require_ready()`，否则重启后头几十秒反馈点了没反应）。
    client = TestClient(api.app)

    unavailable = conv_store.ConvStoreUnavailable("测试用的反馈不可用文案")
    saved = {name: getattr(conv_store, name)
             for name in ("save_feedback", "delete_feedback", "list_feedback")}

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise unavailable

    try:
        for name in saved:
            setattr(conv_store, name, boom)

        cases = [
            ("PUT 503", lambda: client.put("/api/feedback/c1/m1", json={"rating": 1})),
            ("DELETE 503", lambda: client.delete("/api/feedback/c1/m1")),
            ("GET 503", lambda: client.get("/api/feedback")),
        ]
        for label, call in cases:
            resp = call()
            check(
                f"{label}（能力不可用 → 服务端不可用，不是 500）",
                resp.status_code == 503 and resp.json().get("detail") == "测试用的反馈不可用文案",
                detail=f"{resp.status_code} {resp.text[:120]}",
            )

        # ---- 非法 rating → 400，且**在碰 store 之前**就拒掉 ----
        touched: list[str] = []
        setattr(conv_store, "save_feedback",
                lambda *a, **k: touched.append("save_feedback"))
        for bad in (0, 2, -3):
            touched.clear()
            resp = client.put("/api/feedback/c1/m1", json={"rating": bad})
            check(
                f"rating={bad} → 400 且中文提示（不是 pydantic 的 422 英文）",
                resp.status_code == 400 and "rating 只能是" in resp.json().get("detail", ""),
                detail=f"{resp.status_code} {resp.text[:120]}",
            )
            check(f"rating={bad} 时**没有**碰到 store", touched == [], detail=str(touched))

        # ---- 正常路径：snake_case 原样出网 + 时间戳真的是整数 ----
        setattr(conv_store, "save_feedback", lambda *a, **k: None)
        resp = client.put("/api/feedback/c1/m1", json={"rating": 1, "question": "问", "answer": "答",
                                                       "doc_ids": ["kb-12"]})
        check("PUT 200", resp.status_code == 200, detail=resp.text[:120])

        # DELETE 幂等
        setattr(conv_store, "delete_feedback", lambda *a, **k: None)
        codes = {client.delete("/api/feedback/c1/m1").status_code for _ in range(2)}
        check("DELETE 幂等（重复删都是 200）", codes == {200}, detail=str(codes))

        setattr(
            conv_store,
            "list_feedback",
            lambda *_a, **_k: [
                FeedbackRow(
                    conversation_id="c1", message_id="m1", rating=-1,
                    question="问", answer="答", doc_ids=["kb-12", "web:https://x/a"],
                    created_at=1758336000123, updated_at=1758336999456,
                )
            ],
        )
        resp = client.get("/api/feedback", params={"rating": -1})
        row = resp.json()[0] if resp.status_code == 200 and resp.json() else {}
        check("GET 200", resp.status_code == 200, detail=resp.text[:120])
        check(
            "载荷是 snake_case（camelCase 会在前端转换，别漏到后端）",
            set(row) == {"conversation_id", "message_id", "rating", "question", "answer",
                         "doc_ids", "created_at", "updated_at"},
            detail=str(sorted(row)),
        )
        # ⚠ 时间戳必须是 int：不是 int 的 `datetime` 会在 `api.py` 那句 `json.dumps`
        #   处炸成 TypeError（«Decimal / date» 老坑，本项目第五次）。
        check(
            "created_at / updated_at 是整数（epoch 毫秒），不是字符串/日期",
            isinstance(row.get("created_at"), int) and isinstance(row.get("updated_at"), int),
            detail=f"{type(row.get('created_at')).__name__}",
        )
        check("doc_ids 原样出网（网页条目那条带 web: 前缀的也在）",
              row.get("doc_ids") == ["kb-12", "web:https://x/a"], detail=str(row.get("doc_ids")))

        # 查询参数原样透传给 store（rating=0 是「不过滤」，会一路传到 conv_store）
        seen: dict[str, Any] = {}

        def spy(rating: int = 0, conversation_id: str = "", limit: int = 100) -> list:
            seen.update(rating=rating, conversation_id=conversation_id, limit=limit)
            return []

        setattr(conv_store, "list_feedback", spy)
        client.get("/api/feedback", params={"rating": -1, "conversation_id": "c9", "limit": 7})
        check(
            "查询参数原样透传（rating / conversation_id / limit）",
            seen == {"rating": -1, "conversation_id": "c9", "limit": 7},
            detail=str(seen),
        )
        client.get("/api/feedback")
        check(
            "不传参数时用默认值（rating=0 表示不过滤）",
            seen == {"rating": 0, "conversation_id": "", "limit": 100},
            detail=str(seen),
        )
    finally:
        for name, fn in saved.items():
            setattr(conv_store, name, fn)


# ---------------------------------------------------------------- ⑤ 往返
def probe_roundtrip() -> None:
    banner("⑤ 往返：put → list 逐字段相等")

    conv_id = f"{ID_PREFIX}rt"
    conv_store.delete_conversation(conv_id)
    conv_store.delete_feedback(conv_id, "m1")  # 清残留

    data = fb(rating=1, question="履约保证金最高限额是多少？",
              answer="招标文件要求中标人提交履约保证金的，不得超过中标合同金额的 10%。",
              doc_ids=["kb-12", "kb-33", "web:https://ah.gov.cn/zfcg/1"])
    conv_store.save_feedback(conv_id, "m1", data)

    rows = _fb_rows(conv_id)
    check("写入后读到 1 条", len(rows) == 1, detail=f"{len(rows)} 条")
    if not rows:
        return
    row = rows[0]
    check("conversation_id 相等", row.conversation_id == conv_id, detail=row.conversation_id)
    check("message_id 相等", row.message_id == "m1", detail=row.message_id)
    check("rating 相等", row.rating == 1, detail=str(row.rating))
    check("question 相等（中文没被转义坏）", row.question == data.question, detail=row.question)
    check("answer 相等（中文没被转义坏）", row.answer == data.answer, detail=row.answer)
    check("doc_ids 相等且保序", row.doc_ids == data.doc_ids, detail=str(row.doc_ids))
    # ⚠ 时间戳往返逐毫秒相等（同 probe_conv_store ⑧）：`int()` 截断的实现会差 1ms
    check("created_at / updated_at 是毫秒级的合理值",
          row.created_at > 1_600_000_000_000 and abs(row.created_at - row.updated_at) < 5000,
          detail=f"{row.created_at} / {row.updated_at}")

    # 空 doc_ids 也要能存（前端一条来源都没有时就是空数组）
    conv_store.save_feedback(conv_id, "m2", fb(rating=-1, doc_ids=[]))
    row2 = next((r for r in _fb_rows(conv_id) if r.message_id == "m2"), None)
    check("doc_ids 空数组存得下（不是 NULL）", row2 is not None and row2.doc_ids == [],
          detail=str(row2.doc_ids if row2 else None))

    # 默认值：只给 rating 也能存
    conv_store.save_feedback(conv_id, "m3", FeedbackUpsert(rating=1))
    row3 = next((r for r in _fb_rows(conv_id) if r.message_id == "m3"), None)
    check("只提交 rating 也能存（三个快照字段有默认值）",
          row3 is not None and row3.question == "" and row3.answer == "" and row3.doc_ids == [],
          detail=str(row3))


# ---------------------------------------------------------------- ⑥ upsert
def probe_upsert() -> None:
    banner("⑥ upsert：同一条消息再评一次 = 覆盖（created_at 不变）")

    conv_id = f"{ID_PREFIX}upsert"
    conv_store.delete_conversation(conv_id)

    conv_store.save_feedback(conv_id, "m1", fb(rating=1, answer="第一版"))
    first = _fb_rows(conv_id)[0]
    time.sleep(0.02)  # 让 updated_at 与 created_at 拉出可分辨的差

    conv_store.save_feedback(conv_id, "m1", fb(rating=-1, answer="第二版"))
    rows = _fb_rows(conv_id)

    check("仍然只有 1 行（是 upsert 不是追加）", len(rows) == 1, detail=f"{len(rows)} 行")
    check("库里也只有 1 行", _raw_count(conv_id) == 1, detail=str(_raw_count(conv_id)))
    if len(rows) != 1:
        return
    row = rows[0]
    check("rating 被翻转（1 → -1）", row.rating == -1, detail=str(row.rating))
    check("快照被覆盖（answer 变成第二版）", row.answer == "第二版", detail=row.answer)
    check(
        "⚠ created_at **保持首次那一个**（同 conversations 的 upsert 约定）",
        row.created_at == first.created_at,
        detail=f"{row.created_at} vs {first.created_at}",
    )
    check(
        "updated_at 变了（证明这次确实是一次更新）",
        row.updated_at > first.updated_at,
        detail=f"{row.updated_at} vs {first.updated_at}",
    )


# ---------------------------------------------------------------- ⑦⑧ 无外键（本节核心）
def probe_no_fk() -> None:
    banner("⑦⑧ ⚠⚠ 无外键：重存对话 / 删对话后反馈都还在（本节是设计的看门人）")

    conv_id = f"{ID_PREFIX}nosfk"
    conv_store.delete_conversation(conv_id)

    # ---- 两半共存：Message.feedback 随 payload 走 ----
    msgs = [
        make_message("m1", "user", "履约保证金最高限额是多少？"),
        make_message("m2", "assistant", "不得超过中标合同金额的 10%。", feedback=1),
    ]
    conv_store.save_conversation(conv_id, upsert("问一答", msgs, 1, 1))
    full = conv_store.get_conversation(conv_id)
    got = next((m for m in (full.messages if full else []) if m["id"] == "m2"), None)
    check(
        "Message.feedback 原样穿过 payload jsonb（后端零改动地存下「哪个拇指亮着」）",
        got is not None and got.get("feedback") == 1,
        detail=str(got),
    )

    # 记一条评价
    conv_store.save_feedback(conv_id, "m2", fb(rating=1, answer="不得超过中标合同金额的 10%。"))
    check("评价已入库", _raw_count(conv_id) == 1, detail=str(_raw_count(conv_id)))

    # ---- ⑦ 重新保存对话：这**正是** `save_conversation` 会 DELETE FROM messages 的那一步 ----
    conv_store.save_conversation(
        conv_id,
        upsert("问一答（第二版）", [msgs[0], {**msgs[1], "content": "改过的答案正文"}], 1, 2),
    )
    full2 = conv_store.get_conversation(conv_id)
    check(
        "（前置）消息确实被整体替换了（重存真的走了 DELETE + INSERT 那条路）",
        full2 is not None and len(full2.messages) == 2
        and full2.messages[1].get("content") == "改过的答案正文",
        detail=str(full2.messages if full2 else None)[:160],
    )
    rows = _fb_rows(conv_id)
    check(
        "⚠⚠ 重新 save_conversation 后**反馈行还在**（加了外键这里会静默变 0）",
        len(rows) == 1,
        detail=f"{len(rows)} 条 —— 反馈被级联删了！feedback 上不该有 REFERENCES",
    )
    check(
        "⚠⚠ 反馈的 rating 没被那次重存冲掉",
        bool(rows) and rows[0].rating == 1,
        detail=str(rows[0].rating if rows else None),
    )
    check(
        "反馈里的快照仍是**评价那一刻**的正文（不跟着消息改）",
        bool(rows) and rows[0].answer == "不得超过中标合同金额的 10%。",
        detail=rows[0].answer if rows else None,
    )

    # ---- ⑧ 删掉整条对话 ----
    conv_store.delete_conversation(conv_id)
    check("对话确实没了", conv_store.get_conversation(conv_id) is None)
    check(
        "⚠⚠ 删对话后反馈行**仍在**（「攒下来用于优化检索」要的正是这个）",
        _raw_count(conv_id) == 1,
        detail=f"{_raw_count(conv_id)} 条 —— 反馈跟着对话一起没了",
    )

    # 反面：外键真的**没有**才可能这样。查一次目录确认（不是靠读源码相信它）
    rows = conv_store._run(
        """
        SELECT count(*) AS n
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'feedback' AND c.contype = 'f'
        """
    )
    check(
        "（独立凭证）库里 feedback 表上真的**没有外键约束**",
        int(rows[0]["n"]) == 0,
        detail=f"{rows[0]['n']} 个外键约束",
    )


# ---------------------------------------------------------------- ⑨ 删除幂等
def probe_delete() -> None:
    banner("⑨ 取消评价：幂等、只删指着的那一条")

    conv_id = f"{ID_PREFIX}del"
    conv_store.delete_conversation(conv_id)
    for mid in ("m1", "m2"):
        conv_store.save_feedback(conv_id, mid, fb(rating=1))
    check("先存两条", _raw_count(conv_id) == 2, detail=str(_raw_count(conv_id)))

    conv_store.delete_feedback(conv_id, "m1")
    check("删掉一条 → 少一条", _raw_count(conv_id) == 1, detail=str(_raw_count(conv_id)))
    check("删掉的是 m1", [r.message_id for r in _fb_rows(conv_id)] == ["m2"])

    # 幂等：删不存在的 key 是空操作（前端「再点一次 = 取消」会重复发）
    for _ in range(2):
        try:
            conv_store.delete_feedback(conv_id, "m1")
            conv_store.delete_feedback(conv_id, "从没存过的")
            check("删不存在的 key 不报错（幂等）", True)
        except Exception as exc:  # noqa: BLE001
            check("删不存在的 key 不报错（幂等）", False, detail=f"{type(exc).__name__}: {exc}")
    check("剩下的那条没被误删", _raw_count(conv_id) == 1, detail=str(_raw_count(conv_id)))


# ---------------------------------------------------------------- ⑩ 过滤与夹紧
def probe_filters() -> None:
    banner("⑩ 过滤 / 排序 / limit 夹紧")

    conv_id = f"{ID_PREFIX}flt"
    other = f"{ID_PREFIX}flt2"
    conv_store.delete_conversation(conv_id)
    conv_store.delete_conversation(other)

    good = fb(rating=1, question="好的问题")
    bad = fb(rating=-1, question="坏的问题")
    conv_store.save_feedback(conv_id, "m1", good)
    time.sleep(0.01)
    conv_store.save_feedback(conv_id, "m2", bad)
    time.sleep(0.01)
    conv_store.save_feedback(other, "m1", bad)

    rows = conv_store.list_feedback(conversation_id=conv_id, limit=100)
    check("按对话过滤只回本对话的", len(rows) == 2, detail=f"{len(rows)} 条")
    check(
        "按 created_at DESC 排（最新那条在前）",
        [r.message_id for r in rows] == ["m2", "m1"],
        detail=str([r.message_id for r in rows]),
    )

    ups = conv_store.list_feedback(rating=1, conversation_id=conv_id, limit=100)
    check("rating=1 只回好评", [r.message_id for r in ups] == ["m1"], detail=str([r.message_id for r in ups]))
    downs = conv_store.list_feedback(rating=-1, conversation_id=conv_id, limit=100)
    check("rating=-1 只回差评", [r.message_id for r in downs] == ["m2"], detail=str([r.message_id for r in downs]))
    both = conv_store.list_feedback(rating=0, conversation_id=conv_id, limit=100)
    check("rating=0 表示**不过滤**（不是「rating 为 0 的」）", len(both) == 2, detail=f"{len(both)} 条")
    check(
        "rating=-1 能跨对话取（分析时看全部差评）",
        {r.conversation_id for r in conv_store.list_feedback(rating=-1, limit=200)} >= {conv_id, other},
    )

    check("limit 生效", len(conv_store.list_feedback(conversation_id=conv_id, limit=1)) == 1)
    # 夹紧：给个荒唐的值也不能把全表拖出来（`_FEEDBACK_HARD_CAP`）
    try:
        rows = conv_store.list_feedback(limit=100000)
        check("limit=100000 被夹紧而不是真去取十万条", True, detail=f"回了 {len(rows)} 条")
    except Exception as exc:  # noqa: BLE001
        check("limit=100000 被夹紧而不是真去取十万条", False, detail=f"{type(exc).__name__}: {exc}")
    try:
        conv_store.list_feedback(limit=0)
        check("limit=0 不炸（夹到下限 1）", True)
    except Exception as exc:  # noqa: BLE001
        check("limit=0 不炸（夹到下限 1）", False, detail=f"{type(exc).__name__}: {exc}")
    check("_FEEDBACK_HARD_CAP 是个合理的小数字", 0 < conv_store._FEEDBACK_HARD_CAP <= 5000,
          detail=str(conv_store._FEEDBACK_HARD_CAP))

    # 空字符串的 conversation_id = 不过滤（与 rating=0 同一条约定）
    check("conversation_id='' 表示不过滤", len(conv_store.list_feedback(limit=200)) >= 3)


class _Empty:
    messages: list = []
    message_count = -1


_EMPTY = _Empty()


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
    if not status["available"]:
        banner(f"结果：{_pass} 通过 / {_fail} 失败（预热失败: {status['detail']}）")
        return 1
    print(f"\n预热: 对话历史库连接正常（{status['detail']}）")

    try:
        probe_roundtrip()
        probe_upsert()
        probe_no_fk()
        probe_delete()
        probe_filters()
    finally:
        try:
            convs, fbs = cleanup()
            print(f"\n清理: 删掉 {convs} 条 {ID_PREFIX}* 测试对话、{fbs} 条反馈")
        except Exception as exc:  # noqa: BLE001
            print(f"\n⚠ 清理失败（残留 {ID_PREFIX}* 数据）: {type(exc).__name__}: {exc}")

    banner(f"结果：{_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
