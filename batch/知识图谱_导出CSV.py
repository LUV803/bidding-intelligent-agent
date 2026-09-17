"""切出知识图谱要的 6 列，连同交易频次一起导出成 CSV。

输入：batch/结果/招标采购标的物信息提取训练数据_2_标的物.xlsx（8751 行 × 19 列）
      batch/结果/标的物_交易频次.xlsx（2309 行 × 2 列）
输出：batch/结果/招标采购标的物信息提取训练数据_2_知识图谱.xlsx（6 列）
      batch/结果/招标采购标的物信息提取训练数据_2_知识图谱.csv
      batch/结果/标的物_交易频次.csv

⚠ 列名映射（COLUMN_MAP）：请求里说的「采购商 / 供应商 / 交易额 / 所在地」在表里
   **并不叫这些名字**，实际是 采购人 / 中标人 / 中标金额 / 市区。脚本按映射改名后再输出。
   这张映射错了不会报错，只会让整张图挂在错的字段上——改之前先核对一遍。

⚠ 这里是**副本**，不碰 _2_标的物.xlsx。

⚠ CSV 用 UTF-8（无 BOM）写，**故意不走 Excel 的「另存为 CSV」**：中文版 Excel 那个入口
   写出来的是 GBK，而 Neo4j 只认 UTF-8，导进去整片中文变乱码，且不会报错。
   要在 Excel 里看这两个 CSV，用「数据 → 从文本/CSV」导入，别双击。

运行：
    uv run python batch/知识图谱_导出CSV.py
    uv run python batch/知识图谱_导出CSV.py -i 别的表.xlsx -f 别的频次表.xlsx
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
DEFAULT_FREQ = RESULT_DIR / "标的物_交易频次.xlsx"

# 副本文件名里插的标记
KG_SUFFIX = "_知识图谱"

# 两边对齐用的列
ITEM_COLUMN = "标的物"
FREQ_COLUMN = "交易频次"

# (源表列名, 输出列名)，**顺序即输出列序**。
# 请求里用的是右边那组词，源表里其实叫左边那组。改名集中在这里做一次，
# 免得下游到处写「采购商其实就是采购人」这种注释。
COLUMN_MAP: list[tuple[str, str]] = [
    ("标的物", "标的物"),
    ("采购人", "采购商"),
    ("代理机构", "代理机构"),
    ("中标人", "供应商"),
    ("中标金额", "交易额"),
    ("市区", "所在地"),
]

# 控制台预览多少种「所在地」取值
PLACE_PREVIEW = 8


def kg_stem(source_stem: str) -> str:
    """源表叫 ..._2_标的物.xlsx，副本要叫 ..._2_知识图谱.xlsx。

    也就是把结尾那个「列名标记」换成「知识图谱」，而不是往后接——直接接会得到
    ..._2_标的物_知识图谱.xlsx，跟约定的文件名对不上。结尾不是 _标的物 时按原样接。
    """
    suffix = f"_{ITEM_COLUMN}"
    base = source_stem[: -len(suffix)] if source_stem.endswith(suffix) else source_stem
    return f"{base}{KG_SUFFIX}"


def tidy_amount(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """交易额全是整数就转成可空整数，否则原样保留。

    源表里是 float64，直接导出去 CSV 会写成 1684000.0，进图后也变成浮点属性——
    「交易额 > 1000000」这类比较照样能跑，但显示和分组都会带上无意义的小数尾巴。
    只有在确认没有小数时才转，免得把真有角分的金额抹掉。
    """
    values = df["交易额"].dropna()
    if values.empty:
        return df, "交易额：全为空，未做转换"
    if (values % 1 == 0).all():
        out = df.copy()
        out["交易额"] = df["交易额"].astype("Int64")
        return out, f"交易额：全是整数，已从 float64 转为 Int64（{len(values)} 个值）"
    odd = int((values % 1 != 0).sum())
    return df, f"交易额：有 {odd} 个非整数，保留 float64"


def build_kg(df: pd.DataFrame) -> pd.DataFrame:
    """按映射抽列改名。缺列直接抛，不做部分降级——少一列就是另一种图了，不该静默产出。"""
    missing = [src for src, _ in COLUMN_MAP if src not in df.columns]
    if missing:
        raise ValueError(
            f"源表里找不到这些列：{missing}\n"
            f"实际列：{list(df.columns)}"
        )
    # 显式 copy：不然下面改列名会连带改到源 DataFrame，调用方拿到的 df 就不是原来那份了
    return df[[src for src, _ in COLUMN_MAP]].rename(
        columns={src: dst for src, dst in COLUMN_MAP}
    ).copy()


def count_blank(df: pd.DataFrame) -> dict[str, int]:
    """每列的空白数（NaN 和纯空格算同一种）。"""
    return {
        col: int(((df[col].isna()) | (df[col].astype(str).str.strip() == "")).sum())
        for col in df.columns
    }


def find_bad_chars(df: pd.DataFrame) -> list[str]:
    """找出含换行、制表符的单元格。

    CSV 靠引号能装下换行，但 Neo4j 的 LOAD CSV 遇到字段内换行会直接算成新的一行，
    整条记录错位——所以这里先报出来，别等导入时才莫名其妙地少行。
    """
    problems: list[str] = []
    for col in df.columns:
        text = df[col].dropna().astype(str)
        hits = text[text.str.contains(r"[\r\n\t]", regex=True)]
        if not hits.empty:
            problems.append(f"{col}：{len(hits)} 个单元格含换行/制表符，例如 {hits.iloc[0]!r}")
    return problems


def write_csv(df: pd.DataFrame, path: Path) -> None:
    """UTF-8（无 BOM）+ LF。

    不带 BOM 是给 Neo4j 用的：BOM 会粘在第一列列名上，`row.标的物` 就取不到了。
    LF 是显式指定的——Windows 上 pandas 默认吐 CRLF。
    """
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="切出知识图谱的 6 列并导出 CSV")
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT, help="含 19 列的源表")
    parser.add_argument("-f", "--freq", type=Path, default=DEFAULT_FREQ, help="标的物_交易频次.xlsx")
    parser.add_argument("-o", "--outdir", type=Path, default=RESULT_DIR, help="输出目录")
    args = parser.parse_args()

    for path, label in ((args.input, "源表"), (args.freq, "频次表")):
        if not path.exists():
            print(f"[错误] 找不到{label}：{path}", file=sys.stderr)
            return 1

    df = pd.read_excel(args.input)
    try:
        kg = build_kg(df)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    freq = pd.read_excel(args.freq)
    if ITEM_COLUMN not in freq.columns or FREQ_COLUMN not in freq.columns:
        print(
            f"[错误] 频次表里缺少「{ITEM_COLUMN}」或「{FREQ_COLUMN}」列，实际列：{list(freq.columns)}",
            file=sys.stderr,
        )
        return 1

    kg, amount_note = tidy_amount(kg)

    args.outdir.mkdir(parents=True, exist_ok=True)
    out_stem = kg_stem(args.input.stem)
    kg_xlsx = args.outdir / f"{out_stem}.xlsx"
    kg_csv = args.outdir / f"{out_stem}.csv"
    freq_csv = args.outdir / f"{args.freq.stem}.csv"

    print(f"源表：{args.input}")
    print(f"  读取行数：{len(df)}  列数：{len(df.columns)}")
    print("  列名映射：")
    for src, dst in COLUMN_MAP:
        mark = "" if src == dst else "  ← 改名"
        print(f"    {src}  ->  {dst}{mark}")

    # ---------- 体检 ----------
    blanks = count_blank(kg)
    empty = {col: n for col, n in blanks.items() if n}
    if empty:
        print("\n  空值：")
        for col, n in empty.items():
            print(f"    {col}：{n} 行（{(n / len(kg)):.1%}）")

    bad = find_bad_chars(kg)
    if bad:
        print("\n  [警告] 字段里有换行/制表符，建议先清掉再导：")
        for line in bad:
            print(f"    {line}")

    # 两边对得上才谈得上「把频次挂到标的物节点上」。频次表是从同一列统计出来的，
    # 正常情况下应该严丝合缝；对不上说明两份表不是同一批数据。
    def items(series: pd.Series) -> set[str]:
        text = series.dropna().astype(str).str.strip()
        return set(text[text != ""])

    kg_items = items(kg[ITEM_COLUMN])
    freq_items = items(freq[ITEM_COLUMN])
    orphan = freq_items - kg_items
    no_freq = kg_items - freq_items
    print(f"\n  标的物：图谱 {len(kg_items)} 种 / 频次表 {len(freq_items)} 种")
    if orphan:
        print(f"    [警告] 频次表里有 {len(orphan)} 种在图谱里找不到，会挂不上节点：{sorted(orphan)[:5]}")
    if no_freq:
        print(f"    [警告] 图谱里有 {len(no_freq)} 种在频次表里没有：{sorted(no_freq)[:5]}")
    if not orphan and not no_freq:
        print("    [通过] 两边的标的物完全一致，频次能逐条挂上")

    # 所在地用「市区」单列，值够不够当键得看一眼
    places = items(kg["所在地"]) if "所在地" in kg.columns else set()
    print(f"\n  所在地：{len(places)} 种不同取值")
    for name in sorted(places)[:PLACE_PREVIEW]:
        print(f"    {name}")
    if len(places) > PLACE_PREVIEW:
        print(f"    …另有 {len(places) - PLACE_PREVIEW} 种")

    # 交易额是数字还是带单位的文本，直接决定进图后能不能做数值比较
    print(f"\n  {amount_note}")
    print(f"    样例：{kg['交易额'].dropna().head(3).tolist()}")

    # ---------- 写出 ----------
    # index=False：不要把 pandas 行号当成一列写进去
    kg.to_excel(kg_xlsx, index=False)
    write_csv(kg, kg_csv)
    write_csv(freq, freq_csv)

    print("\n输出：")
    for path, rows in ((kg_xlsx, len(kg)), (kg_csv, len(kg)), (freq_csv, len(freq))):
        print(f"  {path}  ({rows} 行, {path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
