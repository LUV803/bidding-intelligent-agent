"""按「项目编号 + 中标金额」对抽取结果去重，重复组内保留 null 值最少的那一行。

输入：batch/结果/抽取结果.xlsx
输出：batch/结果/招标采购标的物信息提取训练数据_2.xlsx（可用 -o 改）

为什么要去重：同一份招标数据里有重复条目——同一对「项目编号 + 中标金额」出现多次，
但模型的抽取质量不一样（有的行其他字段缺得多）。同一份采购留一条即可。

⚠ 输入必须是**抽取结果**（18 列 = 原始 17 列 + 「核心标的物」），不要拿 data/raw 下的
   原始 17 列文件当输入：那份里根本没有「核心标的物」列，去重出来只是原始数据的副本，
   模型抽出来的成果会被整个丢掉——而行数几乎一样，不数列数根本察觉不到。

运行：
    uv run python batch/去重.py
    uv run python batch/去重.py -i 别的文件.xlsx -o 别的输出.xlsx
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

DEFAULT_INPUT = RESULT_DIR / "抽取结果.xlsx"
# 名字里的 _2 沿用项目文档里的叫法：原始数据 17 列，加上「核心标的物」= 18 列
DEFAULT_OUTPUT = RESULT_DIR / "招标采购标的物信息提取训练数据_2.xlsx"

# 联合去重键。两列合起来才唯一，单独任何一列都不行：
# 项目编号会跨行重复，中标金额更是大面积撞车。
DEFAULT_KEYS = ["项目编号", "中标金额"]

# 抽取成果列。缺了它说明输入拿错了（见文件头 ⚠），直接拦下而不是产出一份假的去重结果
RESULT_COLUMN = "核心标的物"

# 拼接复合键的分隔符：U+001F 单元分隔符，正常文本里不可能出现，
# 用它拼接才不会让 ("a|b", "c") 和 ("a", "b|c") 撞成同一个键
KEY_SEP = "\x1f"


def normalize_key_value(value) -> str:
    """把去重键统一成可直接比较的字符串，返回空串表示该键缺失。

    Excel 读回来的同一列里会混着 int / float / str：

    - ``1000001`` 可能读成 ``1000001.0``（该列只要有一行是浮点，整列就被提升成 float），
      直接 str() 会得到 "1000001.0"，和另一处写法的 "1000001" 对不上；
    - 单元格末尾多一个空格，就是 "1000001 "，strip 掉才和 "1000001" 相等。

    不统一的话这些本该合并的重复行会被当成不同的键漏掉。
    """
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def build_missing_mask(df: pd.DataFrame) -> pd.Series:
    """逐行统计缺失值个数，作为「保留 null 最少那条」的排序依据。

    空字符串按缺失算：模型有时把没抽到的字段写成 "" 而不是留空，若不当缺失，
    两条本来一好一坏的行会打平成 0:0，留哪条就变成看运气。
    """
    # 逐列 strip：.str 访问器只挂在 Series 上，DataFrame 没有（写 text.str 会 AttributeError），
    # 所以用 apply 把每一列当 Series 处理，再拼回同形状的布尔表
    text = df.fillna("").astype(str).apply(lambda column: column.str.strip())
    is_blank = text == ""
    # astype(int)：pandas 的可空布尔/整型会让 NA 一路传播下去，这里先落成普通 int
    return is_blank.sum(axis=1).astype(int)


def dedupe(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """按 keys 联合去重，返回保留的行（保持原有的先后顺序）。"""
    missing = build_missing_mask(df)

    # 规范化后的复合键。任一键缺失的行留 None —— 它们没有可比的联合键，
    # 强行参与分组会把不相干的行判成重复（空值全都相等），所以整行原样保留。
    parts = [df[k].map(normalize_key_value).tolist() for k in keys]
    key_text = pd.Series(
        [
            KEY_SEP.join(values) if all(v != "" for v in values) else None
            for values in zip(*parts)
        ],
        index=df.index,
        dtype="object",
    )

    work = pd.DataFrame(
        {"_key": key_text, "_missing": missing, "_order": range(len(df))},
        index=df.index,
    )

    # 先按缺失值个数、再按出现顺序升序，每组取第一条：
    # 即「null 最少的那条」，打平时取先出现的那条。
    # 用稳定排序而不是 groupby().apply()，是为了让「平局取最早」成为排序的直接结果，
    # 不依赖 groupby 是否保留原顺序这一实现细节。
    chosen = (
        work[work["_key"].notna()]
        .sort_values(["_missing", "_order"], kind="stable")
        .drop_duplicates("_key", keep="first")
    )

    kept = chosen.index.union(work.index[work["_key"].isna()])
    return df.loc[sorted(kept)]


def main() -> int:
    parser = argparse.ArgumentParser(description="按联合键对抽取结果去重，保留 null 最少的那条")
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT, help="输入的抽取结果 Excel")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="输出去重后的 Excel")
    parser.add_argument(
        "--keys", nargs="+", default=DEFAULT_KEYS, help="联合去重键（默认：项目编号 中标金额）"
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"[错误] 找不到输入文件：{args.input}", file=sys.stderr)
        print(
            "       去重必须拿「抽取结果」（含核心标的物列），它由 run_batch.py 的回填产出。",
            file=sys.stderr,
        )
        return 1

    df = pd.read_excel(args.input)

    # 缺列就停：尤其是缺「核心标的物」——那说明输入拿的是原始数据，
    # 继续跑只会得到一份没有抽取成果的假去重结果
    absent = [c for c in [*args.keys, RESULT_COLUMN] if c not in df.columns]
    if absent:
        print(f"[错误] 输入缺少列：{absent}", file=sys.stderr)
        print(f"       实际列（{len(df.columns)}）：{list(df.columns)}", file=sys.stderr)
        return 1

    result = dedupe(df, args.keys)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # index=False：Excel 里不需要 DataFrame 行号，多一列反而会打乱下游按列取数的脚本
    result.to_excel(args.output, index=False)

    print(f"输入：{args.input}")
    print(f"输出：{args.output}")
    print(f"  读取行数：{len(df)}")
    print(f"  去重后行数：{len(result)}")
    print(f"  删除行数：{len(df) - len(result)}")
    print(f"  列数：{len(result.columns)}（含 {RESULT_COLUMN}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
