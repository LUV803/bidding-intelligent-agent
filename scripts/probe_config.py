"""验证 `config.py` 的**启动配置体检**（`Settings` 上的 `model_validator`）。

**全离线**：不连任何服务、不发一条网络请求、不读大模型。

⚠⚠ 本文件钉的第一性质是「**只念不拦**」：校验器**绝不 raise**。五个可选能力全靠
「没配就降级」活着（`graph_store.ENABLED` / `pg_store.ENABLED` / `web_search.ENABLED` /
`web_search_exa.ENABLED` / `vision.available` 全是 `bool(settings.X.strip())`），而
`.env.example` 交上去时这几个 Key **本来就是空的** —— 在那里 raise 等于「谁都没配 Key
就起不来服务」，把「可选」变成「必需」。
② 用「所有可选能力的 Key 全空也照样构造得出来」当场钉着它。

⚠ 第二性质是「**只在构造时跑一次**」（`mode="after"`）：`scripts/probe_llm_provider.py`
与 `probe_rate_limit.py` 都靠在运行期给 `settings` 属性赋值来改行为
（`settings.rate_limit_per_minute = 3`）。若哪天有人把校验器改成会在赋值时重跑，
那些探针会**集体**变成噪声甚至假红。⑤ 钉着这条。

⚠ 顺带纠正提案里的一个前提：**本项目从来没有 `settings.validate()`**。`Settings.validate`
是 pydantic 的 `BaseModel.validate(value)` —— 校验**单个值**的 classmethod，与配置完整性无关
（① 里有一条断言把这个误会钉在案）。所以「缺失项」在本仓库原先**没有**任何集中检查点，
`_config_findings()` 就是补上的那一个。

跑法：

    uv run python scripts/probe_config.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa.config import (  # noqa: E402
    PROJECT_ROOT,
    Settings,
    _OPTIONAL_CAPS,
    _TEXT_LLM_SOURCES,
    _config_findings,
    _undeclared_env_keys,
    settings,
)

failures: list[str] = []
count = 0

#: 可选能力的开关字段（当前五个）—— 体检就是围绕它们展开的
_CAP_FIELDS = [field for _label, field, _why in _OPTIONAL_CAPS]
#: 一次抹掉这几个就能造出「什么都没配」的 Settings（init kwargs 优先级高于 .env）
_ALL_BLANK = {field: "" for field in _CAP_FIELDS}

#: 「文本大模型有哪些路可走」—— 体检用它判断会不会退化成 fallback_answer。
#: ⚠ 从 config.py 的 `_TEXT_LLM_SOURCES` 取，**不在探针里抄第二份**：抄一份的话
#:   加第五家供应商时这里不会红，于是「抹掉全部四路」的用例会**静默漏抹**那一路，
#:   断言照样绿、覆盖却没了（本仓库「探针的假名约定」记过同一类静默降级）。
_TEXT_FIELDS = list(_TEXT_LLM_SOURCES)
#: 四路全抹空。⚠ 必须**显式**写全，见 `probe_findings()` 里那段注释。
_ALL_TEXT_BLANK = {field: "" for field in _TEXT_FIELDS}


class _Capture(logging.Handler):
    """把体检那几行 WARNING 收下来 —— 探针要既验「说了什么」也验「有没有真的说」。

    ⚠ 挂了它之后 `logging.lastResort` 就不再兜底打印了（logger 链上找得到 handler），
    所以探针自己的输出是干净的。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


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


def _build(**overrides: str) -> tuple[Settings, _Capture]:
    """构造一个 Settings，并把体检**当场念出来的**那几行收下来一起返回。

    ⚠ init kwargs 的优先级**高于 .env**（pydantic-settings 的既定次序：init > env > dotenv
    > 默认值），所以 `_build(neo4j_uri="")` 得到的是「一个没配图谱的本机」——
    真实 `.env` 一个字都不用动。探针能验「关掉某项」，全靠这一条。
    """
    cap = _Capture()
    cfg_logger = logging.getLogger("bidding_qa.config")
    cfg_logger.addHandler(cap)
    try:
        s = Settings(**overrides)
    finally:
        cfg_logger.removeHandler(cap)
    return s, cap


# ================================================================ ① 接线
def probe_wiring() -> None:
    banner("① 接线：校验器挂上了、字段表对得上、真实 .env 没有未声明的键")

    hooks = Settings.__pydantic_decorators__.model_validators
    check("⭐ Settings 上挂着 model_validator", len(hooks), 1, f"实到 {list(hooks)}")

    # ⚠ `mode="after"` 是刻意的：这时**所有字段都已填好**，体检才看得到值。
    #   `mode="before"` 拿到的是原始输入 dict，得自己重算一遍默认值与 .env 合并 —— 那是白费。
    # ⚠ 判据读的是 pydantic 的装饰器元信息（`_pydantic_decorators`），不是源码文本。
    hook = hooks.get("_report_startup_config")
    check("⭐ 且是 mode='after'（能直接读字段值，不用重算一遍默认值合并）",
          getattr(getattr(hook, "info", None), "mode", None), "after")

    # ⚠ 字段表与体检表**必须逐字对得上**：改名/删字段而漏改 _OPTIONAL_CAPS 的话，
    #   `getattr(s, field, "")` 会静默拿到 ""（哨兵），于是一个**开着**的能力被报成「未启用」。
    #   那是「看起来完全合理的错误答案」，正是本仓库最警惕的一类。
    missing = [f for f in _CAP_FIELDS if f not in Settings.model_fields]
    check("⭐ _OPTIONAL_CAPS 里每个字段都在 Settings.model_fields 里（漏了会静默误报）",
          missing, [])
    # ⚠ 这个数字是**手工维护**的：给 `_OPTIONAL_CAPS` 加/删一行就要跟着改。
    #   它的作用是抓「在 Settings 里加了个可选能力字段、却忘了加这张表的行」——
    #   那种漏改的表现是那个能力**永远不被点名**（开关是空的，用户以为配好了）。
    check("   且五个能力各占一行（不多不少）", len(_CAP_FIELDS), 5)

    # ⚠⚠ 顺带把提案里那个前提钉在案：`Settings.validate` **不是**配置检查函数。
    #   它是 pydantic 的 `BaseModel.validate(value)`，只校验**单个值**，且要求传参。
    #   写在这里是为了让下一个读到「缺失项仅在 settings.validate() 中检查」的人少找一圈。
    import inspect

    params = list(inspect.signature(Settings.validate).parameters)
    check("   注：Settings.validate 是 pydantic 的单值校验器（不是配置体检）",
          params, ["value"], "本仓库从来没有一个叫 validate() 的配置检查函数")

    check("⭐ 真实 .env 里没有未声明的键（有的话下面会逐个点名）",
          _undeclared_env_keys(), [])


# ================================================================ ② 只念不拦
def probe_never_raises() -> None:
    banner("② ⭐⭐ 只念不拦：所有可选能力的 Key 全空也照样构造得出来（降级就靠这个）")

    # ⚠ 只补 `deepseek_api_key`：`zhipu_api_key` **已经在 `_ALL_BLANK` 里**了
    #   （它同时是「图片解析」的开关）—— 重复传会被 Python 当成 duplicate kwarg 抛 TypeError，
    #   而那看起来**很像**「体检把构造搞崩了」。第一版就是这么写的，被这条断言当场逮住。
    try:
        s, cap = _build(**_ALL_BLANK, deepseek_api_key="")
    except Exception as exc:  # noqa: BLE001
        check("⭐⭐ 全空构造 Settings **不抛**异常", f"{type(exc).__name__}: {exc}", "（不抛）")
        return

    check("⭐⭐ 全空构造 Settings **不抛**异常（.env.example 交上去就是这个状态）",
          isinstance(s, Settings), True)
    check("   而且体检确实说话了（不是默默吞掉）", len(cap.messages) >= 1, True,
          f"实际 {len(cap.messages)} 行")
    # ⚠ 级别是 WARNING 不是 INFO：config.py 是 **import 期**跑的，那时 `api.py:49` 的
    #   `logging.basicConfig` 还没执行 —— 只有 WARNING 能经 `logging.lastResort` 落到
    #   stderr，INFO 会被直接丢弃，体检就白做了。别为了「可选能力关掉是正常状态」把它降到 INFO。
    _s2, cap2 = _build(**_ALL_BLANK)
    check("⭐ 级别是 WARNING（降成 INFO 会在 basicConfig 之前被丢掉 = 体检白做）",
          sorted({r.levelno for r in cap2.records}), [logging.WARNING])

    # ⚠ 反过来说：**真的配齐了就不该有任何输出**。静默的机器上刷一屏 WARNING 等于把
    #   体检变成噪声，而噪声里的真警报是没人看的。
    _s, quiet = _build(**_ALL_BLANK)  # 只抹可选能力，两个大模型 Key 留着
    check("   配齐时一行都不多说（只抹掉可选能力、Key 保留）", len(quiet.messages), 1,
          "只该剩「未启用的可选能力」那一句")


def _cap_hits(lines: list[str], label: str) -> list[str]:
    """体检输出里点名了 `label` 的行 —— **标签整体**匹配，不是子串。

    ⚠⚠ 别退回 `label in ln`：`_config_findings()` 把每项拼成 `{label}：{why}`、项与项
    之间用 `；` 连起来，所以**一个标签可以是另一个的后半截** —— 「联网搜索」正是
    「Exa 联网搜索」的后半截。2026-09-30 加 Exa 时当场踩到：关掉 Exa 之后，
    「开着『联网搜索』时它不出现」与「真实配置上」两条断言一起**假红**
    （那会儿 Tavily 好好配着，匹配到的其实是 Exa 那一项）。实测过 `label in ln` 在
    这一格返回 True 而正确判据返回 False —— 假红会把真问题淹掉，所以判据收紧成
    「开头的 `：` 或项间的 `；` 之后紧跟 `{label}：`」，即标签必须**整体**出现。
    ⚠ 末尾那个 `：` 也必需：没有它，标签「联网」会命中「联网搜索」。
    ⚠ 收紧之后**仍然咬得住真 bug**（实测：Exa 真关着时反向断言照样红），不是橡皮图章。
    """
    head, mid = f"：{label}：", f"；{label}："
    return [ln for ln in lines if head in ln or mid in ln]


# ================================================================ ③ 体检内容
def probe_findings() -> None:
    banner("③ 体检内容：每个能力「关了就点名、开着就不提」，逐个单独验")

    # ⚠ 逐个抹、逐个验 —— 一次性全抹只能证明「有话说」，证不了「话说得对」。
    for label, field, _why in _OPTIONAL_CAPS:
        s, _ = _build(**{field: ""})
        check(f"   只关「{label}」时点名了它", len(_cap_hits(_config_findings(s), label)), 1)

        # 反向：留着它时**不能**出现在体检里（否则就是误报）。
        # ⚠⚠ 这一格必须是「**其他几个都关着**、只有它开着」：全开的话体检整行都不出现，
        #   断言会**空过**，而标签串台那类 bug 恰恰只在这一格才看得见（见 `_cap_hits`）。
        # ⚠⚠ 它的值**显式给一个非空串**，不能指望真实 `.env` 里恰好配了它 ——
        #   本机 `.env` 的 `EXA_API_KEY=` 就是空的，靠 .env 的话这一条会在
        #   「那个能力恰好没配」的机器上假红，而报出来的原因指不到真因
        #   （与 `_ALL_TEXT_BLANK` 那条注释是同一类坑）。
        s2, _ = _build(**{f: "" for f in _CAP_FIELDS if f != field}, **{field: "x"})
        check(f"   开着「{label}」时它**不**出现",
              _cap_hits(_config_findings(s2), label), [])

    # ---- 文本大模型：`_TEXT_LLM_SOURCES` 四路的**析取**（全空才报）----
    # ⚠ 不按 provider→key 映射判：那个映射在 clients/llm_factory.py，config.py
    #   反向 import 会成环，照抄一份则是「加第三家要改四处」。析取是这里能拿到的最强信号。
    # ⚠⚠ 每个用例都必须**显式抹掉全部四路**（`_ALL_TEXT_BLANK`），不能只抹手上那两个：
    #   没被 override 的字段会从真实 `.env` 取值 —— 这台机器上配了 `VLLM_BASE_URL`
    #   之后，`_build(deepseek_api_key="", zhipu_api_key="")` 就不再是「全空」，
    #   于是这条断言会在**已经配好本地推理的机器上**变红，而报出来的原因完全指不到真因。
    s, _ = _build(**_ALL_TEXT_BLANK)
    check("⭐ 四路全空 => 单独点名「会退化成 fallback_answer」",
          len([ln for ln in _config_findings(s) if "fallback_answer" in ln]), 1)
    # ⚠⚠ 点名的话术必须把四路都列上：只写两个 Key 的话，配了 vLLM 的人读到这句会
    #   以为自己「没配任何大模型」—— 实际他配了，是体检的话说错了。
    _hint = next((ln for ln in _config_findings(s) if "fallback_answer" in ln), "")
    _missing_from_hint = [f for f in _TEXT_FIELDS if f.upper() not in _hint]
    check("   且那句话里四个键名逐个都点到（漏一个就是指着配好的人说他没配）",
          _missing_from_hint, [])

    for _field, _value in (
        ("deepseek_api_key", "sk-x"),
        ("zhipu_api_key", "k"),
        ("vllm_base_url", "http://127.0.0.1:8001/v1"),
        ("ollama_base_url", "http://127.0.0.1:11434/v1"),
    ):
        _s, _ = _build(**{**_ALL_TEXT_BLANK, _field: _value})
        check(f"   只留 {_field} => 不报（有任一路能用就够了）",
              [ln for ln in _config_findings(_s) if "fallback_answer" in ln], [])

    # ---- 真实 .env 上跑一遍：体检结果必须与字段现状**逐字一致**----
    real = _config_findings(settings)
    expected_off = [label for label, field, _why in _OPTIONAL_CAPS
                    if not getattr(settings, field, "").strip()]
    got_off = [label for label, _f, _w in _OPTIONAL_CAPS if _cap_hits(real, label)]
    check("⭐ 真实配置上：体检点名的那几个 == 字段真为空的那几个",
          sorted(got_off), sorted(expected_off))
    note(f"（当前 .env 下体检输出 {len(real)} 行；空 = 一切正常）")


# ================================================================ ④ .env 未声明键
def probe_undeclared() -> None:
    banner("④ ⭐ .env 未声明键：`extra=\"ignore\"` 静默丢弃（本仓库踩过四次）的探针")

    tmp = PROJECT_ROOT / "_probe_config_fake.env"
    tmp.write_text(
        "# 注释行不该被当成键\n"
        "\n"
        "NEO4J_URI=bolt://localhost:7687\n"     # 已声明
        "DEEPSEEK_MODEL=deepseek-v4-pro\n"      # 已声明（大小写不敏感）
        "  SPACED_KEY = 1\n"                    # 未声明 + 前后空格
        "OLD_AGENT_MAX_ROUNDS=5\n"              # 未声明（真·历史遗留）
        "export EXPORTED_KEY=2\n"               # 未声明 + export 前缀
        "NO_EQUALS_LINE\n"                      # 不是赋值行，忽略
        "# ANOTHER_COMMENT=1\n",                # 注释掉的赋值，忽略
        encoding="utf-8",
    )
    try:
        got = _undeclared_env_keys(tmp)
        check("⭐⭐ 只点名未声明的键，已声明的/注释/空行/非赋值行都不误报",
              got, ["SPACED_KEY", "OLD_AGENT_MAX_ROUNDS", "EXPORTED_KEY"])
    finally:
        tmp.unlink(missing_ok=True)

    check("   文件不存在时返回空列表（不抛）",
          _undeclared_env_keys(PROJECT_ROOT / "_probe_config_nope.env"), [])

    # ⚠ 真实 .env 由 ① 钉着。这一条额外把「键名大小写」验一下：pydantic-settings 是
    #   case_sensitive=False，所以小写写进 .env 也算声明过，不该误报。
    tmp2 = PROJECT_ROOT / "_probe_config_lower.env"
    tmp2.write_text("neo4j_uri=x\n", encoding="utf-8")
    try:
        check("   小写写法的已声明键不算未声明（case_sensitive=False）",
              _undeclared_env_keys(tmp2), [])
    finally:
        tmp2.unlink(missing_ok=True)


# ================================================================ ⑤ 不重跑
def probe_runs_once() -> None:
    banner("⑤ ⭐⭐ 只在构造时跑一次：运行期给 settings 赋值**不**触发体检")

    # ⚠⚠ 这条不是洁癖：probe_llm_provider.py / probe_rate_limit.py 都靠在运行期
    #   给 settings 属性赋值来改行为（`settings.rate_limit_per_minute = 3`）。
    #   校验器若在赋值时重跑，那些探针会集体噪起来，且「改完就生效」这件事
    #   会变成「改完顺手还打一屏日志」—— 骚扰所有调用方。
    cap = _Capture()
    cfg_logger = logging.getLogger("bidding_qa.config")
    cfg_logger.addHandler(cap)
    saved = settings.neo4j_uri
    try:
        settings.neo4j_uri = ""                       # 制造一个「本该被点名」的状态
        after_first = len(cap.messages)
        settings.neo4j_uri = saved                    # 再改回来
        check("⭐⭐ 运行期 setattr 一次体检都没触发（mode='after' 只在构造时跑）",
              after_first, 0)
        check("   改完之后值确实变了（赋值本身是生效的，只是不体检）",
              settings.neo4j_uri, saved)
    finally:
        settings.neo4j_uri = saved
        cfg_logger.removeHandler(cap)

    # 反向对照：**构造**一个新实例时它一定会说话 —— 证明上一条不是「整个机制早就坏了」。
    _s, cap2 = _build(neo4j_uri="")
    check("   对照：构造新实例时它照样说话（说明上一条不是机制整个失效）",
          len(cap2.messages) >= 1, True)


# ================================================================ main
def main() -> int:
    print(f"PROJECT_ROOT = {PROJECT_ROOT}")
    print(f"体检对象 = {_CAP_FIELDS}")
    print("⚠ 探针里构造 Settings 时，体检那几行会被收进 _Capture 不打印到屏幕上")

    probe_wiring()
    probe_never_raises()
    probe_findings()
    probe_undeclared()
    probe_runs_once()

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
