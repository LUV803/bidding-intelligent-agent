"""读取招标数据 Excel，生成智谱 Batch API 所需的 JSONL 输入文件。

用途：把每一条「项目名称」包装成一个独立的 chat 请求，交给 GLM 抽取「核心标的物」。

运行：
    uv run python batch/build_batch_jsonl.py
    uv run python batch/build_batch_jsonl.py --input 别的文件.xlsx

输出：batch/batch_input.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# 本文件在 <项目根>/batch/ 下，parents[1] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "batch"

# 实际数据文件名（注意：不是 ..._s.xlsx，文件名里没有 _s 后缀）
DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "招标采购标的物信息提取训练数据.xlsx"
DEFAULT_OUTPUT = BATCH_DIR / "batch_input.jsonl"

# 模型 id。写成常量，将来换模型只改这一行。
#
# ── 为什么是 glm-4-air-250414 ──────────────────────────────────────────
# 原先用 glm-4-flash（免费），因为它免费、且是非推理模型。但它有个致命问题：
# **免费档在批处理队列里几乎排不上队**。2026-09 的实测记录（同一账号、同一份文件）：
#
#   #1  8789 条  glm-4-flash  傍晚提交  → 0/8789，取消
#   #2  10  条  glm-4-flash  傍晚提交  → 0/8789，取消（10 条也跑不动，排除文件大小因素）
#   #3  8789 条  glm-4-flash  18:10 提交 → 8.9 小时后成功
#   #4  8789 条  glm-4-flash  09:32 提交 → 23.4 小时仍停在 validating
#
# 即「能不能跑起来」全看运气。换到付费档的 air 系列是当前最可行的规避手段——
# 这不是「付费更快」的定论（官方文档对批处理队列优先级只字未提），而是实测下
# 免费档反复失败后的必然选择。单次全量成本约 0.66 元，可接受。
#
# ── 为什么带日期后缀 ───────────────────────────────────────────────────
# 不带日期的 `glm-4-air` 是**浮动别名**，平台换快照时会静默跟着变：同一份数据
# 今天跑和下周跑可能不是同一个模型，结果无法复现。实测两者行为确有差异
# （别名返回 123/6 token，250414 返回 119/7），证明它们指向不同快照。
# 抽取结果要写进 Excel 去做去重和分析，可复现比「总是最新」重要。
#
# ── 为什么不用 glm-4.7-flash / glm-5.1 这类更新的模型 ───────────────────
# ① glm-4.7-flash 根本不在批量接口的白名单里（实时接口有、批处理没有），
#    上传阶段整份被拒——这是最初撞到的报错，也是本文件里 MODEL 换过一次的原因；
# ② glm-5.1 / glm-5-turbo 是**推理模型**，会先吐思维链再出 content，max_tokens
#    给小了 content 会是空串（实测 max_tokens=64 时确实返回 ''）。对「只输出一个
#    词」的抽取任务纯属浪费。而 air 系列实测 reasoning_content 为 None，直出 content。
#
# 批量接口完整白名单可直接问服务端要：上传一个模型名非法的 jsonl，报错里会列出
# 当前支持的全部模型（2026-09-17 实测有 24 个，其中文本对话类 13 个）。
MODEL = "glm-4-air-250414"

# Batch 接口的固定写法：url 与 method 都要照抄，body 才是真正的请求参数
ENDPOINT = "/v4/chat/completions"
METHOD = "POST"

# 单文件请求数上限为 50000（官方限制），超过该值必须拆成多个文件分别提交
MAX_REQUESTS_PER_FILE = 50000

# custom_id 最短长度。官方文档没写这条，是实测撞出来的：
# 直接用行号 "0"、"1" … 上传时会整个文件被拒——
#   400 {"error":{"code":"1210","message":"custom id 长度不足, 最短: 6"}}
# 所以行号必须零填充到 6 位（"000000"、"000001" …）。
# 填充后仍是纯数字，run_batch.parse_results 里的 int(custom_id) 照样解析得回行号。
MIN_CUSTOM_ID_LEN = 6


def make_custom_id(position: int) -> str:
    """把行位置编码成合法的 custom_id（零填充到至少 6 位）。

    零填充而非加前缀，是为了让它仍能被 int() 直接还原成行号；
    行数超过 999999 时自然变长，不会违反下限。
    """
    return str(position).zfill(MIN_CUSTOM_ID_LEN)

# 提示词模板。{name} 处填入项目名称。
#
# v1（单句版）的问题：「商品**或**服务」那个「或」没界定清楚，服务类采购会被
# 剥掉服务后缀——「某燃气有限公司超声波流量计检测服务项目」抽成
# 「超声波流量计」，可这条采购的标的恰恰是**检测服务**本身，不是流量计这个硬件。
# v2 补了两条硬规则：先剔除公司名/单位名/地名/年份等修饰，但服务类必须保留
# 完整服务名。示例刻意用一个**不在这批数据里**的办公设备维保，避免把实测样例
# 直接喂进提示词、结果看起来好其实是在背答案。
#
# v2 实测（实时接口 glm-4-flash，temperature=0）拿下面 9 条对拍：
#   超声波流量计检测服务项目     -> 超声波流量计检测服务   ★ 本次要修的
#   六安烟草现代化建设咨询服务   -> 烟草现代化建设咨询服务 （v1 抽成「烟草」）
#   某市人民医院公开招聘服务     -> 公开招聘服务           （v1 抽成「服务项目」）
#   农业农村水务局…文件管理…     -> 文件管理               （v1 抽成「网上超市采购」）
#   某街道社区卫生服务中心…应用软件… -> 应用软件          （v1 抽成「网上超市采购」）
#   某园林管理服务中心…铭牌…、触摸式终端设备…、服务器…、物业服务项目
#                                -> 铭牌 / 触摸式终端设备 / 服务器 / 物业服务 （无回归）
# 即 v2 除了修目标样例，还顺手修掉了 v1 那 43 条「网上超市采购」类缺陷。
PROMPT_TEMPLATE = (
    "请从以下项目名称中，提取一个字段：核心标的物。\n\n"
    "要求：\n"
    "1. 只输出标的物本身，不要包含公司名、单位名、地名、年份等修饰成分；\n"
    "2. 若采购的是服务，必须保留完整的服务名称——例如「XX公司办公设备维保服务项目」"
    "应提取为「办公设备维保服务」，不能只写「办公设备」；\n"
    "3. 只输出结果本身，不要任何解释、引号或标点。\n\n"
    "项目名称：{name}"
)


def build_request(custom_id: str, project_name: str) -> dict:
    """组装单条 Batch 请求。

    custom_id 是请求与结果之间唯一的对应凭据：结果文件里的顺序不保证与输入一致，
    只能靠 custom_id 反查回是哪一行数据，因此这里用行号做绑定。
    """
    return {
        "custom_id": custom_id,
        "method": METHOD,
        "url": ENDPOINT,
        "body": {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": PROMPT_TEMPLATE.format(name=project_name)}
            ],
            # temperature=0：抽取任务要的是稳定复现，同样的输入必须给出同样的输出
            "temperature": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成智谱 Batch API 的 JSONL 输入文件")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="源 Excel 路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出的 jsonl 路径")
    parser.add_argument(
        "--column", default="项目名称", help="要抽取的文本所在列名（默认：项目名称）"
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"[错误] 找不到输入文件：{args.input}", file=sys.stderr)
        return 1

    df = pd.read_excel(args.input)
    if args.column not in df.columns:
        print(
            f"[错误] 列 {args.column!r} 不存在。可用的列：{list(df.columns)}",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    # newline="\n"：Windows 的文本模式会把 "\n" 翻译成 "\r\n"，产出的 jsonl 就成了
    # CRLF 换行（JSONL 规范要求 LF）。实测 CRLF 服务端也能正常解析——用它跑出过
    # 8789/8789 的完整结果——所以这纯粹是规范问题，不是能不能跑的问题。
    # 之所以还是统一成 LF：提数据_编JSONL.py 产出的同种文件就是 LF，
    # 两个脚本写同一种文件却换行不同，只会让将来对比文件的人白费工夫。
    with args.output.open("w", encoding="utf-8", newline="\n") as fh:
        for position, name in df[args.column].items():
            # 跳过空值：空项目名称送进模型只会浪费一次请求
            if pd.isna(name) or not str(name).strip():
                skipped += 1
                continue

            # custom_id 绑定「行号」= DataFrame 的行位置（0 表示表头下的第一行数据）。
            # 用位置而非 Excel 行号（+2）是因为位置在重新读取文件后依然稳定。
            # 必须经 make_custom_id 零填充——短于 6 位会被服务端整份拒收。
            request = build_request(make_custom_id(position), str(name).strip())

            # ensure_ascii=False 让中文按原样写入而不是转成 \uXXXX 转义，
            # 便于人工核对文件内容；两种写法都是合法 JSON，接口都能解析。
            fh.write(json.dumps(request, ensure_ascii=False) + "\n")
            written += 1

    size_mb = args.output.stat().st_size / 1024 / 1024
    print(f"已生成：{args.output}")
    print(f"  请求数：{written}" + (f"（跳过空值 {skipped} 条）" if skipped else ""))
    print(f"  文件大小：{size_mb:.2f} MB")
    print(f"  模型：{MODEL}  temperature=0")

    if written > MAX_REQUESTS_PER_FILE:
        print(
            f"[警告] 请求数超过单文件上限 {MAX_REQUESTS_PER_FILE}，需要拆分成多个文件后分别提交",
            file=sys.stderr,
        )
        return 2
    if written > 5000:
        # 官方文档的实践建议：单批不超过 5000 条，可降低上传失败的概率
        print(f"  [提示] 超过 5000 条，若上传失败可考虑拆分文件后分批提交")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
