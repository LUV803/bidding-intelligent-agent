"""统计「标的物」列里每个标的物出现的次数（交易频次），输出「标的物」「交易频次」两列。

输入：batch/结果/招标采购标的物信息提取训练数据_2_标的物.xlsx（去重后的表）
输出：batch/结果/标的物_交易频次.xlsx

「交易频次」= 该标的物在这份表里出现的行数。表**已经去过重**（同一对
「项目编号 + 中标金额」只留一条），所以这个次数就是**不同采购项目的个数**，
也就是同一类标的物被采购了多少次——拿没去重的表来统计会把它整体放大。

运行：
    uv run python batch/统计数据_插交易频次.py
    uv run python batch/统计数据_插交易频次.py -i 别的表.xlsx -o 别的输出.xlsx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# 本文件在 <项目根>/batch/ 下，parents[1] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "batch"
RESULT_DIR = BATCH_DIR / "结果"

DEFAULT_INPUT = RESULT_DIR / "招标采购标的物信息提取训练数据_2_标的物.xlsx"
DEFAULT_OUTPUT = RESULT_DIR / "标的物_交易频次.xlsx"

DEFAULT_COLUMN = "标的物"
COUNT_COLUMN = "交易频次"

# 控制台预览前多少名，只影响打印，不影响输出文件
TOP_N_PREVIEW = 10


def count_frequencies(df: pd.DataFrame, column: str) -> tuple[pd.DataFrame, int]:
    """统计频次，返回 (两列的统计表, 跳过的空值行数)。"""
    series = df[column].dropna().astype(str).str.strip()

    # 空值/空白单独报数，不混进统计：否则结果里会凭空多出一行空名称的「标的物」，
    # 频次还挺高，看着像某种真实标的物
    empty = int((series == "").sum())
    series = series[series != ""]

    counts = series.value_counts().rename_axis(column).reset_index(name=COUNT_COLUMN)

    # 频次降序，同频次按名称升序。排序刻意写死成确定的：两次运行结果完全一致，
    # 拿去前后对比时才不会因为顺序抖动而误判成数据变了
    counts = counts.sort_values(
        [COUNT_COLUMN, column], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)
    return counts, empty


def main() -> int:
    parser = argparse.ArgumentParser(description="统计标的物的交易频次，输出「标的物」「交易频次」两列")
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT, help="含标的物列的表")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="输出的 Excel")
    parser.add_argument("--column", default=DEFAULT_COLUMN, help=f"要统计的列名（默认：{DEFAULT_COLUMN}）")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"[错误] 找不到输入文件：{args.input}", file=sys.stderr)
        return 1

    df = pd.read_excel(args.input)
    if args.column not in df.columns:
        print(f"[错误] 输入里没有「{args.column}」列。可用的列：{list(df.columns)}", file=sys.stderr)
        return 1

    counts, empty = count_frequencies(df, args.column)
    if counts.empty:
        print(f"[错误] 「{args.column}」列里没有任何可统计的值", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # index=False：不要把 pandas 的行号当成一列写进 Excel
    counts.to_excel(args.output, index=False)

    total = int(counts[COUNT_COLUMN].sum())
    print(f"输入：{args.input}")
    print(f"输出：{args.output}")
    print(f"  读取行数：{len(df)}" + (f"（跳过空标的物 {empty} 行）" if empty else ""))
    print(f"  计入统计：{total} 行")
    print(f"  不同标的物：{len(counts)} 种")
    print(f"  频次最高：{counts.at[0, args.column]!r} × {counts.at[0, COUNT_COLUMN]}")
    print(f"  频次最低：{counts.at[len(counts) - 1, args.column]!r} × {counts.at[len(counts) - 1, COUNT_COLUMN]}")

    head = counts.head(TOP_N_PREVIEW)
    print(f"\n  频次前 {len(head)}：")
    for _, row in head.iterrows():
        print(f"    {row[COUNT_COLUMN]:>4}  {row[args.column]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
