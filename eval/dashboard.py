#!/usr/bin/env python
"""Agent 运行看板 —— 评估体系的「总账」那一半。

    uv run python eval/dashboard.py

==================================================
  Agent 运行看板
==================================================
  总对话数     : 2
  用户反馈     : 0 条
    👍 满意     : 0 (—)
    👎 不满意   : 0 (—)
  检索缓存     : (暂无数据)
==================================================

**它与 `eval/agent_eval.py` 是互补的两半，回答两个不同的问题**：

| 脚本 | 答什么 | 数据源 | 什么时候跑 |
| --- | --- | --- | --- |
| `agent_eval.py` | Agent **答得好不好**（抽样体检：10 条固定用例 + LLM 裁判） | 语料 + 真机跑一遍 | 改检索/提示词/工具之后 |
| `dashboard.py`（本脚本） | **线上跑成什么样**（总账：攒了多少对话、用户满不满意） | PostgreSQL | 随时 |

⚠⚠ **本脚本不碰 Qdrant**，所以**不像 `agent_eval.py` 那样需要先停掉后端** ——
两者都只读 PostgreSQL 的三张表（`conversations` / `messages` / `feedback`），
后端正在跑也照跑。这是它俩运维前提上唯一、也是最大的差别。

三条设计决定（都是「别让报表说谎」，改之前先读）：

1. **计数走真 `count(*)`，不用 `len(list_*)`**。`conv_store` 那两个列表函数都封顶 500，
   数据长起来会给出一个**看起来完全合理的错误数字**。见 `conv_store.usage_stats()`。
2. **零反馈打 `0 (—)`，不打 `0 (0%)`** —— `0%` 断言了一个从没测量过的比率。
   同 `page.tsx` 的 `webConfigured` / `historySynced` 那套**三态**纪律。
3. **`检索缓存` 如实报「没有这个数据源」**，不造一个 0% 命中率出来。见 `_cache_stats()`。

已知局限：

- **只读、不改任何数据**：不建也不删对话/反馈。⚠ 连接路径上那条
  `CREATE TABLE IF NOT EXISTS` 是 `conv_store` 既有的幂等 DDL（读路由也走同一条路，
  见 `ensure_schema`），不是本脚本引入的。
- **`检索缓存` 恒为 `(暂无数据)`**：仓库里没有检索缓存子系统（见 `_cache_stats()` 的说明）。
- **没有时间维度**（今日/本周）：给的是全量。加了会改输出形状，需要先谈。
- **没有用户维度**：沿用「对话历史持久化」那条**单用户共享**假设（没有 `user_id` 列）。

改 `conv_store.usage_stats()` 之后跑 `scripts/probe_conv_store.py` ⑫。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unicodedata
from pathlib import Path
from typing import Any

# ⚠ 版本先拦一道：本机 PATH 上的裸 `python` 是 3.8.8（不是 .venv 的那个），
#   直接跑会在下面某个 3.12 语法处炸出一堆看不懂的 SyntaxError。
#   同 `eval/agent_eval.py`。
if sys.version_info < (3, 12):
    print(f"✗ 需要 Python ≥ 3.12，当前是 {sys.version.split()[0]}。")
    print("  这个解释器不是本项目的 .venv —— 请改用：")
    print("      uv run python eval/dashboard.py")
    raise SystemExit(2)

# ⚠ Windows Git Bash 的 stdout 默认是 GBK，而下面要打 👍/👎 ——
#   不 reconfigure 会 UnicodeEncodeError（同 `curl -d` / `psql -c` 那个老病）。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import bidding_qa.conv_store as conv_store  # noqa: E402
from bidding_qa.conv_store import ConvStoreUnavailable  # noqa: E402

WIDTH = 50
TITLE = "Agent 运行看板"

#: 「没数据」的统一写法。⚠ 与 `0` 不是一回事 —— 见模块 docstring 第 2 条。
_EMPTY = "(暂无数据)"

#: ⚠ 看板自己的降级文案（用户规格里没有这一行，是出错时才出现的）。
#: `_guard` 抛的就是这个字符串本身（原始 psycopg 异常只进日志），所以它得自带「谁失败了」。
_STATS_UNAVAILABLE_MSG = "看板需要的数据库暂时不可用。"


# ---------------------------------------------------------------- 排版
def _dwidth(text: str) -> int:
    """终端显示宽度：CJK 算 2 列。

    ⚠ 与 `eval/agent_eval.py:_dwidth` 是**同一条约定**，这里是**刻意复制的 3 行**：
    那个模块 import 期会 `sys.path.insert`、跑版本守卫（提示语还写死了
    `eval/agent_eval.py`，从看板里炸出来会误导）、并拖进整个 `eval_rag`。
    为 3 行助手付那些副作用的账不划算。
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _line(label: str, value: str) -> str:
    """父行：`  标签<pad>: 值`，冒号对齐到第 16 列（0-based 索引，同 `agent_eval`）。

    ⚠ 用户给的示例里 `检索缓存:` 那行**没对齐**（其余两行对齐了），那是手打漏的 ——
    这里一律对齐，冒号列不齐看起来像脚本有 bug。
    ⚠ 填充是 `max(0, …)` 不是 `max(1, …)`：宽度**正好 14** 的标签会多顶一格。
    """
    return f"  {label}{' ' * max(0, 14 - _dwidth(label))}: {value}"


def _sub_line(icon: str, label: str, value: str) -> str:
    """子行：缩进 4，冒号与父行**同一列**（所以字段宽是 12 不是 14）。

    ⚠👍/👎 的显示宽度**依终端而定**（多数现代终端当宽字符 = 2 列，部分当 4 列）。
    这里一律按 `_dwidth` 算，所以在渲染不一的终端上仍可能差一列 ——
    这是 emoji 的固有问题，`--selftest` ② 钉的是**本函数内部自洽**，
    不保证在你那台终端上视觉对齐。
    """
    block = f"{icon} {label}"
    return f"    {block}{' ' * max(0, 12 - _dwidth(block))}: {value}"


def _pct(part: int, total: int) -> str:
    """占比。**`total == 0` 时返回 `—` 而不是 `0%`**（模块 docstring 第 2 条）。

    `0%` 的意思是「测了，全是差评」；`—` 的意思是「还没有人评过」。两者完全不同，
    而库里现在（2026-09-21）就正是后者。
    """
    if total <= 0:
        return "—"
    return f"{round(part / total * 100)}%"


# ---------------------------------------------------------------- 数据
def _cache_stats() -> dict[str, Any] | None:
    """检索缓存统计 —— **本仓库现在没有这个子系统，恒返回 `None`**。

    ⚠⚠ 这不是「还没实现」，是**如实报告一个不存在的数据源**。全仓没有任何检索缓存层：
    `retriever.search()` 每次都真跑；`grep -rn cache src/` 只命中 SSE 的
    `Cache-Control` 响应头、HF 模型缓存目录、`llm_factory` 的按供应商实例缓存、
    以及 `__pycache__`。所以这一行**只能**打 `(暂无数据)` ——
    造一个 `0% 命中` 出来是在说一件没发生过的事。

    要接真东西：先给 `retriever` 加一层以 `(query, mode, top_k, rerank)` 为键的缓存，
    把命中/未命中计数记下来，再从这里返回 `{"hits": …, "misses": …}`。
    **别拿别的指标凑数**（那会让这一行看起来有数据、实际答的不是这个问题）。
    """
    return None


def _cache_line(cache: dict[str, Any] | None) -> str:
    """⚠ 非 `None` 那一支今天**到不了**（上面恒返回 `None`）—— 它是给日后接上缓存时
    预留的形状，由 `--selftest` ⑤ 用替身钉着，不是死代码。
    """
    if cache is None:
        return _EMPTY
    return f"命中 {cache.get('hits', 0)} / 未命中 {cache.get('misses', 0)}"


def render(stats: dict[str, int], cache: dict[str, Any] | None = None) -> str:
    """渲染整块报表。**纯函数**（数据进、字符串出），`--selftest` 靠这一点断言。"""
    total = int(stats.get("feedback") or 0)
    up = int(stats.get("up") or 0)
    down = int(stats.get("down") or 0)
    bar = "=" * WIDTH
    return "\n".join(
        [
            bar,
            f"  {TITLE}",
            bar,
            _line("总对话数", str(int(stats.get("conversations") or 0))),
            _line("用户反馈", f"{total} 条"),
            _sub_line("👍", "满意", f"{up} ({_pct(up, total)})"),
            _sub_line("👎", "不满意", f"{down} ({_pct(down, total)})"),
            _line("检索缓存", _cache_line(cache)),
            bar,
        ]
    )


def gather() -> dict[str, int]:
    """取数。连不上/没配置时抛 `ConvStoreUnavailable`（由 `main()` 转成一行提示）。"""
    return conv_store.usage_stats(_STATS_UNAVAILABLE_MSG)


def _status_detail() -> str:
    """降级时补一行诊断。`conv_status()` **不抛、也不触发连接**，失败路径上是安全的。

    ⚠ 它成功时只报 `host:port/db`（设计如此，`conv_status()` 的注释写着
    「绝不能把连接串或密码带出去」）；失败时给的是 `_last_error` 的原文 ——
    对着本机 `.env` 跑的开发者在自己的终端上看到连接错误，正是这里想要的效果。
    """
    try:
        detail = str(conv_store.conv_status().get("detail") or "")
    except Exception:  # noqa: BLE001 —— 诊断行自己绝不能把主流程搞崩
        return ""
    return detail


# ---------------------------------------------------------------- 入口
def main() -> int:
    try:
        stats = gather()
    except ConvStoreUnavailable as exc:
        print(f"\n  ✗ {exc}")
        detail = _status_detail()
        if detail:
            # ⚠ psycopg 的连接错误原文是**多行**的（每个 hostaddr 一行）。不缩进续行的话
            #   后面几行会顶到第 0 列，看起来像是看板之外的东西打出来的。
            #   缩进宽度 = `    诊断: ` 的显示宽度（4 + 4 + 2）。
            print(f"    诊断: {detail.replace(chr(10), chr(10) + ' ' * 10)}")
        print()
        return 1

    print()
    print(render(stats, _cache_stats()))
    print()
    return 0


# ---------------------------------------------------------------- 离线自检
_pass = 0
_fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    """与 `scripts/probe_*.py` / `eval_rag.py:check` **同一套打印格式**。"""
    global _pass, _fail
    if ok:
        _pass += 1
        print(f"  [OK  ] {label}")
    else:
        _fail += 1
        print(f"  [FAIL] {label}")
        if detail:
            print(f"         {detail}")


def _colon_cols(text: str) -> set[int]:
    """每行第一个 `:` 的**显示宽度列**（0-based）。用来断言对齐。"""
    cols: set[int] = set()
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        head = raw[: raw.index(":")]
        cols.add(_dwidth(head))
    return cols


def _render_capture(stats: dict[str, int], cache: Any = None) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print(render(stats, cache))
    return buf.getvalue()


_FULL = {"conversations": 3, "messages": 12, "feedback": 3, "up": 3, "down": 0}
_EMPTY_FB = {"conversations": 2, "messages": 10, "feedback": 0, "up": 0, "down": 0}


def selftest() -> int:
    """离线断言，**不需要任何服务 / Key / 数据库**。"""
    global _pass, _fail
    _pass = 0
    _fail = 0

    # ① 规格形状：横幅宽度 + 六个标签逐字
    text = _render_capture(_FULL)
    lines = text.splitlines()
    check(
        "① 横幅 50 宽（用户规格里那条，与 agent_eval 的宽度不同，别互相抄）",
        lines[0] == "=" * 50 and lines[-1] == "=" * 50,
        f"首行 {len(lines[0])!r}",
    )
    check("①b 标题行", lines[1] == "  Agent 运行看板", lines[1])
    for label in ("总对话数", "用户反馈", "满意", "不满意", "检索缓存"):
        check(f"①c 标签 {label} 在", label in text)

    # ② 冒号对齐 —— ⚠ 用户示例里 `检索缓存:` 那行没对齐（手打漏的），这里必须齐
    cols = _colon_cols(text)
    check("② 全部 5 行的冒号在同一列", len(cols) == 1, f"实际列: {sorted(cols)}")

    # ③ 比例数学 —— ⚠ 含 0/0，那是**当前库的真实情形**，不能炸
    check("③ 3/3 = 100%", _pct(3, 3) == "100%", _pct(3, 3))
    check("③b 0/3 = 0%", _pct(0, 3) == "0%", _pct(0, 3))
    check("③c 1/3 取整到 33%", _pct(1, 3) == "33%", _pct(1, 3))
    check("③d 2/3 取整到 67%", _pct(2, 3) == "67%", _pct(2, 3))
    check("③e 0/0 不除零，回 —", _pct(0, 0) == "—", _pct(0, 0))

    # ④ 零反馈：当前库就是这副样子，所以这是**主路径**不是边角
    t = _render_capture(_EMPTY_FB)
    # ⚠ 断言用 `_line` 现算，**别手打常数**：填充宽度是显示宽度算出来的
    #   （「用户反馈」= 8 列 ⇒ 6 个空格），手数空格必错。
    check("④ 零反馈时写 `0 条`", _line("用户反馈", "0 条") in t, t)
    check("④b 零反馈时两行都是 `(—)`", t.count("(—)") == 2, f"出现 {t.count('(—)')} 次")
    check(
        "④c⚠ 零反馈时**绝不出现 0%**（`0%` 断言了一个从没测过的比率）",
        "0%" not in t,
        t,
    )

    # ⑤ 检索缓存行：仓库里没有这个数据源，必须如实报「暂无数据」
    check("⑤ 缓存缺失时打 (暂无数据)", _cache_line(None) == _EMPTY, _cache_line(None))
    check(
        "⑤b 接上缓存后走另一支（今日到不了，用替身钉形状）",
        _cache_line({"hits": 7, "misses": 3}) == "命中 7 / 未命中 3",
        _cache_line({"hits": 7, "misses": 3}),
    )
    check("⑤c 报表里那一行也是暂无数据", _line("检索缓存", _EMPTY) in t, t)

    # ⑥ 降级：连不上时打一行原因、**不 traceback**、退出码 1
    real_gather = globals()["gather"]
    globals()["gather"] = lambda: (_ for _ in ()).throw(
        ConvStoreUnavailable(_STATS_UNAVAILABLE_MSG)
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = main()
    finally:
        globals()["gather"] = real_gather
    out = buf.getvalue()
    check("⑥ 连不上时退出码 1", code == 1, str(code))
    check("⑥b 打出的是自己那句文案，不是 traceback", _STATS_UNAVAILABLE_MSG in out, out)
    check("⑥c 没有 Traceback 漏到用户眼前", "Traceback" not in out, out)

    # ⑦ 取数形状：看板只调 conv_store，表结构归它
    # ⚠⚠ **必须只扫实现段**（`def selftest(` 之前）。扫全文的话，下面这条断言里的
    #    needle 字面量**自己就在 `src` 里**，`"SELECT" not in src` 恒为假 ——
    #    一个**永远失败**的断言，而失败原因是它自己。（第一版就是这么写的。）
    src = Path(__file__).with_name("dashboard.py").read_text(encoding="utf-8")
    impl = src.split("def selftest(")[0]
    check(
        "⑦ 看板自己**不写 SQL**（表结构属于 conv_store）",
        "SELECT" not in impl and "usage_stats" in impl,
        f"实现段 {len(impl)} 字符",
    )
    check(
        "⑦b 计数字段名与 conv_store.usage_stats 的键一致",
        set(conv_store._STAT_KEYS) == {"conversations", "messages", "feedback", "up", "down"},
        str(conv_store._STAT_KEYS),
    )
    check(
        "⑦c usage_stats 带 msg 默认值（现有调用点不用改）",
        "msg" in conv_store.usage_stats.__code__.co_varnames,
        str(conv_store.usage_stats.__code__.co_varnames),
    )

    # ⑧ 纯函数：render 不改入参、同输入同输出
    snapshot = dict(_FULL)
    first = render(_FULL, None)
    check("⑧ render 不改入参", _FULL == snapshot, str(_FULL))
    check("⑧b render 幂等", render(_FULL, None) == first)
    check("⑧c 缺键不炸（`.get` 兜底，全是 0）", _line("总对话数", "0") in render({}), render({}))

    print()
    print("=" * 60)
    print(f"  selftest: {_pass} 通过, {_fail} 失败")
    print("=" * 60)
    return 1 if _fail else 0


def main_cli() -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard.py",
        description="Agent 运行看板：读 PostgreSQL 的对话/反馈总账（不碰 Qdrant，不用停后端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--selftest", action="store_true", help="离线断言，不需要任何服务/Key")
    opts = parser.parse_args()
    if opts.selftest:
        return selftest()
    return main()


if __name__ == "__main__":
    raise SystemExit(main_cli())
