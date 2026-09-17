"""提交智谱 Batch 任务、轮询进度、下载结果并写回 Excel。

完整流程：
    1. 上传 batch_input.jsonl            → 得到 file_id
    2. 用 file_id 创建 batch 任务        → 得到 batch_id
    3. 循环轮询任务状态，直到终态
    4. 下载结果 jsonl，按 custom_id 解析出「核心标的物」
    5. 写回 Excel → batch/结果/抽取结果.xlsx

运行：
    uv run python batch/run_batch.py

说明：官方标注任务最长 24 小时内完成，中途 Ctrl+C 不会丢失进度——
上传与任务 id 会记录在 batch/batch_state.json，重新运行会自动接着轮询，
不会重复提交（重复提交是要计费的）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "batch"

DEFAULT_INPUT_JSONL = BATCH_DIR / "batch_input.jsonl"
DEFAULT_SOURCE_XLSX = PROJECT_ROOT / "data" / "raw" / "招标采购标的物信息提取训练数据.xlsx"
# 最终产物统一放这个子目录：与脚本、中间文件（jsonl）分开，一眼能看出哪个是要交付的东西。
# 去重.py 的 DEFAULT_INPUT 必须与本路径保持一致，改这里就要同步改那边。
RESULT_DIR = BATCH_DIR / "结果"
DEFAULT_OUTPUT_XLSX = RESULT_DIR / "抽取结果.xlsx"
STATE_FILE = BATCH_DIR / "batch_state.json"
# 预算台账。刻意与 STATE_FILE 分开存：state 在任务正常结束后会被删掉，
# 而台账必须跨多次提交持续累计，否则每次重跑都从 0 开始，护栏形同虚设。
BUDGET_FILE = BATCH_DIR / "batch_budget.json"

# 抽取结果写回 Excel 时的列名
RESULT_COLUMN = "核心标的物"

# 请求里被抽取的那一列。对齐自检（verify_alignment）拿它把请求和源表逐行比一遍。
# 与 build_batch_jsonl.py 的 --column 默认值保持一致。
NAME_COLUMN = "项目名称"

# Batch 接口的 endpoint。官方文档明确写着「目前仅支持 /v4/chat/completions」。
# 注意别改：zhipuai SDK 把这个参数的类型标注成了
# Literal["/v1/chat/completions", "/v1/embeddings"]，那是从 OpenAI SDK 整段抄过来
# 忘了改的（连 docstring 里的链接都还指着 platform.openai.com）。
# Python 运行时并不检查字面量类型，最终以服务端为准，所以照着类型提示改成 /v1 会失败。
ENDPOINT = "/v4/chat/completions"

# 任务的终态：到达其中任意一个就不该继续轮询了
TERMINAL_STATUSES = {"completed", "expired", "cancelled", "failed"}

# 轮询间隔（秒）。任务动辄跑几十分钟，轮询太密没有意义还容易触发限流。
POLL_INTERVAL = 30

# ---------- 预算护栏 ----------
# 提交前估算花费、把每次提交的估算值记进台账，累计超过上限就直接拒绝提交。
# 为什么用「估算 + 台账」而不是查账单：批处理动辄跑几小时，等账单出来才发现
# 超支已经晚了——护栏必须在 create() 之前就能给出结论。而免费档任务反复卡死、
# 需要一遍遍重跑，正是最容易不知不觉花超的场景，所以累计值必须持久化。
DEFAULT_BUDGET = 5.0

# 价格表（元 / 百万 token），**仅用于预算估算**。
# 来源是公开报道而非服务端接口——服务端只提供模型白名单，不提供价格查询。
# 各家口径有出入（glm-4-air 有 0.5 与 0.6 两个版本，另有 2024 年的 1.0 旧价），
# 因此这里统一取**偏高**的值：估高了最多少跑一次，估低了会真的超预算。
# 实际价格请在 https://open.bigmodel.cn/ 控制台核对，需要修正时用 --price 覆盖。
MODEL_PRICES = {
    "glm-4-flash": 0.0,           # 免费档
    "glm-4-flashx-250414": 0.1,
    "glm-4-air-0111": 0.6,
    "glm-4-air": 0.6,
    "glm-4-air-250414": 0.6,
    "glm-4-plus": 5.0,
}
# 价格表里没有的模型一律按最贵档算，宁可估高
DEFAULT_PRICE = 5.0

# 每条请求的输出 token 估算值。v1 的 8789 条实测：平均 5.2、中位 5、最大 44。
# 取 12 约为平均值的 2.3 倍。输出只占总成本的 4%，高估的代价极小。
ASSUMED_OUTPUT_TOKENS = 12

# 字符数 → token 的换算比率。实测 glm-4-air-250414：189 字符的提示词 = 119 token。
CHARS_PER_TOKEN = 189 / 119


def load_api_key() -> str:
    """读取智谱 API Key。

    优先取环境变量；环境变量没有则回退到项目根目录的 .env。
    这里手写一个极简的 .env 解析，避免为了一个键引入额外依赖。
    """
    key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if key:
        return key

    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "ZHIPU_API_KEY":
                return value.strip().strip('"').strip("'")

    print(
        "[错误] 未找到 ZHIPU_API_KEY。\n"
        "       请到 https://open.bigmodel.cn/ 获取 API Key，然后任选其一：\n"
        "         · 在项目根目录的 .env 里加一行：ZHIPU_API_KEY=你的key\n"
        "         · 或设置环境变量：export ZHIPU_API_KEY=你的key",
        file=sys.stderr,
    )
    raise SystemExit(1)


def load_state(path: Path | None = None) -> dict:
    path = path or STATE_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict, path: Path | None = None) -> None:
    # 允许指定路径，是为了让小规模验证任务（--state）与正式任务互不干扰
    path = path or STATE_FILE
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def response_to_bytes(response) -> bytes:
    """把 SDK 返回的文件内容统一转成 bytes。

    不同版本的 SDK 可能返回 httpx.Response（取 .content）或裸 bytes/str，
    这里全部兼容掉，免得升级 SDK 后脚本突然跑不通。
    """
    if isinstance(response, bytes):
        return response
    if isinstance(response, str):
        return response.encode("utf-8")
    for attr in ("content", "text", "read"):
        value = getattr(response, attr, None)
        if value is None:
            continue
        value = value() if callable(value) else value
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, bytes):
            return value
    raise TypeError(f"无法从 {type(response)} 中取出文件内容")


def detect_model(jsonl_path: Path) -> str:
    """从输入文件里读出实际使用的模型名。

    不把模型名写死在脚本里，是为了保证预检用的就是真正要提交的那个模型——
    否则预检通过了、批量任务用的却是另一个名字，白检。
    """
    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                return json.loads(line)["body"]["model"]
    raise ValueError(f"输入文件是空的：{jsonl_path}")


def verify_alignment(jsonl_path: Path, source_xlsx: Path, column: str = NAME_COLUMN) -> str | None:
    """逐行核对请求里的项目名称与源 Excel 是否配套。配套返回 None，否则返回错误说明。

    custom_id 就是行号（见 build_batch_jsonl.py / 提数据_编JSONL.py），回填时按行号写回。
    两条链路并存之后，配错是很容易发生的事：

        提数据_编JSONL.py 从**去重后**的 _2.xlsx（8751 行）编请求，
        回填却用了默认的**原始**表（8789 行）。

    去重删掉的 38 行散布全表，**第 0 行就被删掉了**，于是两边从第一行起就错开、越往后偏
    得越多。而 write_back() 取的是 extracted.get(position, pd.NA)——取不到就默默填空，
    不抛异常，整份表张冠李戴却一声不吭。

    所以这里逐条比一次：源表第 N 行的项目名称必须出现在 custom_id=N 的请求正文里。
    对不上就是两份文件不配套——宁可不提交，也不能让它跑完几小时再来发现全错。
    """
    try:
        names = pd.read_excel(source_xlsx, usecols=[column])[column]
    except FileNotFoundError:
        return f"找不到源 Excel：{source_xlsx}"
    except ValueError as exc:
        return f"源 Excel {source_xlsx.name} 里没有「{column}」列（{exc}）"

    checked = 0
    with jsonl_path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
                position = int(str(item["custom_id"]).strip())
                content = item["body"]["messages"][0]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                return f"{jsonl_path.name} 第 {line_no} 行读不出 custom_id / 提示词（{exc}）"

            if position >= len(names):
                return (
                    f"custom_id={position} 超出源表范围——{source_xlsx.name} 只有 "
                    f"{len(names)} 行。\n"
                    "           两份文件不是同一套，请核对 --jsonl 与 --source 是否配套。"
                )

            value = names.iloc[position]
            # 源表该行为空时无从比对（空行本来也不该被编进请求），跳过
            if pd.isna(value):
                checked += 1
                continue
            name = str(value).strip()
            if name and name not in content:
                return (
                    f"第 {position} 行对不上：源表是「{name}」，"
                    f"而 custom_id={position} 的请求正文里没有出现这个项目名称。\n"
                    "           两份文件不是同一套，请核对 --jsonl 与 --source 是否配套。"
                )
            checked += 1

    print(f"[通过] 对齐自检：{checked} 条请求与 {source_xlsx.name} 逐行对得上")
    return None


def preflight(client, model: str) -> bool:
    """提交批量任务前先发一条最小请求。返回 True 表示可以继续提交。

    key 无效、模型名写错、账户余额不足——这三个问题都只会在任务**提交之后**才暴露，
    而那时已经开始计费了。一次很小的请求几乎不花钱，却能提前排掉它们。

    **429（模型访问量过大）不算失败**，这是本函数最反直觉的一点，别「顺手修掉」：
    它的真实含义是「key 有效、模型名正确、网络通畅，只是该模型此刻被挤爆了」——
    恰恰证明配置是对的。而且实时接口限流不代表批量接口跑不动：Batch 是离线排队处理，
    官方明确说无并发限制。若把 429 当成中止条件，模型一忙就永远提交不了任务。
    真正该中止的只有三类：key 无效(401)、模型名不存在(404)、余额不足(402)。

    另外，返回空 content 也是正常的——GLM 是推理模型，max_tokens 给小了会被思维链吃光，
    这同样不构成中止理由（批量任务没设 max_tokens，不受此影响）。
    """
    print(f"预检：用 {model} 发一条最小请求 …", flush=True)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复两个字：正常"}],
            max_tokens=64,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 —— 失败原因很多，统一归类成可读提示
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__

        if status == 429 or "ReachLimit" in name:
            print(
                "[警告] 模型当前访问量过大（429）。但这说明 key 与模型名都是对的——"
                "实时接口限流不影响批量接口（Batch 离线排队、无并发限制），继续提交。",
                file=sys.stderr,
            )
            return True

        hint = {
            401: "key 无效或复制不完整",
            402: "账户余额不足，请先充值",
            404: "模型名不存在，核对 build_batch_jsonl.py 里的 MODEL 常量",
        }.get(status, "网络不通，或无法访问 open.bigmodel.cn")
        print(f"[失败] 预检未通过：{name}: {exc}", file=sys.stderr)
        print(f"       最可能的原因：{hint}", file=sys.stderr)
        return False

    text = (response.choices[0].message.content or "").strip()
    print(f"[通过] 预检成功，模型返回：{text!r}")
    return True


def parse_results(content: bytes) -> tuple[dict[int, str], list[str]]:
    """解析结果 jsonl，返回 ({行号: 核心标的物}, [错误信息])。

    custom_id 就是行号字符串（见 build_batch_jsonl.py），这里转回 int 作为键。
    """
    extracted: dict[int, str] = {}
    errors: list[str] = []

    for line_no, raw in enumerate(content.decode("utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"结果第 {line_no} 行无法解析：{exc}")
            continue

        custom_id = str(item.get("custom_id", "")).strip()
        if not custom_id:
            errors.append(f"结果第 {line_no} 行缺少 custom_id")
            continue

        # 失败请求会在 error 字段里给出原因，此时 response 通常为 null
        error = item.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            errors.append(f"custom_id={custom_id} 请求失败：{message}")
            continue

        try:
            body = item["response"]["body"]
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            errors.append(f"custom_id={custom_id} 响应结构异常（{exc}）")
            continue

        try:
            extracted[int(custom_id)] = text.strip()
        except ValueError:
            errors.append(f"custom_id={custom_id} 不是数字，无法对回行号")

    return extracted, errors


def write_back(source_xlsx: Path, extracted: dict[int, str], output_xlsx: Path) -> None:
    """把抽取结果按行号写回原始 Excel。

    刻意重新读取源文件（而不是复用内存里的 DataFrame），保证输出的列顺序、
    列名与原文件完全一致，只是在末尾追加一列。
    """
    df = pd.read_excel(source_xlsx)

    # 按 DataFrame 行位置取值；没抽到的行留空（NaN），而不是填占位字符串，
    # 这样后续 去重.py 统计空值数量时才能正确识别。
    df[RESULT_COLUMN] = [extracted.get(position, pd.NA) for position in range(len(df))]

    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    # index=False：不要把 pandas 的行号当成一列写进 Excel
    df.to_excel(output_xlsx, index=False)

    hit = sum(1 for position in range(len(df)) if position in extracted)
    print(f"已写出：{output_xlsx}")
    print(f"  抽到结果：{hit} / {len(df)} 行，其余为空")


# ---------- 预算护栏 ----------

def resolve_price(model: str, override: float | None = None) -> float:
    """查出模型单价（元 / 百万 token）。

    override（来自 --price）优先。价格表里查不到的模型一律按 DEFAULT_PRICE
    最贵档算——护栏宁可估高，估低了就守不住了。
    """
    if override is not None:
        return override
    return MODEL_PRICES.get(model, DEFAULT_PRICE)


def estimate_cost(jsonl_path: Path, unit_price: float) -> tuple[float, int]:
    """估算整份 jsonl 的提交成本（元），返回 (金额, 请求数)。

    为什么不取真实 token 数：真实用量只有请求发出去才知道，而为了统计先把
    8789 条都发一遍，成本反而超过任务本身。所以改用「字符数 ÷ 实测比率」换算
    ——所有请求共用同一个提示词模板，只有项目名称长度不同，换算误差很小。
    再加上 ASSUMED_OUTPUT_TOKENS 的刻意高估，整体是偏保守的。
    """
    total_chars = 0
    count = 0
    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            body = json.loads(line)["body"]
            total_chars += sum(len(m.get("content", "")) for m in body["messages"])
            count += 1

    in_tokens = total_chars / CHARS_PER_TOKEN
    out_tokens = count * ASSUMED_OUTPUT_TOKENS
    return (in_tokens + out_tokens) / 1_000_000 * unit_price, count


def load_budget() -> dict:
    if BUDGET_FILE.exists():
        try:
            return json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 台账损坏时当作空账本，而不是让整个脚本起不来
            pass
    return {"entries": []}


def save_budget(data: dict) -> None:
    BUDGET_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def committed_total(data: dict) -> float:
    """台账里累计的**已提交**预估花费。

    记的是提交时的估算值而非账单实际值，理由见 DEFAULT_BUDGET 处的注释：
    护栏必须在 create() 之前就能算出结论。
    """
    return sum(float(entry.get("est_cost", 0)) for entry in data.get("entries", []))


def main() -> int:
    parser = argparse.ArgumentParser(description="提交并轮询智谱 Batch 任务")
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_INPUT_JSONL, help="Batch 输入文件")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_XLSX, help="原始 Excel")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_XLSX, help="结果 Excel 路径")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL, help="轮询间隔秒数")
    parser.add_argument(
        "--reset", action="store_true", help="丢弃已记录的任务，强制重新提交（会产生新费用）"
    )
    parser.add_argument(
        "--check", action="store_true", help="只做预检（验证 key / 模型名 / 余额），不提交任务"
    )
    parser.add_argument(
        "--skip-check", action="store_true", help="跳过提交前的自动预检（不建议）"
    )
    parser.add_argument(
        "--skip-align-check",
        action="store_true",
        help="跳过「请求与源表逐行对齐」自检（仅当请求确实不是用项目名称编的才该用）",
    )
    parser.add_argument(
        "--state", type=Path, default=STATE_FILE, help="任务 id 的记录文件（用于隔离验证任务）"
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET,
        help=f"累计提交花费上限（元），超过则拒绝提交（默认 {DEFAULT_BUDGET}）",
    )
    parser.add_argument(
        "--price",
        type=float,
        default=None,
        help="手工指定单价（元/百万 token），覆盖内置价格表——内置值来自公开报道，可能与实际有出入",
    )
    args = parser.parse_args()
    state_path: Path = args.state

    # 输出重定向到文件时 Python 默认走块缓冲，几小时的任务会一直看不到进度，
    # 直到进程退出才一次性吐出来。改成行缓冲，日志实时可见。
    sys.stdout.reconfigure(line_buffering=True)

    try:
        from zhipuai import ZhipuAI
    except ImportError:
        print(
            "[错误] 未安装 zhipuai SDK。请先安装：\n"
            "         uv add zhipuai        （记入项目依赖）\n"
            "         uv pip install zhipuai（只装进当前虚拟环境）",
            file=sys.stderr,
        )
        return 1

    client = ZhipuAI(api_key=load_api_key())

    # ---------- 0. 读出模型名 ----------
    # 这一步必须**无条件**执行，不能留在下面的预检分支里：预算估算要用 model，
    # 而 --skip-check 会跳过整个预检分支，那时 model 就成了未定义变量。
    if not args.jsonl.exists():
        print(f"[错误] 找不到输入文件：{args.jsonl}", file=sys.stderr)
        print("       请先运行：uv run python batch/build_batch_jsonl.py", file=sys.stderr)
        return 1
    try:
        model = detect_model(args.jsonl)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"[错误] 无法从输入文件读出模型名：{exc}", file=sys.stderr)
        return 1

    # ---------- 0.4 对齐自检 ----------
    # 纯本地、不联网，所以排在预检前面：两份文件配错了，就一点都不该往服务端发。
    if not args.skip_align_check:
        problem = verify_alignment(args.jsonl, args.source)
        if problem:
            print(f"\n[拒绝提交] {problem}", file=sys.stderr)
            print("           未上传、未提交、未产生花费。", file=sys.stderr)
            return 1

    # ---------- 0.5 预检 ----------
    # 放在「动状态文件」和「上传」之前：key 或模型名有问题时，这两步都不该发生。
    if args.check or not args.skip_check:
        if not preflight(client, model):
            return 1
        if args.check:
            print("预检模式：到此为止，未提交任务、未改动状态文件。")
            return 0

    state = {} if args.reset else load_state(state_path)
    if args.reset and state_path.exists():
        state_path.unlink()
        print("[提示] 已清空历史任务记录，将重新提交")

    # ---------- 1. 预算护栏 ----------
    # 放在**上传之前**：被拒绝的提交应当不产生任何副作用，连文件都不该传上去。
    # 只在确实要新建任务时才检查——复用已有任务（state 里有 batch_id）不会再花钱。
    unit_price: float | None = None
    est_cost = 0.0
    n_requests = 0
    budget_data: dict = {}
    if not state.get("batch_id"):
        unit_price = resolve_price(model, args.price)
        est_cost, n_requests = estimate_cost(args.jsonl, unit_price)
        budget_data = load_budget()
        already = committed_total(budget_data)
        print(
            f"预算：本次预估 {est_cost:.3f} 元"
            f"（{n_requests} 条 × {unit_price} 元/百万 token）"
        )
        print(f"      历史已提交累计 {already:.3f} 元，上限 {args.budget:.2f} 元")
        if already + est_cost > args.budget:
            print(
                f"\n[拒绝提交] 本次预估 {est_cost:.3f} 元会把累计花费推到 "
                f"{already + est_cost:.3f} 元，超过上限 {args.budget:.2f} 元。\n"
                f"           未提交任何内容，也未产生花费。\n"
                f"           确需继续，用 --budget {already + est_cost:.2f} 显式调高上限。",
                file=sys.stderr,
            )
            return 1

    # ---------- 2. 上传输入文件 ----------
    file_id = state.get("input_file_id")
    if file_id:
        print(f"复用已上传的输入文件：{file_id}")
    else:
        # 存在性已在第 0 步检查过，这里不再重复
        print(f"正在上传 {args.jsonl} …")
        with args.jsonl.open("rb") as fh:
            uploaded = client.files.create(file=fh, purpose="batch")
        file_id = uploaded.id
        state["input_file_id"] = file_id
        save_state(state, state_path)
        print(f"  上传完成，file_id = {file_id}")

    # ---------- 3. 创建（或复用）Batch 任务 ----------
    batch_id = state.get("batch_id")
    if batch_id:
        print(f"复用已创建的任务：{batch_id}")
    else:
        print("正在创建 Batch 任务 …")
        job = client.batches.create(
            input_file_id=file_id,
            endpoint=ENDPOINT,
            # 保留输入文件，便于对照排查；默认是 True（任务结束后自动删除）
            auto_delete_input_file=False,
            metadata={"source": args.jsonl.name},
        )
        batch_id = job.id
        state["batch_id"] = batch_id
        save_state(state, state_path)
        print(f"  任务已创建，batch_id = {batch_id}")

        # 记进台账。刻意放在 create() **之后**：创建失败就没有产生花费，
        # 不该占用预算额度。
        budget_data.setdefault("entries", []).append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "batch_id": batch_id,
                "model": model,
                "requests": n_requests,
                "est_cost": round(est_cost, 4),
            }
        )
        save_budget(budget_data)
        print(f"  已记入预算台账（累计 {committed_total(budget_data):.3f} 元）")

    # ---------- 3. 轮询直到终态 ----------
    print(f"开始轮询（每 {args.interval} 秒一次，Ctrl+C 可安全中断，重跑会接着轮询）")
    job = None
    try:
        while True:
            job = client.batches.retrieve(batch_id)
            status = job.status
            counts = getattr(job, "request_counts", None)
            if counts is not None:
                done = getattr(counts, "completed", 0) or 0
                total = getattr(counts, "total", 0) or 0
                print(f"  [{time.strftime('%H:%M:%S')}] 状态={status}  进度={done}/{total}")
            else:
                print(f"  [{time.strftime('%H:%M:%S')}] 状态={status}")

            if status in TERMINAL_STATUSES:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n已中断。任务仍在后台运行，batch_id = {batch_id}")
        print("重新运行本脚本即可接着轮询。")
        return 130

    if job is None:
        print("[错误] 未能获取任务状态", file=sys.stderr)
        return 1

    if job.status != "completed":
        print(f"[错误] 任务未正常完成，状态={job.status}", file=sys.stderr)
        errors = getattr(job, "errors", None)
        if errors:
            print(f"        详情：{errors}", file=sys.stderr)
        return 1

    # ---------- 4. 下载并解析结果 ----------
    output_file_id = getattr(job, "output_file_id", None)
    if not output_file_id:
        print("[错误] 任务已完成但没有结果文件 id", file=sys.stderr)
        return 1

    print(f"正在下载结果：{output_file_id} …")
    raw = response_to_bytes(client.files.content(file_id=output_file_id))

    # 原始结果留一份，出问题时可以直接翻查，不必重跑任务
    raw_path = BATCH_DIR / "batch_output.jsonl"
    raw_path.write_bytes(raw)
    print(f"  原始结果已存：{raw_path}")

    extracted, errors = parse_results(raw)

    # 失败的请求会被单独收进 error_file_id。少了这一步，就只能看到
    # 「解析成功 N 条」，却不知道其余的为什么没回来——而任务是要计费的。
    error_file_id = getattr(job, "error_file_id", None)
    if error_file_id:
        print(f"正在下载失败明细：{error_file_id} …")
        raw_errors = response_to_bytes(client.files.content(file_id=error_file_id))
        error_path = BATCH_DIR / "batch_error.jsonl"
        error_path.write_bytes(raw_errors)
        # 注意返回值顺序是 (抽到的内容, 错误列表)；错误文件里我们要的是后者
        _, failed_messages = parse_results(raw_errors)
        errors.extend(failed_messages)
        print(f"  失败明细已存：{error_path}")

    print(f"  解析成功 {len(extracted)} 条，失败 {len(errors)} 条")

    # 空回答的哨兵。GLM 是推理模型，思维链会先吃掉 max_tokens；预检时 max_tokens=64
    # 就返回了空 content。批量任务没设 max_tokens（走服务端默认），正常不该为空——
    # 但万一全空，这里必须立刻炸出来，否则会拿着 8789 条空结果一路跑到去重才发觉。
    empty = sum(1 for text in extracted.values() if not text)
    if empty:
        print(
            f"  [警告] 其中 {empty} 条是空回答！请先抽查 batch_output.jsonl 再往下走。",
            file=sys.stderr,
        )
    for message in errors[:10]:
        print(f"    - {message}")
    if len(errors) > 10:
        print(f"    …另有 {len(errors) - 10} 条，详见 batch_output.jsonl / batch_error.jsonl")

    # ---------- 5. 写回 Excel ----------
    write_back(args.source, extracted, args.output)

    # 任务已正常结束，清掉状态文件，避免下次运行误复用旧任务
    if state_path.exists():
        state_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
