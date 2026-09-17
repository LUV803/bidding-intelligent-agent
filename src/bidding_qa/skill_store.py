"""技能说明书：把「固定流程」做成可按需加载的长文本。

存在的理由：这套系统有一堆**写死的领域知识**（同一个「空调」图谱/采购库给出三个不同的数、
图谱的 total 是下限不是总数、源数据只有 12 天快照所以做不了趋势…）。这些以前只写在
`CLAUDE.md` 和 `.claude/skills/` 里 —— 那是**给操作员看的**，线上 Agent 一个字也读不到。

做法是**渐进式披露**，这是本模块唯一的设计要点：

- **描述常驻**：`catalogue()` 里的 `description` 被拼进 `load_skill` 工具的 schema，
  每轮推理都在模型眼前（`build_agent_messages` 在循环外只构造一次，见 `agent.py:359-365`）。
- **正文按需**：每份约 9K 字符，只在模型判断相关时用 `load_skill` 拉进上下文。

⚠⚠ **定位用 `Path(__file__)`，绝不能用 `config.PROJECT_ROOT`**。后者是
`parents[2]`，只在「源码检出 + 可编辑安装」下才是仓库根目录；正式 `pip install` 之后
它是 `.venv/Lib`，整个技能目录会解析到不存在的地方（同 CLAUDE.md 记的
「`src/tools/` 非可编辑安装时整个消失」）。而 `skills/` 就在包**里面**，
所以它跟着 wheel 一起装出去，两种安装形态都成立。

⚠ 模块名**必须**是 `skill_store` 而不是 `skills` —— 本包内有 `skills/` 这个目录，
同名的话 `import bidding_qa.skills` 拿到的是**目录**（命名空间包），不是本模块。
命名对齐既有的 `graph_store` / `pg_store` / `conv_store`。

⚠ 技能文件在本仓库有**两份**：`.claude/skills/<name>/SKILL.md`（Claude Code 用的，
给操作员看）与 `src/bidding_qa/skills/<name>/SKILL.md`（线上 Agent 用的，本模块读的）。
两边**布局逐字同构**，所以同步就是**整个目录对拷**（`cp -r`），不用改名换形 ——
改其中一份时记得同步另一份。不写同步脚本是因为只有两个技能、复制一次的成本
远低于一个脚本的维护成本，但**漂移是静默的**（线上 Agent 会照着旧流程说）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 技能文件所在目录。⚠ 包内路径，跟着 wheel 走 —— 见模块 docstring 第一条。
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

#: 单份技能正文的字符上限。⚠ **这是防跑飞的安全阀，不是标定值** ——
#: 当前两份技能正文各约 9K **字符**（`wc -m` 在 Git Bash 里报的 17K 是 **UTF-8 字节数**，
#: 别拿那个数来估上下文），离上限远得很，所以调它不需要重标任何东西。
#: 真撞上时照 `clients/vision_client._clip` 的形状**带告知地**截断：
#: 静默截断会让模型以为它读完了整份说明书，然后漏掉后半段的规矩。
MAX_SKILL_CHARS = 24000

#: frontmatter 里认得的键。其余键忽略（`allowed-tools` 之类是 Claude Code 的，
#: 对本模块没有意义）。
_FRONTMATTER_KEYS = ("name", "description")


@dataclass(frozen=True)
class Skill:
    """一份技能说明书。``name`` 同时是 `load_skill` 的参数值。"""

    name: str  # = SKILL.md 的**父目录名**（见 `_scan`），不是文件名
    description: str
    path: Path  # <技能名>/SKILL.md


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出 frontmatter 与正文，返回 ``({键: 值}, 正文)``。

    ⚠ **刻意手写、不用 pyyaml**：frontmatter 只有 `key: value` 一种形状，而 pyyaml
    在本项目里只是某个依赖的传递依赖、**不在 `pyproject.toml` 的 dependencies 里** ——
    依赖一个「碰巧装了」的包，换台机器装不上时表现为整份技能静默消失。

    格式不合法时**不抛异常**：返回空 frontmatter + 整段原文当正文。降级而非报错
    （同 `_report_startup_config` 的纪律），代价只是那一条 `description` 是空的。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    for idx in range(1, len(lines)):
        if lines[idx].strip() != "---":
            continue
        meta: dict[str, str] = {}
        for line in lines[1:idx]:
            # ⚠ 只在**第一个**冒号处切：描述里本来就有冒号（"Use when 用户说…"），
            #   按所有冒号切会把值切成好几段。
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key = key.strip()
            if key in _FRONTMATTER_KEYS:
                meta[key] = value.strip()
        # 正文从关闭的 `---` 之后开始。**把 frontmatter 整段吃掉**——
        # 留着它的话，模型会在正文开头再读到一遍 YAML，白占上下文。
        return meta, "\n".join(lines[idx + 1:]).strip()

    # 只有开头那个 `---`、没有收尾的：当成没有 frontmatter
    return {}, text


def _scan(directory: Path) -> tuple[Skill, ...]:
    """扫一个目录，返回按名字排序的技能表。目录不存在时返回空元组。

    ⚠⚠ **布局是 `<技能名>/SKILL.md`，技能名取父目录名** —— 每个文件都叫 `SKILL.md`，
    拿文件名区分不了（`path.stem` 对全部技能都是 `"SKILL"`）。这个形状与
    `.claude/skills/` 完全一致，两个好处：① 两边同步是整目录对拷，不用改名；
    ② 一个技能将来要带附件（脚本、模板）时，有自己的目录可以放。

    **完全不认硬编码的技能名**：这里扫到什么就是什么，往目录里丢一份新的
    `<名字>/SKILL.md` 就自动生效，不需要改任何代码（这份"自动生效"由探针 ② 钉着）。
    """
    if not directory.is_dir():
        return ()

    found: list[Skill] = []
    for path in sorted(directory.glob("*/SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # 权限 / 编码 / 被占用
            logger.warning("技能文件读不出来，已跳过：%s（%s）", path.parent.name, exc)
            continue

        meta, _ = _parse_frontmatter(text)
        # ⚠ **目录名才是权威**：它同时是 `load_skill` 的参数值、注册表的键、
        #   前端展示的落点。frontmatter 里的 `name:` 不一致时记一条 WARNING，
        #   但**以目录名为准** —— 否则改个 YAML 字段就能让模型调的名字凭空变掉。
        name = path.parent.name
        if meta.get("name") and meta["name"] != name:
            logger.warning(
                "技能 %s 的 frontmatter name=%r 与目录名不一致，以目录名为准。",
                name, meta["name"],
            )
        found.append(Skill(name=name, description=meta.get("description", ""), path=path))
    return tuple(found)


#: 进程级缓存。⚠ 探针靠**重新赋值**它来验降级路径（`bidding_qa.skill_store._CATALOGUE = ()`），
#: 所以别把它藏进函数闭包里。
_CATALOGUE: tuple[Skill, ...] | None = None


def catalogue() -> tuple[Skill, ...]:
    """全部可用技能。首次调用扫盘，之后走缓存（进程内文件不会变）。"""
    global _CATALOGUE
    if _CATALOGUE is None:
        _CATALOGUE = _scan(SKILLS_DIR)
        if not _CATALOGUE:
            logger.info("技能目录 %s 下没有 <名字>/SKILL.md，技能能力关闭。", SKILLS_DIR)
    return _CATALOGUE


#: 技能能力的总开关。⚠ 与图谱/采购库/联网**同构**：为假时 `TOOL_SCHEMAS` 不挂这个工具、
#: `rag.py` 的提示词片段也不提它 —— 两处必须读同一个值，否则就是「提示词让模型去调一个
#: 列表里没有的工具」（`probe_enum_route.py` 的矩阵探针守着的正是这条）。
#:
#: ⚠ 它的判据是「**目录里有没有文件**」，不是某个 `Settings` 字段 —— 所以本能力
#: **不新增任何配置项**，也就绕开了本仓库记过四次的那个坑（`.env` / `.env.example` /
#: `config.py` 三处一起改，漏一处会被 `extra="ignore"` 静默丢弃）。
ENABLED: bool = bool(catalogue())


def available_names() -> tuple[str, ...]:
    return tuple(s.name for s in catalogue())


def _normalize(name: str) -> str:
    """宽松比对用：小写 + 下划线转连字符。

    模型把 `bid-analysis` 写成 `bid_analysis` 或 `Bid-Analysis` 都是可预料的，
    这里收下比白掉一轮强。⚠ **只在比对时用，回给模型/前端的永远是原名**
    （同 `graph_store` 的枚举值归一化：`entity_search` ↔ 它的反序写法）。
    """
    return name.strip().lower().replace("_", "-")


def load(name: str) -> tuple[str | None, str]:
    """按名字取一份技能的正文。返回 ``(正文, 错误文案)``，成功时错误文案为空串。

    ⚠ **不抛异常**：错误一律转成可读中文回给模型（`execute_tool` 那条
    「工具执行永不抛」的不变量）。找不到时**列出全部可用名字** ——
    照 `graph_store` 那句「没有名为 X 的查询类型。可用类型：…」的成例，
    修正线索本身就是最重要的信息。
    """
    raw = (name or "").strip()
    names = available_names()
    if not raw:
        return None, (
            "错误：skill 不能为空。请给出要加载的技能名。"
            f"可用技能：{'、'.join(names) if names else '（无）'}。"
        )

    for skill in catalogue():
        if skill.name == raw:
            return _read(skill), ""

    # 精确没中才放宽（见 `_normalize`）
    target = _normalize(raw)
    for skill in catalogue():
        if _normalize(skill.name) == target:
            return _read(skill), ""

    return None, (
        f"错误：没有名为 {raw} 的技能。"
        f"可用技能：{'、'.join(names) if names else '（无）'}。"
    )


def _read(skill: Skill) -> str:
    """读正文并去掉 frontmatter。读失败时回一句可读的中文，**不抛**。"""
    try:
        text = skill.path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("技能 %s 读取失败：%s", skill.name, exc)
        return f"错误：技能 {skill.name} 的文件读不出来，请改用已有资料作答。"

    _, body = _parse_frontmatter(text)
    if len(body) > MAX_SKILL_CHARS:
        # ⚠ 截断必须**带告知**（照 vision_client._clip）：静默截断会让模型以为
        #   它读完了整份说明书，然后漏掉后半段的规矩 —— 那比报错难查得多。
        return body[:MAX_SKILL_CHARS] + f"\n…（已截断，原文另有 {len(body) - MAX_SKILL_CHARS} 字）"
    return body


def tool_description() -> str:
    """`load_skill` 工具给模型看的描述 = 技能目录。

    ⚠⚠ **在 import 期由磁盘生成**（`rag_tools.LoadSkillTool.description`），
    所以往 `skills/` 里丢一个 `<名字>/SKILL.md` **不需要改任何代码**，
    下一轮推理就会看到它（探针 ② 钉着这条）。

    ⚠ 目录里只放名字与**描述首句**：完整描述动辄几百字，5 份就是一大段常驻上下文，
    而它每轮都要花钱。正文才是那份贵的东西，按需取。
    """
    items = catalogue()
    if not items:
        return "加载一份技能说明书。当前没有可用技能。"

    lines = ["把一份**技能说明书**读进当前上下文，然后按它的流程作答。", "", "当前可用："]
    lines += [f"- {s.name}：{_first_sentence(s.description)}" for s in items]
    lines += [
        "",
        "当用户的请求与上面某个技能高度相关时，**先调用本工具把它读进来再动手** —— "
        "这些说明书里写着数据源分工、口径陷阱和已验证的流程，不读容易给出看似合理但错误的结论。",
        "⚠ 与当前问题无关时不要调用，它会把不短的一段说明塞进上下文。",
    ]
    return "\n".join(lines)


def _first_sentence(description: str) -> str:
    """截到第一个句号，别把整段描述搬进常驻的工具 schema。

    ⚠ 找不到句号时**整段返回**而不是砍到某个字数：宁可多花一点上下文，
    也不要让模型看到半句话去猜这个技能是干什么的。
    """
    for end in ("。", ". "):
        idx = description.find(end)
        if idx > 0:
            return description[: idx + len(end)].strip()
    return description.strip()
