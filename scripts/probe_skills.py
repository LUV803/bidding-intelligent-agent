"""验证**技能机制**（`skill_store.py` + `load_skill` 工具 + `rag.py` 的提示词片段）。

**全离线**：不连任何服务、不发一条网络请求、不读大模型。改 `skill_store.py` /
`tools/rag_tools.py` 的 `LoadSkillTool` / `rag.py` 的 `_AGENT_SKILL_RULES` 之后都该跑一次。

⚠ `load_skill` 有三个 MCP 路由分支之一，所以 `main()` **开场就把 `settings.mcp_server_url`
强制置空**（`finally` 还原）—— 否则环境里配了 `MCP_SERVER_URL` 时这句「全离线」就是假的。
理由与那一整处注释见 `main()`。（路由本身由 `probe_mcp_server.py` 的 ⑥ 专测。）

⚠⚠ 本文件钉的**最重要**的一条是 ③：`assess_outcome("load_skill", 成功outcome)` **必须是
`None`**。基类 `BaseTool.empty_reason()` 的默认判据读的是 `new_sources` / `known_indices` /
`dropped`，而技能加载**这三者恒空**（它不产生任何「来源」）—— 不覆写的话，模型**刚读完一份
说明书的下一行**就会读到「本次调用没有拿到任何结果…若都不适用，就如实告诉用户『没有查到』」。
那是**没有任何日志**的静默故障，而且它跟工具回传正文直接打架。

⚠ 该组用例的写法值得照抄：**先断言「不覆写的话确实会坏」**（拿 `BaseTool.empty_reason`
在同一个 outcome 上跑一遍，必须非 `None`），**再断言覆写后是 `None`**。只测后者的话，
哪天判据被改成「恒返回 None」那条也会绿 —— 测的是另一回事。（同 `probe_conv_store.py` ⑫
「先证明钳制真的在动，再证明计数不受影响」。）

跑法：

    uv run python scripts/probe_skills.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import rag, skill_store  # noqa: E402
from bidding_qa.clients.base_client import ToolCall  # noqa: E402
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.tools import base as base  # noqa: E402
from bidding_qa.tools import rag_tools as R  # noqa: E402

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
    if not ok:
        print(f"      got={got!r} want={want!r}")
    if detail:
        print(f"      {detail}")
    if not ok:
        failures.append(label)


def note(text: str) -> None:
    print(f"      {text}")


def _ctx() -> base.ToolContext:
    # ⚠ retriever=None：技能加载是纯文件读，不碰检索器（所以本探针不需要 Qdrant，
    #   后端在跑也能跑）。传 None 是安全的 —— 唯一读它的工具是 KnowledgeBaseTool。
    return base.ToolContext(
        registry=base.SourceRegistry(cap=20),
        retriever=None,  # type: ignore[arg-type]
        mode="hybrid",
        rerank=False,
        tool_names=R.TOOL_NAMES,
    )


async def _call(arguments: str) -> base.ToolOutcome:
    return await base.execute_tool(
        ToolCall(id="probe", name=R.TOOL_SKILL, arguments=arguments), _ctx()
    )


# ================================================================ ① 目录与解析
def probe_catalogue() -> None:
    banner("① 目录与解析：两个技能都进来了、名字取自目录名、frontmatter 被吃掉")

    skills = skill_store.catalogue()
    check("⭐ 目录里有技能（ENABLED 为真）", skill_store.ENABLED, True)
    check("  两个技能都在", tuple(s.name for s in skills),
          ("bid-analysis", "debug-answer"), f"实到 {len(skills)} 个")

    for s in skills:
        # ⚠⚠ 布局是 `<名字>/SKILL.md`，技能名取**父目录名** —— 所有文件都叫 `SKILL.md`，
        #   拿文件名区分不了（`path.stem` 对每个技能都会是 "SKILL"）。
        #   它同时是 `load_skill` 的参数值、注册表的键、前端展示的落点；
        #   frontmatter 的 `name:` 只是副本（不一致时记 WARNING，**以目录名为准**）。
        check(f"  {s.name}：文件落在 <名字>/SKILL.md",
              (s.path.parent.name, s.path.name), (s.name, "SKILL.md"))
        check(f"  {s.name}：description 非空", bool(s.description.strip()), True,
              f"len={len(s.description)}")
        # ⚠ 描述要拼进**每轮都在眼前**的工具 schema，含换行会把 JSON 描述撑成多行、
        #   也让目录渲染错位。frontmatter 是单行 `key: value`，所以这应当恒成立。
        check(f"  {s.name}：description 不含换行", "\n" in s.description, False)

        body, err = skill_store.load(s.name)
        check(f"  {s.name}：load() 成功", err, "")
        assert body is not None
        check(f"  {s.name}：正文非空", len(body) > 2000, True, f"实为 {len(body)} 字")
        # ⚠ frontmatter 必须**整段**剥掉：留着的话模型在正文开头又读一遍 YAML。
        check(f"  {s.name}：正文开头没有 frontmatter 残留", body.startswith("---"), False)
        check(f"  {s.name}：正文里没有 `name:` 那行", f"name: {s.name}" in body, False)

    # 手写解析的边界：只有半个 frontmatter、没有 `: ` 的行、值里带冒号
    meta, body = skill_store._parse_frontmatter("---\nname: a\ndescription: 甲：乙。\n---\n正文")
    check("  解析：值里的第二个冒号不被切掉", meta.get("description"), "甲：乙。")
    check("  解析：正文从收尾 --- 之后开始", body, "正文")
    meta, body = skill_store._parse_frontmatter("---\ndescription: 没有收尾\n正文")
    check("  解析：缺收尾 --- 时整段当正文（不抛）", (meta, body), ({}, "---\ndescription: 没有收尾\n正文"))
    meta, body = skill_store._parse_frontmatter("没有 frontmatter")
    check("  解析：没有 frontmatter 时原样返回", (meta, body), ({}, "没有 frontmatter"))
    # ⚠ 无关的键要忽略（`allowed-tools` 之类是 Claude Code 的，对本模块没意义）
    meta, _ = skill_store._parse_frontmatter("---\nallowed-tools: Read\nname: x\n---\nb")
    check("  解析：不认识的键不进 meta", tuple(meta), ("name",))


# ================================================================ ② 加文件即生效
def probe_autogrow() -> None:
    banner("② 丢一个技能目录就自动出现在模型眼前（本设计的头号承诺）")

    before = skill_store.tool_description()
    check("  当前目录里有 bid-analysis", "bid-analysis" in before, True)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # 先把现有两份技能照原样铺进临时目录（整目录对拷，与同步两份副本的做法一致）
        for name in skill_store.available_names():
            (tmpdir / name).mkdir()
            (tmpdir / name / "SKILL.md").write_text(
                skill_store.SKILLS_DIR.joinpath(name, "SKILL.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        # ⚠ 新技能：**只建目录 + 写文件，不改任何代码** —— 这正是要验的那件事
        (tmpdir / "brand-new").mkdir()
        (tmpdir / "brand-new" / "SKILL.md").write_text(
            "---\nname: brand-new\ndescription: 一个刚丢进来的技能。后面这句不该进目录。\n---\n正文",
            encoding="utf-8",
        )
        # ⚠ 扁平 `<名字>.md` 刻意**不认**：布局只有一种，免得日后有人在同一个目录里
        #   同时摆 `foo.md` 与 `foo/SKILL.md`，然后「谁赢」变成一个没人定的歧义。
        (tmpdir / "flat-one.md").write_text(
            "---\ndescription: 扁平布局，不该被收。\n---\n正文", encoding="utf-8"
        )
        saved_dir, saved_cache = skill_store.SKILLS_DIR, skill_store._CATALOGUE
        try:
            skill_store.SKILLS_DIR = tmpdir
            skill_store._CATALOGUE = None
            after = skill_store.tool_description()
            check("⭐ 新技能出现在目录里（零代码改动）", "brand-new" in after, True)
            check("⭐ 扁平 <名字>.md 不被收（布局只有 SKILL.md 一种）",
                  "flat-one" in after, False)
            # ⚠ 目录只取**描述首句**：完整描述动辄几百字，那是每轮都要付的上下文。
            check("  目录里只有描述首句，没搬整段",
                  "后面这句不该进目录" in after, False)
            body, err = skill_store.load("brand-new")
            check("  新技能也能加载", err == "" and body == "正文", True, f"{err!r} {body!r}")
        finally:
            skill_store.SKILLS_DIR, skill_store._CATALOGUE = saved_dir, saved_cache


# ================================================================ ③ 降级
def probe_degrade() -> None:
    banner("③ 降级：目录空 ⇒ ENABLED 假 ⇒ 工具不挂、提示词不提")

    # ⚠⚠ 与图谱/采购库/联网**同构**的不变量：`rag_tools.TOOL_SCHEMAS` 挂不挂这个工具，
    #   与 `rag._agent_system_prompt()` 提不提它，必须读**同一个** `ENABLED`。
    #   两者不一致就是「提示词让模型去调一个列表里没有的工具」（盐化工那个 bug 的形态）。
    check("⭐ TOOL_SCHEMAS 挂 load_skill ⟺ skill_store.ENABLED",
          R.TOOL_SKILL in R.TOOL_NAMES, skill_store.ENABLED)
    check("  提示词提 load_skill ⟺ TOOL_SCHEMAS 里有它",
          R.TOOL_SKILL in rag._agent_system_prompt(), R.TOOL_SKILL in R.TOOL_NAMES)

    saved_dir, saved_cache = skill_store.SKILLS_DIR, skill_store._CATALOGUE
    saved_enabled = skill_store.ENABLED
    on = rag._agent_system_prompt()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            skill_store.SKILLS_DIR = Path(tmp)  # 空目录
            skill_store._CATALOGUE = None
            check("  空目录 ⇒ catalogue() 为空", skill_store.catalogue(), ())
            check("  空目录 ⇒ available_names() 为空", skill_store.available_names(), ())
            check("  空目录 ⇒ ENABLED 为假", bool(skill_store.catalogue()), False)

            # ⚠ 目录不存在（不是空目录）也要安全 —— 正式安装下 `skills/` 没打进去就是这个形态
            skill_store.SKILLS_DIR = Path(tmp) / "不存在"
            skill_store._CATALOGUE = None
            check("  目录不存在 ⇒ 空表而不是抛异常", skill_store.catalogue(), ())

            # 提示词侧：把 ENABLED 置假，片段必须当场消失
            # ⚠ `_agent_system_prompt()` 是**调用期**读 `skill_store.ENABLED` 的
            #   （`rag` 里拿到的是模块对象，改属性它立刻看得见）；而 `rag_tools.TOOL_SCHEMAS`
            #   是 **import 期**算死的常量，这里改不动它 —— 两处分叉只在「运行期手改 ENABLED」
            #   时出现，生产路径上都是 import 期定死的同一个值，所以下面那条只断言提示词侧。
            skill_store.ENABLED = False
            prompt = rag._agent_system_prompt()
            check("⭐ ENABLED 假 ⇒ 提示词不提 load_skill", R.TOOL_SKILL in prompt, False)
            # ⭐ 逐字节：关掉技能段后剩下的部分**等于**开着的那个减去末尾那段，
            #   一个字符都不许动（probe_enum_route 的矩阵探针按同样的口径钉另两个工具）
            check("⭐ 关掉只是少了末尾那段，其余逐字节不变",
                  on, prompt + rag._AGENT_SKILL_RULES)

        # 错误文案在空目录下也要点得清
        bad, err = skill_store.load("bid-analysis")
        check("  空目录下 load() 报「可用技能：（无）」", "（无）" in err, True, err)
    finally:
        skill_store.SKILLS_DIR, skill_store._CATALOGUE = saved_dir, saved_cache
        skill_store.ENABLED = saved_enabled


# ================================================================ ④ ⚠⚠ 结果质量回归
def probe_quality_gate() -> None:
    banner("④ ⚠⚠ 回归主线：技能加载**不得**被判成「空手而归」")

    out = asyncio.run(_call(json.dumps({"skill": "bid-analysis"})))
    check("  成功加载 ok=True", out.ok, True)
    check("  正文已回传", len(out.text) > 2000, True, f"实为 {len(out.text)} 字")

    # 构造出「基类默认判据会踩中」的那个形状：三个信号全空
    check("  该 outcome 的三个来源信号确实全空（夹具有效）",
          (bool(out.new_sources), bool(out.known_indices), out.dropped), (False, False, 0))

    # ① 先证明**不覆写的话确实会坏** —— 拿基类默认实现跑同一个 outcome
    # ⚠ 少了这一步，「覆写后是 None」在基类判据被改成恒 None 时也会绿（测的是另一回事）。
    base_default = base.BaseTool.empty_reason(R.SKILL_TOOL, out)
    check("⭐ 基类默认判据在同 outcome 上**非空**（证明覆写不是多余的）",
          base_default is not None, True, f"基类给的是 {base_default!r}")

    # ② 再断言覆写后为空 ⇒ assess_outcome 不追加任何东西
    check("⭐ 覆写后 empty_reason 为 None", R.SKILL_TOOL.empty_reason(out), None)
    check("⭐ assess_outcome 不追加【结果质量】",
          base.assess_outcome(R.TOOL_SKILL, out, R.TOOL_NAMES), None)

    # ⚠ 覆写必须挂在**类**上（挂在实例上会在重启后消失，而 `_REGISTRY` 里的是单例，
    #   探针查 `__dict__` 才挡得住「顺手改成实例属性」那种改法）
    check("  empty_reason / peek_display 都是类上的覆写",
          ("empty_reason" in R.LoadSkillTool.__dict__, "peek_display" in R.LoadSkillTool.__dict__),
          (True, True))


# ================================================================ ⑤ 错误路径
def probe_errors() -> None:
    banner("⑤ 错误路径：空名 / 错名 ⇒ bad_arguments + 列出可用名字")

    names = skill_store.available_names()
    for label, arguments in (
        ("不存在的技能名", json.dumps({"skill": "nope"})),
        ("模型漏传 skill", "{}"),
        ("skill 是空串", json.dumps({"skill": ""})),
        ("skill 全是空格", json.dumps({"skill": "   "})),
    ):
        out = asyncio.run(_call(arguments))
        check(f"  {label} ⇒ ok=False", out.ok, False)
        # ⚠ 用现成的 `bad_arguments` 码而**不新造 `unknown_skill`**：它在
        #   `_SELF_CORRECTING_ERRORS` 里 ⇒ 自动不追加【结果质量】那段噪声，
        #   而那段会跟 text 里本来就有的「可用技能：…」这条**更重要的**修正线索打架。
        check(f"    error == 'bad_arguments'", out.error, "bad_arguments")
        check(f"    该码在 _SELF_CORRECTING_ERRORS 里（所以不会追加噪声）",
              out.error in base._SELF_CORRECTING_ERRORS, True)
        check(f"    text 里列出了全部可用名字",
              all(n in out.text for n in names), True, out.text.strip())
        check(f"    也不追加【结果质量】",
              base.assess_outcome(R.TOOL_SKILL, out, R.TOOL_NAMES), None)

    # 宽松比对：模型把它写成下划线/大小写，收下比白掉一轮强
    body, err = skill_store.load("Bid_Analysis")
    check("  宽松比对：Bid_Analysis ⇒ bid-analysis", err == "" and bool(body), True)
    out = asyncio.run(_call(json.dumps({"skill": "Bid_Analysis"})))
    # ⚠ 回传文案里回显的是**模型自己写的那个名字**（归一化只用于查表，不用于回话）。
    #   刻意不改成规范名：那要 `load()` 多返回一个值，而模型后续引不引用规范名
    #   对结果毫无影响 —— 换了名字反而让它以为自己写错了。
    check("  回传文案回显模型写的原名", "「Bid_Analysis」" in out.text, True)
    check("  且正文真的读出来了（证明归一化查表生效）", len(out.text) > 2000, True,
          f"实为 {len(out.text)} 字")

    # 非法参数形状走 parse_args 的兜底，不该抛
    out = asyncio.run(_call(json.dumps({"skill": {"嵌套": "对象"}})))
    check("  参数形状不对 ⇒ 也不抛，如实报错", out.ok, False)


# ================================================================ ⑥ schema 形状
def probe_schema() -> None:
    banner("⑥ schema 形状（照 probe_recover.py 用例 0 的纪律）")

    schema = [s for s in R.TOOL_SCHEMAS if s["function"]["name"] == R.TOOL_SKILL]
    check("  load_skill 在 TOOL_SCHEMAS 里", len(schema), 1)
    fn = schema[0]["function"]
    check("  name 逐字", fn["name"], R.TOOL_SKILL)
    check("  required == ['skill']", fn["parameters"]["required"], ["skill"])
    check("  description 非空", len(fn["description"]) > 50, True, f"{len(fn['description'])} 字")

    props = fn["parameters"]["properties"]
    check("  只有 skill 一个参数", tuple(props), ("skill",))

    def walk(node: object, hits: list[str], path: str) -> None:
        if isinstance(node, dict):
            if "title" in node:
                hits.append(path)  # Pydantic 把 top_k 转成英文标题，纯噪声
            for k, v in node.items():
                walk(v, hits, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, hits, f"{path}[{i}]")

    titles: list[str] = []
    walk(schema[0], titles, "schema")
    # ⚠ 清洗层（base._clean_property）必须剥掉 title。漏了的话模型会看到一串
    #   "Skill" / "Top K" 这类英文标题，白占描述预算、还可能与中文描述打架。
    check("  ⭐ 全表没有 title", titles, [])

    sk = props["skill"]
    # ⚠ 顶层必须有 type：`str | None` 在 Pydantic 里产出 anyOf，清洗层要把**非 null 的
    #   那个分支提回顶层**。`.get("type")` 落空会让 `_normalize_args` 走 else 分支
    #   把原值原样收下（`to_openai_schema` 那条静默降级的坑）。
    check("  skill 顶层有 type", sk.get("type"), "string")
    check("  skill 没有残留 anyOf", "anyOf" in sk, False)
    check("  description 逐字保留", sk["description"].startswith("技能名"), True)


# ================================================================ ⑦ 提示词接线
def probe_prompt() -> None:
    banner("⑦ 提示词接线：只在 tools_ok 时出现，且不碰另两段（逐字节）")

    on, off = rag._agent_system_prompt(True), rag._agent_system_prompt(False)
    check("  tools_ok=True  ⇒ 有技能段", R.TOOL_SKILL in on, True)
    # ⚠ `tools_ok=False` 是「本轮一个工具都没挂」（后端不支持工具）。此时提它就是让模型
    #   去调一个不存在的东西 —— 见 probe_enum_route.py ③b 那段注释（盐化工那次就是这么翻的）。
    check("  ⭐ tools_ok=False ⇒ 没有技能段", R.TOOL_SKILL in off, False)
    check("  技能段拼在**末尾**", on.endswith(rag._AGENT_SKILL_RULES), True)
    # ⚠⚠ 这里**不能**写成 `on == off + _AGENT_SKILL_RULES`：`tools_ok=False` 连图谱/采购库
    #   那两段也一起摘了（它们各自还 `and graph_ok` / `and pg_ok`），所以 `off` 本来就比 `on`
    #   少三段，不是少一段。（第一版就是这么写的，当场红 —— 它测的是「工具全关时的形状」，
    #   不是「技能段加在哪」。）
    # ⭐ 真正该钉的是：**`tools_ok=False` 的全文一字不差地是前者的前缀**，而技能段只在
    #   最末尾。两条合起来 = 「既有每一段原地未动 + 技能段是追加的」。
    #   位置写错（比如插到图片规则前面）或改到既有句子，这两条都会红。
    check("⭐ tools_ok=False 的全文是前者的前缀（既有段落一字未动）",
          on.startswith(off), True)
    check("⭐ 且技能段只在最末尾", on.endswith(rag._AGENT_SKILL_RULES), True)

    # ⚠⚠ 另两段**一个字都不能碰**：`probe_enum_route.py:probe_tool_matrix()` 有一条断言是
    #   「两个可选工具都关掉时，后两者必须逐字节等于那两个 SEARCH_ONLY 常量」。
    check("⭐ _no_result_prompt(False) 逐字节 == SEARCH_ONLY",
          rag._no_result_prompt(False), rag._AGENT_NO_RESULT_SEARCH_ONLY)
    check("⭐ _sources_reminder(False) 逐字节 == SEARCH_ONLY",
          rag._sources_reminder(False), rag._SOURCES_REMINDER_SEARCH_ONLY)
    check("  这两段在 tools_ok=True 时也**不提**技能（避免与工具路由打架）",
          (R.TOOL_SKILL in rag._no_result_prompt(True), R.TOOL_SKILL in rag._sources_reminder(True)),
          (False, False))

    # ⚠ 文案里**一个技能名都不能有**：目录由工具描述在 import 期从磁盘生成，
    #   在这里抄一遍就是两处会漂移的真值（加个 .md 后提示词还停在旧名单上）。
    leaked = [n for n in skill_store.available_names() if n in rag._AGENT_SKILL_RULES]
    check("  ⭐ 提示词片段里不列技能名（名字只在工具描述里）", leaked, [])

    # 路由短标签要给模型看（空手而归时点名用），且别把 description 抄一遍
    check("  LoadSkillTool 配了 route_label", bool(R.SKILL_TOOL.route_label), True)
    check("  route_label 比 description 短得多",
          len(R.SKILL_TOOL.route_label) < len(R.SKILL_TOOL.description) / 3, True)


# ================================================================ ⑧ 展示
def probe_display() -> None:
    banner("⑧ peek_display：前端那格显示技能名，而不是一对空的「」")

    for raw, want in (
        (json.dumps({"skill": "bid-analysis"}), "bid-analysis"),
        (json.dumps({"skill": "  debug-answer  "}), "debug-answer"),
    ):
        check(f"  {raw} ⇒ {want!r}", base._peek_display(R.TOOL_SKILL, raw), want)

    # ⚠ 基类默认读 `query`（base.py:345 明写「每个主参数不叫 query 的工具都必须覆写它」）。
    #   不覆写的话前端每次都是空的「」—— 演示时看着像工具没被调用。
    check("  ⭐ 基类默认实现在本工具上给的是空串（证明覆写不是多余的）",
          base.BaseTool.peek_display(R.SKILL_TOOL, {"skill": "bid-analysis"}), "")
    # 参数不合法时不抛 —— 它跑在 tool_call 事件的推送路径上
    check("  参数不是合法 JSON ⇒ 空串不抛", base._peek_display(R.TOOL_SKILL, "不是 JSON"), "")
    check("  工具名不存在 ⇒ 空串不抛", base._peek_display("no_such_tool", "{}"), "")

    # ⚠ 前端 `newCount` 守卫按**工具名**判，漏一个就是「新增 0 条资料」这种
    #   看起来合理但完全错误的说法。这里只钉「名字在 TOOL_NAMES 里」，
    #   真正的守卫在 useChat.ts（前端侧，本探针够不着）。
    check("  工具名在 TOOL_NAMES 里（前端展示表的键）", R.TOOL_SKILL in R.TOOL_NAMES, True)


# ================================================================ main
def main() -> int:
    # ⚠⚠ 本文件在模块 docstring 里承诺「**全离线**：不连任何服务、不发一条网络请求」，
    #   而 ④ 组会 `execute_tool(..., name=TOOL_SKILL)` —— 那正是**三个带 MCP 路由分支
    #   的工具之一**。只要环境里（或 `.env` 里）有 `MCP_SERVER_URL`，那一步就会真去连一个
    #   HTTP 服务：承诺当场变成假话，而**输出上完全看不出来** —— 服务器没起时它如实报
    #   「连不上…」，与「技能文件读不到」在报告里长得一模一样。
    # ⇒ 强制置空，钉住「本探针测的是**进程内**那条路径」。`finally` 还原，无全局副作用。
    #   ⚠ 这不是在绕开路由 —— 路由本身由 `probe_mcp_server.py` 的 ⑥ 两组（关 / 开）专测。
    original_url = settings.mcp_server_url
    settings.mcp_server_url = ""
    try:
        print(f"SKILLS_DIR   = {skill_store.SKILLS_DIR}")
        print(f"技能         = {skill_store.available_names()}")
        print(f"TOOL_NAMES   = {R.TOOL_NAMES}")
        if original_url.strip():
            print(f"MCP_SERVER_URL = {original_url!r} ⇒ **临时置空**（见 main 里的注释）")

        probe_catalogue()
        probe_autogrow()
        probe_degrade()
        probe_quality_gate()
        probe_errors()
        probe_schema()
        probe_prompt()
        probe_display()
    finally:
        settings.mcp_server_url = original_url

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
