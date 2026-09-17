"""从 Batch 结果 jsonl 里取出「标的物」，插入 Excel 的「标的物」列。

输入：batch/batch_output_sorted.jsonl（按 custom_id 排序后的结果）
      batch/结果/招标采购标的物信息提取训练数据_2.xlsx（去重后的表，8751 行）
输出：batch/结果/招标采购标的物信息提取训练数据_2_标的物.xlsx

两种对齐方式，按行数自动选：

  ① **行数一致** —— 结果 jsonl 的 custom_id 正好铺满 0…N-1，直接按行号插。
     这是「编请求那张表 = 要插入那张表」的常规情形。

  ② **行数不一致** —— 结果对应的是编请求那张表（data/raw，8789 行），
     而目标表是它的**去重子集**（8751 行）。custom_id 在这里失效，
     于是重放一次去重，拿到「保留下来的原始行号」再依次接过去。

  为什么不用「项目编号 + 中标金额」去匹配：_2.xlsx 里有 116 行的键是不完整的
  （去重时它们不参与分组、被整行保留），键匹配会把这 116 行整片漏掉。
  重放去重拿的是行号，不依赖键是否完整。

⚠ 重放去重依赖 去重.py 的 dedupe()。如果将来换了去重规则，这里必须跟着换，
   否则接出来的值会整体错位——所以下面拿「核心标的物」列做交叉校验，对不上就拒绝。

运行：
    uv run python batch/提数据_插标的物.py
    uv run python batch/提数据_插标的物.py -i 别的表.xlsx --column 标的物
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
RESULT_DIR = BATCH_DIR / "结果"

# 让 `from 去重 import ...` 在脚本被直接运行时也能找到同目录的模块
sys.path.insert(0, str(BATCH_DIR))
from 去重 import DEFAULT_KEYS, dedupe  # noqa: E402  （放在 sys.path 之后是必须的）

# 编请求时用的原始表。custom_id 就是它的行号；行数不一致时也靠它重放去重
ORIGIN_XLSX = PROJECT_ROOT / "data" / "raw" / "招标采购标的物信息提取训练数据.xlsx"

DEFAULT_JSONL = BATCH_DIR / "batch_output_sorted.jsonl"
DEFAULT_INPUT = RESULT_DIR / "招标采购标的物信息提取训练数据_2.xlsx"

# 要写入 Excel 的列名。输出文件名是 <输入名>_<列名>.xlsx
DEFAULT_COLUMN = "标的物"

# 加工后的结果 jsonl 里直接放标的物的字段名（见 load_values 的形态②）
VALUE_FIELD = "标的物"

# 交叉校验用的参照列：去重前的全量结果里已经有了同一批值。
# 两边对不上就说明对齐方式选错了，必须停下来而不是硬写。
REFERENCE_COLUMN = "核心标的物"


def load_values(path: Path) -> tuple[dict[int, str], list[str]]:
    """读出 {行号: 标的物}，以及解析过程中的问题。

    兼容两种形态，这样 --jsonl 指原始结果还是加工后的结果都能用：

      ① 服务端原始结果：标的物在 response.body.choices[0].message.content
      ② 加工后的结果：顶层直接有「标的物」字段

    行号取自 custom_id 而非行在文件里的位置——结果文件不保证按行号排列
    （实测原始结果整体旋转过、还夹杂逆序），按位置对必然错位。
    """
    values: dict[int, str] = {}
    problems: list[str] = []

    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                problems.append(f"第 {line_no} 行不是合法 JSON：{exc}")
                continue

            custom_id = str(item.get("custom_id", "")).strip()
            if not custom_id:
                problems.append(f"第 {line_no} 行没有 custom_id")
                continue

            text = item.get(VALUE_FIELD)
            if text is None:  # 形态①
                try:
                    text = item["response"]["body"]["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    problems.append(f"custom_id={custom_id} 里找不到标的物")
                    continue

            try:
                values[int(custom_id)] = str(text or "").strip()
            except ValueError:
                problems.append(f"custom_id={custom_id} 不是数字，对不回行号")

    return values, problems


def resolve_by_dedupe(
    values: dict[int, str], n_rows: int, origin_xlsx: Path
) -> tuple[list[str] | None, str | None]:
    """行数不一致时的接法：重放去重，按「保留下来的原始行号」依次取值。

    返回 (值列表, 错误说明)，二者只有一个非 None。
    """
    if not origin_xlsx.exists():
        return None, f"找不到编请求时用的原始表：{origin_xlsx}"

    kept = sorted(set(dedupe(pd.read_excel(origin_xlsx), DEFAULT_KEYS).index))
    if len(kept) != n_rows:
        return None, (
            f"重放去重得到 {len(kept)} 行，与目标表的 {n_rows} 行不符——"
            "目标表不是这份结果对应表的去重结果"
        )

    beyond = [i for i in kept if i not in values]
    if beyond:
        return None, f"有 {len(beyond)} 行的原始行号没有对应结果（例如第 {beyond[0]} 行）"

    return [values[i] for i in kept], None


def describe_mismatch(jsonl_path: Path, input_path: Path, n_rows: int,
                      values: dict[int, str]) -> None:
    """两种接法都失败时，把「差在哪」说清楚。"""
    ids = sorted(values)
    print("[错误] 结果文件与目标表对不上，拒绝插入。", file=sys.stderr)
    print(f"       {jsonl_path.name}：{len(values)} 条（custom_id {ids[0]} … {ids[-1]}）", file=sys.stderr)
    print(f"       {input_path.name}：{n_rows} 行", file=sys.stderr)
    print("\n       custom_id 是**编请求时那张表**的行号。请确认目标表要么与它行数一致，", file=sys.stderr)
    print("       要么正好是它的去重子集（去重规则见 batch/去重.py）。", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="把结果 jsonl 里的标的物插入 Excel 的指定列")
    parser.add_argument("-j", "--jsonl", type=Path, default=DEFAULT_JSONL, help="结果 jsonl")
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT, help="要插入的 Excel")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"输出的 Excel（默认 {RESULT_DIR.name}/<输入名>_<列名>.xlsx）",
    )
    parser.add_argument("--column", default=DEFAULT_COLUMN, help=f"写入的列名（默认：{DEFAULT_COLUMN}）")
    parser.add_argument(
        "--origin",
        type=Path,
        default=ORIGIN_XLSX,
        help="编请求时用的原始表，行数不一致时靠它重放去重",
    )
    args = parser.parse_args()

    for path, label in ((args.jsonl, "结果 jsonl"), (args.input, "Excel")):
        if not path.exists():
            print(f"[错误] 找不到{label}：{path}", file=sys.stderr)
            return 1

    values, problems = load_values(args.jsonl)
    for message in problems[:10]:
        print(f"  [警告] {message}", file=sys.stderr)
    if len(problems) > 10:
        print(f"  [警告] …另有 {len(problems) - 10} 条", file=sys.stderr)
    if not values:
        print(f"[错误] {args.jsonl} 里没读到任何标的物", file=sys.stderr)
        return 1

    df = pd.read_excel(args.input)
    n_rows = len(df)

    # ---------- 选对齐方式 ----------
    if sorted(values) == list(range(n_rows)):
        # 常规情形：custom_id 正好铺满目标表，直接按行号取。
        # 刻意不用 df[col] = list(values.values())——那依赖 jsonl 里的先后顺序，
        # 一旦没排序就整体错位，而且看不出来。
        result: list[str] = [values[i] for i in range(n_rows)]
        method = f"按 custom_id 行号（{n_rows} 行一一对应）"
    else:
        result, error = resolve_by_dedupe(values, n_rows, args.origin)
        if error:
            print(f"[错误] {error}", file=sys.stderr)
            describe_mismatch(args.jsonl, args.input, n_rows, values)
            return 1
        method = f"重放去重（{len(values)} 条结果 → {n_rows} 行）"

    print(f"结果 jsonl：{args.jsonl}")
    print(f"输入：{args.input}")
    print(f"  对齐方式：{method}")

    df[args.column] = result

    # ---------- 交叉校验 ----------
    # 去重前的全量表里已经有同一批值（run_batch.py 写回去的）。两边对不上就说明
    # 对齐方式选错了——宁可拒绝，也不要产出一份看起来没问题的错数据。
    if REFERENCE_COLUMN in df.columns:
        diff = sum(
            1
            for i in range(n_rows)
            if str(df.at[i, args.column]).strip() != str(df.at[i, REFERENCE_COLUMN]).strip()
        )
        print(f"  与已有「{REFERENCE_COLUMN}」列比对：{n_rows - diff} / {n_rows} 一致")
        if diff:
            print(
                f"[错误] 有 {diff} 行与「{REFERENCE_COLUMN}」列不一致，对齐方式可能错了，未写出文件。",
                file=sys.stderr,
            )
            return 1

    output = args.output or RESULT_DIR / f"{args.input.stem}_{args.column}.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    # index=False：不要把 pandas 的行号当成一列写进 Excel
    df.to_excel(output, index=False)

    empty = sum(1 for value in result if not value)
    print(f"输出：{output}")
    print(f"  写入行数：{n_rows}")
    print(f"  列数：{len(df.columns)}（含「{args.column}」）")
    if empty:
        print(f"  [警告] 其中 {empty} 行的标的物是空的")

    # 抽查两条，方便肉眼确认没串行
    name_col = "项目名称" if "项目名称" in df.columns else df.columns[0]
    for i in (0, n_rows - 1):
        print(f"    第 {i} 行  {df.at[i, name_col]}  ->  {df.at[i, args.column]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
